"""Milestone 5 Block 2: the execution engine.

`ActionExecutionEngine` is the one thing allowed to drive an approved `Action` all the
way to `completed`/`failed`. It does not duplicate any existing component — it
orchestrates them:

    approved Action
        -> ActionService.get                          (workspace-scoped load)
        -> nerva_actions.eligibility.build_execution_input  (status + payload check)
        -> resolve an ActionExecutor for the action's type  (fail fast if none)
        -> ActionService.start_execution                    (approved -> executing, commits)
        -> ActionExecutor.execute                            (the one external call)
        -> ActionService.complete / ActionService.fail       (executing -> terminal, commits)

No Gmail/OpenAI/vendor-SDK code, no HTTP concerns, and no workspace/user identity ever
enter this module — see `nerva_actions.executor.ActionExecutionInput` for why the
executor itself cannot see any of that either.

**Executor resolution** is deliberately a plain `Mapping[ActionType, ActionExecutor]`
handed in at construction (by whatever future boundary wires up real adapters), not a
plugin framework. Resolution happens *before* `start_execution` is ever called: an
action whose type has no registered executor fails with `ActionExecutorUnavailableError`
while still `approved`, never `executing`. Transitioning first and discovering the
executor second would strand the action in `executing` with no way back (lifecycle only
allows `executing -> completed | failed`) — so the engine checks everything it can
without touching the database before it ever calls `start_execution`.

**Transaction boundary.** `ActionService.start_execution` commits `approved -> executing`
in its own transaction; the engine then calls the executor with no open transaction or
row lock; `complete`/`fail` commits the terminal transition in a second transaction. This
is the existing `ActionService` commit boundary (see `docs/ARCHITECTURE.md` §5), reused
rather than re-implemented. It leaves one crash window: if the external provider
succeeds but the process crashes before `complete` commits, the `Action` is stuck at
`executing` forever (lifecycle has no way out of `executing` except `complete`/`fail`).
This block does not solve that with a worker/queue/reconciler — see `docs/ARCHITECTURE.md`
§5.2 for why, and what a future reconciliation step would need.

**Idempotency.** The engine calls `ActionExecutor.execute` at most once per `execute()`
call, with `ActionExecutionInput.idempotency_key` set to `str(action.id)` (from
`build_execution_input`). Double-execution across *separate* calls is prevented the same
way every other status change is: `ActionService.start_execution` -> `_transition` locks
the row and re-validates `approved -> executing` inside its own transaction, so a second
concurrent `execute()` call for the same action fails at that point (a `409
InvalidStateTransitionError`, propagated unchanged) rather than ever reaching the
executor.
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionType
from nerva.models import Action
from nerva.nerva_actions.eligibility import build_execution_input
from nerva.nerva_actions.errors import (
    ActionExecutionError,
    ActionExecutorUnavailableError,
    ActionNotExecutableError,
    ActionProviderError,
    ActionRateLimitError,
)
from nerva.nerva_actions.executor import ActionExecutionResult, ActionExecutor
from nerva.services.actions import ActionService

ActionExecutorRegistry = Mapping[ActionType, ActionExecutor]


def safe_error_message(error: ActionExecutionError) -> str:
    """A bounded, generic classification of `error` — never `str(error)` verbatim, which
    (from a real provider adapter) could echo back response text that must not land in
    `Action.result` or a log line. Mirrors `services.analysis._safe_error_message`.
    `retry_after` is a plain number the provider itself supplied, not raw response data,
    so it is safe to fold into the message.

    Public (no leading underscore) so a caller outside this module — the API route that
    constructs an `ActionExecutionEngine` and must also handle an
    `ActionNotExecutableError`/`ActionExecutorUnavailableError` that *escapes* `execute()`
    (raised before `start_execution`, see its docstring) — can map that to the exact same
    safe message this module already uses for the same error type, rather than
    re-deriving a second, possibly-inconsistent mapping."""
    if isinstance(error, ActionRateLimitError):
        message = "The provider is rate-limiting requests."
        if error.retry_after is not None:
            message += f" Retry after {error.retry_after:g}s."
        return message
    if isinstance(error, ActionProviderError):
        return "Could not reach the provider."
    if isinstance(error, ActionNotExecutableError):
        return "The action is not executable."
    if isinstance(error, ActionExecutorUnavailableError):
        return "No executor is available for this action."
    return "The action could not be executed."


def _safe_result(result: ActionExecutionResult) -> dict[str, Any]:
    """`ActionExecutionResult.metadata` is already documented as safe-only (see
    `nerva_actions.executor`); this just folds `external_reference_id` into the same
    dict `ActionService.complete` persists as `Action.result`."""
    safe: dict[str, Any] = dict(result.metadata)
    if result.external_reference_id is not None:
        safe["external_reference_id"] = result.external_reference_id
    return safe


class ActionExecutionEngine:
    """Construct one per request/unit of work, the same way `AnalysisService` is
    constructed with its `AIAnalysisClient` — `executors` is the engine's only
    dependency beyond the database session and workspace context."""

    def __init__(
        self, session: Session, ctx: WorkspaceContext, executors: ActionExecutorRegistry
    ) -> None:
        self._actions = ActionService(session, ctx)
        self._executors = executors

    def execute(self, action_id: uuid.UUID) -> Action:
        """Drive one approved `Action` to `completed` or `failed`.

        Raises `nerva.core.errors.NotFoundError` if `action_id` does not name an action
        in this workspace; `nerva_actions.errors.ActionNotExecutableError` if the action
        is not `approved` or its payload fails its type's contract;
        `ActionExecutorUnavailableError` if no executor is registered for the action's
        type; or `nerva.core.errors.InvalidStateTransitionError` (409) if a concurrent
        call already moved the action out of `approved` between this check and
        `start_execution`'s row-locked re-check. In every one of those cases the executor
        is never called and the action's status is left exactly where it was.

        A failure from the executor itself (`ActionExecutionError` and its subclasses) is
        caught and turned into a `failed` action with a safe, provider-neutral message —
        it is not re-raised, mirroring `ActionService.fail` already having committed by
        the time this returns.
        """
        action = self._actions.get(action_id)
        execution_input = build_execution_input(action)
        executor = self._resolve_executor(execution_input.action_type)

        self._actions.start_execution(action_id)

        try:
            result = executor.execute(execution_input)
        except ActionExecutionError as error:
            return self._actions.fail(action_id, error=safe_error_message(error))

        return self._actions.complete(action_id, result=_safe_result(result))

    def _resolve_executor(self, action_type: ActionType) -> ActionExecutor:
        executor = self._executors.get(action_type)
        if executor is None:
            raise ActionExecutorUnavailableError(
                f"No executor is registered for '{action_type.value}'."
            )
        return executor
