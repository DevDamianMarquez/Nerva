"""Milestone 5 Block 5: the Gmail `send_email` provider boundary — the contract Block
4 designed, now with a real implementation behind it. See `docs/ARCHITECTURE.md` §5.5
for the full design and `nerva.nerva_connect.google.send_executor` for the real
`ActionExecutor` that drives this module.

Four small pieces, each independently testable without a network call:

- `GmailSender` — the provider-neutral-to-the-*domain*-layer `Protocol` the real
  `send_email` executor depends on: "send this email using the connected Google
  mailbox." It takes a bare access token, never an `Integration` row or anything
  SQLAlchemy/workspace-shaped — the same "plain values only" discipline `gmail.py`
  and `oauth.py` already follow.
- `GmailAPISender` — the real implementation: builds a plain-text MIME message and
  POSTs it to Gmail's `users.messages.send` endpoint. No read/search logic lives
  here — that stays in `gmail.py` — and no cc/bcc, attachments or HTML. Any failure
  to reach Google, or a 200 response that is not a parseable JSON object containing a
  message id, becomes a `GoogleApiError`; a missing `threadId` is represented as
  `GmailSendResult.thread_id is None`, never fabricated from `message_id`. Exercised
  in isolation (mocked `httpx` transport, no network) by `tests/test_gmail_api_sender.py`.
- `ensure_connected_integration` / `integration_has_send_scope` /
  `ensure_send_scope_granted` — the credential/scope boundary: given the workspace's
  own Google `Integration` row (or `None`, if never connected), decide whether it is
  usable, and whether its stored OAuth grant actually includes `gmail.send` (not just
  `gmail.readonly`) — both *before* ever touching Google.
  `integration_has_send_scope` is the non-raising predicate a read-only caller (e.g.
  the integrations list endpoint) can use to show this; `ensure_send_scope_granted`
  is the fail-closed version an executor about to actually send uses.
- `translate_send_error` — maps a live Gmail-boundary failure (`GoogleAuthError` /
  `GoogleApiError`) into `nerva_actions.errors`' existing, provider-neutral hierarchy,
  now distinguishing rate limiting (429) and other client/server failures using the
  status code `GoogleApiError` preserves (Block 5) instead of collapsing everything
  into one generic `ActionProviderError` the way Block 4 had to.

What this module deliberately does **not** do: obtain or refresh an access token
(that remains `nerva.services.integrations.GoogleCredentialRefresher`'s job, reused —
not duplicated — by `nerva_connect.google.send_executor`), resolve *which*
`Integration` belongs to a workspace (a plain `IntegrationRepository` lookup), or
construct/register an `ActionExecutor` with `ActionExecutionEngine` (see
`send_executor.py` for both).
"""

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Protocol, runtime_checkable

import httpx

from nerva.core.enums import IntegrationStatus
from nerva.models import Integration
from nerva.nerva_actions.errors import (
    ActionExecutionError,
    ActionExecutorUnavailableError,
    ActionProviderError,
    ActionRateLimitError,
)
from nerva.nerva_connect.google.errors import (
    GoogleApiError,
    GoogleApiErrorCategory,
    GoogleAuthError,
)
from nerva.nerva_connect.google.gmail import API_BASE
from nerva.nerva_connect.google.oauth import SCOPE_GMAIL_SEND

_TIMEOUT = httpx.Timeout(15.0)


@dataclass(frozen=True, slots=True)
class GmailSendResult:
    """What `GmailSender.send_email` returns on success — Gmail's own identifiers for
    the created message, and nothing else (no raw API response, no headers).
    `message_id` is what the executor persists as
    `ActionExecutionResult.external_reference_id` (see `nerva_actions.executor`); both
    fields exist specifically so a future reconciliation step has *something* to look
    the message up by later — see the idempotency analysis in
    `docs/ARCHITECTURE.md` §5.5 for why that is still not a full guarantee.

    `thread_id` is `None` when Gmail's response did not include one: `GmailAPISender`
    never invents a thread id (e.g. by defaulting it to `message_id`) just to satisfy
    this field — an absent thread id is represented as absent, not guessed."""

    message_id: str
    thread_id: str | None


