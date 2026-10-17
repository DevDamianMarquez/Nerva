"""Errors raised while checking or carrying out an approved action's execution.

Kept separate from `nerva.core.errors` deliberately: these describe failures at the
executor/provider boundary that `nerva_actions.engine.ActionExecutionEngine` must
interpret (retry vs. give up vs. mark needing reconfiguration), not HTTP-shaped API
errors — the same reasoning as `nerva.nerva_ai.errors` and
`nerva.nerva_connect.google.errors`. Nothing
here may carry a provider token, a raw provider response, an email body, or a stack
trace: every message here must be safe to fold into `Action.result` or a log line
verbatim.
"""

from nerva.core.enums import ActionStatus


class ActionExecutionError(Exception):
    """Base for every error this package's contracts can raise."""


class ActionNotExecutableError(ActionExecutionError):
    """Raised when execution is attempted on an action that is not eligible: its status
    is not `approved`, or its payload does not satisfy the typed contract for its type.

    Distinct from `nerva.core.errors.InvalidStateTransitionError`: that is the HTTP-shaped
    409 `ActionService._transition` raises when a status change itself is illegal, at the
    moment it is attempted, against a row-locked read. This is the provider-neutral check
    `ActionExecutionEngine` runs *before* ever calling `ActionService.start_execution`,
    so a bad payload or a wrong status is caught without touching the database. (A real
    HTTP caller — `routes.actions.execute_action`, §5.6 — maps this to its own 409, since
    this exception type is not itself `NervaError`-shaped.)
    """

    def __init__(self, message: str, *, status: ActionStatus | None = None) -> None:
        super().__init__(message)
        self.status = status


class ActionExecutorUnavailableError(ActionExecutionError):
    """No executor is registered for the action's type, or the executor's own dependency
    (an `Integration`, a provider SDK) is not configured. Callers should treat this as a
    configuration problem, not a transient failure — retrying immediately will not help."""


class ActionProviderError(ActionExecutionError):
    """A call to the external provider failed for a reason other than rate limiting: a
    transport failure, an outage, or a response the executor could not interpret. The
    message must never include the provider's raw response body or any credential."""


class ActionRateLimitError(ActionExecutionError):
    """The provider is rate-limiting us. `retry_after`, when the provider supplied one,
    is the number of seconds to wait before trying again."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after
