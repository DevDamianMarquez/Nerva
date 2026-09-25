"""Fakes for the Google OAuth / Gmail boundary (`nerva.nerva_connect.google`).

Tests never make a real network call or need a real Google account: the FastAPI app
under test has `get_google_oauth_client` / `get_gmail_client` overridden to return
these (see `tests/conftest.py`).
"""

import base64
from datetime import datetime

from nerva.nerva_connect.google.errors import GoogleApiError, GoogleAuthError
from nerva.nerva_connect.google.gmail import RawGmailMessage
from nerva.nerva_connect.google.oauth import GoogleIdentity, GoogleTokens

DEFAULT_SCOPE = "openid email https://www.googleapis.com/auth/gmail.readonly"


class FakeGoogleOAuthClient:
    """Scripted responses standing in for Google's OAuth endpoints. Set `fail_exchange`
    / `fail_refresh` to simulate Google rejecting the grant (a revoked or expired
    refresh token)."""

    def __init__(
        self,
        *,
        identity: GoogleIdentity | None = None,
        access_token: str = "fake-access-token",
        refresh_token: str | None = "fake-refresh-token",
    ) -> None:
        self.identity = identity or GoogleIdentity(
            subject="google-sub-1", email="alex@gmail.example"
        )
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.fail_exchange = False
        self.fail_refresh = False
        self.revoked_tokens: list[str] = []
        self.built_states: list[str] = []

    def build_authorization_url(self, state: str) -> str:
        self.built_states.append(state)
        return f"https://accounts.google.com/fake-consent?state={state}"

    def exchange_code(self, code: str) -> GoogleTokens:
        if self.fail_exchange:
            raise GoogleAuthError("fake: invalid_grant")
        return GoogleTokens(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_in=3600,
            id_token="fake-id-token",
            scope=DEFAULT_SCOPE,
        )

    def refresh(self, refresh_token: str) -> GoogleTokens:
        if self.fail_refresh:
            raise GoogleAuthError("fake: invalid_grant (token revoked)")
        return GoogleTokens(
            access_token=self.access_token,
            refresh_token=None,  # Google does not always reissue one
            expires_in=3600,
            id_token=None,
            scope="",
        )

    def revoke(self, token: str) -> None:
        self.revoked_tokens.append(token)

    def verify_id_token(self, id_token_value: str) -> GoogleIdentity:
        return self.identity


class FakeGmailClient:
    """A canned mailbox. Populate `messages` (id -> raw message, see
    `make_gmail_message`) before the sync under test runs."""

    def __init__(self) -> None:
        self.messages: dict[str, RawGmailMessage] = {}
        self.fail_list = False
        self.get_calls: list[str] = []

    def list_recent_message_ids(
        self, access_token: str, *, after: object, max_results: int
    ) -> list[str]:
        if self.fail_list:
            raise GoogleApiError("fake: could not list messages")
        return list(self.messages.keys())[:max_results]

    def get_message(self, access_token: str, message_id: str) -> RawGmailMessage:
        self.get_calls.append(message_id)
        return self.messages[message_id]

    def get_profile(self, access_token: str) -> dict[str, str]:
        return {"emailAddress": "alex@gmail.example"}


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def make_gmail_message(
    *,
    message_id: str,
    thread_id: str,
    sender: str,
    to: str,
    subject: str,
    sent_at: datetime,
    body_text: str | None = None,
    body_html: str | None = None,
    cc: str | None = None,
) -> RawGmailMessage:
    """Builds a raw Gmail `users.messages.get` response shaped closely enough to the
    real API for `nerva.nerva_connect.google.mapper` to parse."""
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": to},
        {"name": "Subject", "value": subject},
    ]
    if cc:
        headers.append({"name": "Cc", "value": cc})

    payload: dict[str, object]
    if body_text is not None and body_html is not None:
        payload = {
            "mimeType": "multipart/alternative",
            "headers": headers,
            "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64(body_text)}},
                {"mimeType": "text/html", "body": {"data": _b64(body_html)}},
            ],
        }
    elif body_html is not None:
        payload = {"mimeType": "text/html", "headers": headers, "body": {"data": _b64(body_html)}}
    else:
        payload = {
            "mimeType": "text/plain",
            "headers": headers,
            "body": {"data": _b64(body_text or "")},
        }

    return {
        "id": message_id,
        "threadId": thread_id,
        "snippet": (body_text or "")[:100],
        "internalDate": str(int(sent_at.timestamp() * 1000)),
        "payload": payload,
    }
