"""Milestone 5 Block 3: the first real `ActionExecutor` — `update_opportunity_stage`.

Moves the `Opportunity` named by `Action.opportunity_id` to
`UpdateOpportunityStagePayload.target_stage`. No Gmail, no OpenAI, no HTTP, no vendor
SDK: this is the one `ActionType` that only ever mutates Nerva's own database, which is
exactly why it is the first type to get a real executor (see `docs/ARCHITECTURE.md`
§5.3 for why it was chosen over `send_email`/`create_task`).

`ActionExecutionInput` deliberately carries neither `workspace_id` nor the `Action` row
itself (see `nerva_actions.executor`), so this executor cannot read `opportunity_id` off
its input the way it would off the payload. Instead it is constructed with the same
`(session, ctx)` the `ActionExecutionEngine` driving it was constructed with — never
from the payload, never from the LLM — and uses `execution_input.idempotency_key`
(`str(action.id)`, see `nerva_actions.eligibility.build_execution_input`) to re-resolve
the owning `Action` through a workspace-scoped `ActionRepository`. That repository's
`workspace_id` binding, not anything on the input, is what makes the lookup
workspace-safe; `Action.opportunity_id` is additionally protected by a composite foreign
key (`tenant_fk`, see `nerva.db.base`), so the database itself refuses to let an action
ever reference an opportunity outside its own workspace.

No commit happens here. `ActionExecutionEngine.execute` calls `ActionService.complete`
immediately after `execute()` returns, and `complete` is what commits — so the
`Opportunity` mutation and the action's `completed` transition land in the same
transaction. Unlike an external-provider executor, there is no window where the mutation
itself has happened but is not yet reflected as `completed`: if the process crashes
before that commit, the mutation is rolled back with it. The `Action` can still be left
stuck at `executing` in that case (`start_execution`'s commit already landed before this
executor ever ran) — the known, documented limitation from Block 2 — but the Opportunity
itself is never left half-changed.
"""

import uuid

from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionType
from nerva.nerva_actions.errors import ActionExecutorUnavailableError, ActionNotExecutableError
from nerva.nerva_actions.executor import ActionExecutionInput, ActionExecutionResult
from nerva.nerva_actions.payloads import UpdateOpportunityStagePayload
from nerva.repositories.workspace_data import ActionRepository, OpportunityRepository


class UpdateOpportunityStageExecutor:
    """One executor, bound to one request's `(session, ctx)` the same way `ActionService`
    and `OpportunityService` already are — see the module docstring for why that, and not
    anything on `ActionExecutionInput`, is what keeps this workspace-safe."""

    action_type = ActionType.UPDATE_OPPORTUNITY_STAGE

    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        self._session = session
        self._ctx = ctx

    def execute(self, execution_input: ActionExecutionInput) -> ActionExecutionResult:
        payload = execution_input.payload
        if not isinstance(payload, UpdateOpportunityStagePayload):
            # The engine only ever reaches this executor for `ActionType.
            # UPDATE_OPPORTUNITY_STAGE`, whose payload contract is always
            # `UpdateOpportunityStagePayload` (see `nerva_actions.payloads.
            # EXECUTABLE_PAYLOADS`) — this means the executor registry itself is
            # misconfigured, not a bad action.
            raise ActionExecutorUnavailableError(
                "update_opportunity_stage executor was called with a different action type's "
                "payload."
            )

        action_id = uuid.UUID(execution_input.idempotency_key)
        action = ActionRepository(self._session, self._ctx.workspace_id).get(action_id)
        opportunity_id = action.opportunity_id if action is not None else None
        if opportunity_id is None:
            # `eligibility.ensure_executable` already requires `opportunity_id` to be set
            # before the engine ever starts executing — reaching here regardless would
            # mean the action (or its opportunity link) disappeared mid-flight.
            raise ActionNotExecutableError("This action has no opportunity to update.")

        opportunity = OpportunityRepository(self._session, self._ctx.workspace_id).get(
            opportunity_id
        )
        if opportunity is None:
            raise ActionNotExecutableError("This action's opportunity could not be found.")

        previous_stage = opportunity.stage
        opportunity.stage = payload.target_stage

        return ActionExecutionResult(
            metadata={
                "opportunity_id": str(opportunity.id),
                "previous_stage": previous_stage.value,
                "new_stage": opportunity.stage.value,
            }
        )
