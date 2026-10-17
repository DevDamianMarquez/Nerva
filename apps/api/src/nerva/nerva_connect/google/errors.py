"""Errors raised across the Google OAuth / Gmail boundary.

Kept separate from `nerva.core.errors` deliberately: these are provider-facing failures
a service must interpret (into `needs_reauth` vs `error`), not HTTP-shaped API errors.
"""

from enum import StrEnum


class GoogleAuthError(Exception):
    """The authorization grant itself is bad: Google rejected or revoked it.
    Callers should mark the integration `needs_reauth`, not merely `error`."""


class GoogleApiErrorCategory(StrEnum):
    """A coarse, safe classification of a failed Google API call — derived only from
    an HTTP status code, never from the response body. Milestone 5 Block 5: a future
    caller (e.g. `nerva_connect.google.sender.translate_send_error`) needs at least
    this much to distinguish "try again later" from "this will never work" without
    ever inspecting provider-controlled response content."""

    RATE_LIMIT = "rate_limit"  # HTTP 429
    CLIENT_ERROR = "client_error"  # other 4xx: bad request, invalid recipient, ...
    SERVER_ERROR = "server_error"  # 5xx: provider-side outage
    UNKNOWN = "unknown"  # no HTTP status available (transport failure, etc.)


def _categorize(status_code: int | None) -> GoogleApiErrorCategory:
    if status_code == 429:
        return GoogleApiErrorCategory.RATE_LIMIT
    if status_code is not None and 400 <= status_code < 500:
        return GoogleApiErrorCategory.CLIENT_ERROR
    if status_code is not None and 500 <= status_code < 600:
        return GoogleApiErrorCategory.SERVER_ERROR
    return GoogleApiErrorCategory.UNKNOWN


class GoogleApiError(Exception):
    """A Google API call failed for a reason other than a bad grant (network, rate
    limit, unexpected response shape, …). Callers should mark the integration
    `error`, not claim the sync succeeded.

    Carries only two pieces of safe, structured metadata beyond the message itself:
    `status_code` (the bare HTTP status, when the failure came from an actual HTTP
    response — `None` for a transport-level failure that never got one) and
    `retry_after` (the number of seconds Google's own `Retry-After` header asked for,
    only ever set on a 429). `category` is derived from `status_code` alone. None of
    this is the response body: a caller that needs to distinguish rate limiting from
    a bad request from a provider outage can do so from `category` without this
    module — or the message string — ever repeating what Google actually sent back.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.category = _categorize(status_code)
        self.retry_after = retry_after