@runtime_checkable
class GmailSender(Protocol):
    """ "Send this email using the connected Google mailbox" — the one capability
    `nerva_connect.google.send_executor.GmailSendExecutor` depends on.
    `access_token` is a short-lived Gmail access token the caller already obtained
    and refreshed if necessary (see the module docstring) — this `Protocol` itself
    never touches a refresh token, encryption, or OAuth.

    Raises `nerva_connect.google.errors.GoogleAuthError` / `GoogleApiError` on
    failure, the same convention `GmailClient`'s read methods already use; a caller
    turns that into the domain's own error hierarchy via `translate_send_error`.
    """

    def send_email(
        self, access_token: str, *, recipients: Sequence[str], subject: str, body: str
    ) -> GmailSendResult: ...


class GmailAPISender:
    """The real `GmailSender`: builds a plain-text RFC 5322 message and POSTs it to
    Gmail's `users.messages.send` endpoint over HTTPS — the official API, the same
    one `GmailClient` already uses for reading (see `gmail.py`), never a
    scraping/unofficial route. No cc/bcc, no attachments, no HTML, and no
    read/search call of any kind — that is `gmail.py`'s job, not this one's.

    The ordinary unit-test suite never constructs this class — `send_executor.py` and
    everything above it take a `GmailSender`, and `tests.fakes.google.FakeGmailSender`
    stands in for it there, so that suite makes no network call and sends no real
    email. This class's own behavior (request shape, response parsing, error
    translation) is exercised in isolation, against a mocked `httpx` transport, by
    `tests/test_gmail_api_sender.py` — never against a real Google endpoint."""

    def send_email(
        self, access_token: str, *, recipients: Sequence[str], subject: str, body: str
    ) -> GmailSendResult:
        message = EmailMessage()
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")

        try:
            response = httpx.post(
                f"{API_BASE}/messages/send",
                json={"raw": raw},
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            # `exc` may name the request URL; it never carries the Authorization header
            # or message content. Still never returned to a caller verbatim — see
            # `translate_send_error`, which maps this into a fixed, generic message.
            raise GoogleApiError(f"Could not reach the Gmail API: {exc}") from exc

        if response.status_code == 401:
            raise GoogleAuthError("Gmail rejected the access token.")
        if response.status_code != 200:
            raise GoogleApiError(
                f"Gmail API returned {response.status_code} for messages.send.",
                status_code=response.status_code,
                retry_after=_retry_after_seconds(response) if response.status_code == 429 else None,
            )

        data = _parse_send_response(response)
        message_id = data.get("id")
        if not isinstance(message_id, str) or not message_id:
            raise GoogleApiError(
                "Gmail's send response was missing a message id.",
                status_code=response.status_code,
            )
        thread_id = data.get("threadId")
        return GmailSendResult(
            message_id=message_id,
            thread_id=thread_id if isinstance(thread_id, str) and thread_id else None,
        )


def _parse_send_response(response: httpx.Response) -> dict[str, Any]:
    """Gmail returned HTTP 200, but a 200 status alone does not guarantee a parseable
    JSON object — an empty body, truncated response, or a proxy/CDN error page would
    all raise from `response.json()` otherwise. Converted here into the same
    `GoogleApiError` every other response-shape problem raises, carrying only the
    (successful) status code — never the unparseable body itself, which could be
    arbitrary, non-UTF8, or very large."""
    try:
        data = response.json()
    except ValueError as exc:
        raise GoogleApiError(
            "Gmail's send response was not valid JSON.", status_code=response.status_code
        ) from exc
    if not isinstance(data, dict):
        raise GoogleApiError(
            "Gmail's send response was not a JSON object.", status_code=response.status_code
        )
    return data


def _retry_after_seconds(response: httpx.Response) -> float | None:
    """Google's `Retry-After` header, when present on a 429 — a bare number of
    seconds, never parsed from (or compared against) anything in the response
    body."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def ensure_connected_integration(integration: Integration | None) -> Integration:
    """Raise `ActionExecutorUnavailableError` unless `integration` is a connected
    Google `Integration` the send executor could actually use — before ever calling
    `GmailSender` or refreshing a token. Covers three cases uniformly: never
    connected (`integration is None`), disconnected, and needing reauthorization —
    `ActionExecutorUnavailableError`'s own docstring already anticipates exactly this
    ("the executor's own dependency (an `Integration`, ...) is not configured"), so no
    new error type is introduced for it. `IntegrationStatus.ERROR` (the most recent
    *sync* attempt failed) is deliberately still treated as usable here: a stale import
    sync has no bearing on whether sending would work, and `GmailSender` would still
    discover a genuinely bad credential itself.
    """
    if integration is None or integration.status in (
        IntegrationStatus.DISCONNECTED,
        IntegrationStatus.NEEDS_REAUTH,
    ):
        raise ActionExecutorUnavailableError(
            "Gmail is not connected for this workspace; it needs to be (re)connected."
        )
    return integration


def integration_has_send_scope(integration: Integration) -> bool:
    """Whether `integration`'s stored OAuth grant includes `gmail.send` — not just the
    `gmail.readonly` scope Milestone 3's connect flow originally requested (see
    `oauth.SCOPES`). A plain predicate (never raises) so a read-only caller — the
    Settings UI listing integrations, say — can show this without attempting to send
    first; `ensure_send_scope_granted` is the fail-closed version of the same check,
    for callers about to actually send."""
    return SCOPE_GMAIL_SEND in integration.scopes.split()


def ensure_send_scope_granted(integration: Integration) -> None:
    """Raise `ActionExecutorUnavailableError` unless `integration_has_send_scope`.
    An integration connected before Block 5 ever requested `gmail.send` only has a
    *readonly* token: trying to send with it would, at best, fail at Google and, at
    worst, rely on Google being lenient about an undocumented permission. Fail closed
    here instead, before ever calling `GmailSender` or refreshing a token — the same
    ordering `ensure_connected_integration` already uses.

    Deliberately reuses `ActionExecutorUnavailableError` rather than introducing a
    new `Integration` status or error type: the fix is identical to needing
    reauthorization (reconnect through the OAuth flow, which now always requests
    `gmail.send` too), so the caller-facing outcome should be identical too. Callers
    run this *after* `ensure_connected_integration`, which is what actually narrows
    `integration` to non-`None`.
    """
    if not integration_has_send_scope(integration):
        raise ActionExecutorUnavailableError(
            "Gmail is connected without permission to send email; it needs to be reconnected."
        )


def translate_send_error(error: GoogleAuthError | GoogleApiError) -> ActionExecutionError:
    """Maps a live Gmail-boundary failure into `nerva_actions.errors`' hierarchy.
    Mirrors the split `GoogleCredentialRefresher.refresh_access_token` already makes
    one level up (`GoogleAuthError` -> mark `needs_reauth`, `GoogleApiError` -> mark
    `error`): an auth failure is a configuration problem (the connected account needs
    reconnecting, not a retry).

    For `GoogleApiError`, Block 5 can now go further than Block 4's single catch-all:
    `error.category` (derived only from the HTTP status code `GmailAPISender`
    preserves — never the response body) distinguishes rate limiting (429, ->
    `ActionRateLimitError`, carrying `retry_after` when Google supplied one) from
    other client/server failures (-> `ActionProviderError`). A transport-level
    failure with no status at all (`category is UNKNOWN`) still falls back to the
    same generic `ActionProviderError` Block 4 always raised.
    """
    if isinstance(error, GoogleAuthError):
        return ActionExecutorUnavailableError(
            "Gmail rejected the connected account's credentials; it needs to be reconnected."
        )
    if error.category is GoogleApiErrorCategory.RATE_LIMIT:
        return ActionRateLimitError(
            "Gmail is rate-limiting requests.", retry_after=error.retry_after
        )
    if error.category is GoogleApiErrorCategory.CLIENT_ERROR:
        return ActionProviderError("Gmail rejected the request.")
    if error.category is GoogleApiErrorCategory.SERVER_ERROR:
        return ActionProviderError("Gmail is currently unavailable.")
    return ActionProviderError("Could not reach Gmail.")
