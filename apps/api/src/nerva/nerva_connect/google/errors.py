"""Errors raised across the Google OAuth / Gmail boundary.

Kept separate from `nerva.core.errors` deliberately: these are provider-facing failures
a service must interpret (into `needs_reauth` vs `error`), not HTTP-shaped API errors.
"""


class GoogleAuthError(Exception):
    """The authorization grant itself is bad: Google rejected or revoked it.
    Callers should mark the integration `needs_reauth`, not merely `error`."""


class GoogleApiError(Exception):
    """A Google API call failed for a reason other than a bad grant (network, rate
    limit, unexpected response shape, …). Callers should mark the integration
    `error`, not claim the sync succeeded."""
