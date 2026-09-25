"""Errors raised across the AI analysis boundary.

Kept separate from `nerva.core.errors` deliberately: these are provider-facing
failures a service must interpret (retry vs. give up vs. mark needing
reconfiguration), not HTTP-shaped API errors — the same reasoning as
`nerva.nerva_connect.google.errors`.
"""


class AIError(Exception):
    """Base for every error `AIAnalysisClient.analyze` can raise."""


class AIAuthError(AIError):
    """The provider rejected our credentials (missing, invalid, or revoked API key).
    Callers should treat this as a configuration problem, not a transient failure."""


class AIRateLimitError(AIError):
    """The provider is rate-limiting us. `retry_after`, when the provider supplied
    one, is the number of seconds to wait before trying again."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class AIProviderError(AIError):
    """A call to the provider failed for a reason other than auth or rate limiting:
    a transport failure, an outage, or an unexpected response shape."""


class AIOutputValidationError(AIError):
    """The provider's output could not be parsed or did not validate against
    `ConversationAnalysisResult`. Callers must never persist the invalid output as
    if it were a real analysis."""
