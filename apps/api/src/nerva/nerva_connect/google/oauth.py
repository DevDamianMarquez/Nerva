"""Google OAuth 2.0 for a server-side web app.

Only `google-auth` is used here (for verifying the ID token's signature — not
something to hand-roll, see `verify_id_token`); the authorization/token-exchange HTTP
calls are plain requests to Google's documented endpoints, which does not need a
heavier SDK.
"""

import contextlib
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

from nerva.core.config import Settings
from nerva.nerva_connect.google.errors import GoogleApiError, GoogleAuthError

AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"  # noqa: S105 (a URL, not a secret)
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# openid+email identify the connected account (a stable `sub`, and an email for
# display only). gmail.readonly (Milestone 3) lets Nerva import mail; gmail.send
# (Milestone 5 Block 5) lets an approved `send_email` action actually send through
# the connected mailbox — nothing else is ever requested. Every *new* connect (or
# reconnect) requests both scopes together; an account connected before Block 5 only
# holds `gmail.readonly` in its stored `Integration.scopes` until it reconnects (see
# `nerva_connect.google.sender.ensure_send_scope_granted`).
SCOPE_IDENTITY = "openid email"
SCOPE_GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_GMAIL_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPES = f"{SCOPE_IDENTITY} {SCOPE_GMAIL_READONLY} {SCOPE_GMAIL_SEND}"

_TIMEOUT = httpx.Timeout(10.0)


@dataclass(frozen=True, slots=True)
class GoogleTokens:
    access_token: str
    refresh_token: str | None
    expires_in: int
    id_token: str | None
    scope: str


@dataclass(frozen=True, slots=True)
class GoogleIdentity:
    """The connected account, from a verified ID token."""

    subject: str
    email: str


class GoogleOAuthClient:
    """The real implementation. Tests use a fake with the same method signatures
    (see `apps/api/tests/fakes/google.py`) — never real network calls."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build_authorization_url(self, state: str) -> str:
        if not self._settings.google_oauth_configured:
            raise GoogleApiError("Google OAuth is not configured on this server.")
        params = {
            "client_id": self._settings.google_client_id,
            "redirect_uri": self._settings.google_redirect_uri,
            "response_type": "code",
            "scope": SCOPES,
            "state": state,
            "access_type": "offline",
            # Forces Google to return a refresh token even on a reconnect, when the
            # user may have already granted this app access once before.
            "prompt": "consent",
        }
        return f"{AUTHORIZATION_ENDPOINT}?{urlencode(params)}"

    def exchange_code(self, code: str) -> GoogleTokens:
        return self._token_request(
            {
                "code": code,
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "redirect_uri": self._settings.google_redirect_uri,
                "grant_type": "authorization_code",
            }
        )

    def refresh(self, refresh_token: str) -> GoogleTokens:
        return self._token_request(
            {
                "refresh_token": refresh_token,
                "client_id": self._settings.google_client_id,
                "client_secret": self._settings.google_client_secret,
                "grant_type": "refresh_token",
            }
        )

    def _token_request(self, data: dict[str, str]) -> GoogleTokens:
        try:
            response = httpx.post(TOKEN_ENDPOINT, data=data, timeout=_TIMEOUT)
        except httpx.HTTPError as exc:
            raise GoogleApiError(f"Could not reach Google's token endpoint: {exc}") from exc

        if response.status_code == 400:
            # Google's documented shape for a bad/revoked grant: {"error": "invalid_grant", ...}
            body = _safe_json(response)
            if body.get("error") in {"invalid_grant", "unauthorized_client"}:
                raise GoogleAuthError(body.get("error_description", body["error"]))
            raise GoogleApiError(f"Google rejected the request: {body}")
        if response.status_code != 200:
            raise GoogleApiError(f"Google's token endpoint returned {response.status_code}.")

        body = _safe_json(response)
        try:
            return GoogleTokens(
                access_token=body["access_token"],
                refresh_token=body.get("refresh_token"),
                expires_in=int(body["expires_in"]),
                id_token=body.get("id_token"),
                scope=body.get("scope", ""),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise GoogleApiError("Google's token response was missing expected fields.") from exc

    def revoke(self, token: str) -> None:
        """Best-effort: disconnect proceeds locally even if this fails (the user may
        have already revoked access from their Google account settings)."""
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(REVOKE_ENDPOINT, params={"token": token}, timeout=_TIMEOUT)

    def verify_id_token(self, id_token_value: str) -> GoogleIdentity:
        """Verifies the ID token's signature, issuer, audience and expiry against
        Google's published keys — never trust an unverified JWT payload."""
        try:
            # google-auth ships no inline type hints for this function.
            claims: dict[str, str] = google_id_token.verify_oauth2_token(  # type: ignore[no-untyped-call]
                id_token_value,
                google_requests.Request(),
                audience=self._settings.google_client_id,
            )
        except ValueError as exc:
            raise GoogleAuthError(f"Google's ID token could not be verified: {exc}") from exc

        subject = claims.get("sub")
        email = claims.get("email")
        if not subject or not email:
            raise GoogleApiError("Google's ID token did not include the expected identity claims.")
        return GoogleIdentity(subject=subject, email=email)


def _safe_json(response: httpx.Response) -> dict[str, str]:
    try:
        data: dict[str, str] = response.json()
    except ValueError:
        return {}
    return data
