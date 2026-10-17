"""The provider-neutral contract a future execution engine drives, and every future
provider adapter implements.

`ActionExecutor` must never depend on a vendor SDK or an `Integration`'s stored token:
the domain/service layer knows only this protocol. A real adapter (Gmail send, a
calendar, a CRM, Slack, ...) lives with its provider client (e.g. `nerva_connect`) and
is wired in at the boundary that constructs an `ActionExecutionEngine` — not implemented
in this block. This mirrors `nerva.nerva_ai.client.AIAnalysisClient`, the equivalent
provider-neutral boundary for AI analysis: like `AIAnalysisClient.analyze`, `execute`
raises an `nerva_actions.errors.ActionExecutionError` subclass on any failure and never
returns a partially-successful result — there is no `success` flag on
`ActionExecutionResult` for this reason.

`nerva_actions.engine.ActionExecutionEngine` is what resolves the right `ActionExecutor`
for an action's type, calls `nerva_actions.eligibility.build_execution_input`, calls
`ActionService.start_execution`, drives the executor, and calls `ActionService.complete`
/ `.fail` with the outcome. Two real implementations exist:
`nerva_actions.opportunity_stage_executor.UpdateOpportunityStageExecutor` (domain-only,
§5.3) and `nerva_connect.google.send_executor.GmailSendExecutor` (Gmail send, §5.5);
`nerva.api.deps.get_action_executors` is where a production `ActionExecutorRegistry`
containing both is actually built and driven by a real HTTP route (§5.6).
`tests.fakes.actions.FakeActionExecutor` remains a third implementation, used only in
tests that want to control the outcome directly rather than exercise a real one.
"""

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from pydantic import JsonValue

from nerva.core.enums import ActionType
from nerva.nerva_actions.payloads import ExecutablePayload


@dataclass(frozen=True, slots=True)
class ActionExecutionInput:
    """Everything one execution attempt needs, and nothing more: no `workspace_id`, no
    `user_id`, no raw `Action` row, no database session, no provider token. An executor
    that is never given an identifier cannot leak or misuse it (the same reasoning as
    `nerva_ai.client.AnalysisInput`).

    `idempotency_key` is the action's own id, as a string — see
    `nerva_actions.eligibility.build_execution_input` and
    `docs/ARCHITECTURE.md` §5 for why one `Action` is exactly one execution attempt, and
    why that makes the action's id the correct idempotency key without any extra
    bookkeeping table. A provider adapter that calls an idempotent provider API should
    pass this key through to it (e.g. as an `Idempotency-Key` header); one that does not
    should still log it, so a duplicate call is at least detectable after the fact.
    """

    action_type: ActionType
    payload: ExecutablePayload
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class ActionExecutionResult:
    """A *successful* execution attempt — what `ActionService.complete` would persist
    into `Action.result`. `metadata` is for safe, structured facts about the outcome (a
    provider message id, a delivery timestamp): never a raw provider response, a token,
    or enough of the message content to reconstruct it. Failure is represented by
    `execute` raising instead of returning (see the module docstring), not by a field on
    this class."""

    external_reference_id: str | None = None
    metadata: dict[str, JsonValue] = field(default_factory=dict)


@runtime_checkable
class ActionExecutor(Protocol):
    """One typed executor per `ActionType`. Implementations live with their provider
    adapter, not in the domain layer."""

    action_type: ActionType

    def execute(self, execution_input: ActionExecutionInput) -> ActionExecutionResult: ...
