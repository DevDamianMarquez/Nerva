"""The gate `nerva_actions.engine.ActionExecutionEngine` passes before calling
`ActionService.start_execution` and handing an action to an `ActionExecutor`.

Two independent checks compose here:

1. **Status eligibility** — reuses `nerva_actions.lifecycle.can_transition` rather than a
   second state machine: an action is eligible only if `approved -> executing` is a legal
   transition for its current status. This is deliberately the *same* rule
   `ActionService._transition` enforces at the database layer (row-locked, inside the
   transaction that flips the status); this module lets a caller check it before ever
   opening that transaction, so a doomed attempt (wrong status, unparseable payload)
   fails fast without taking a row lock for nothing.
2. **Payload eligibility** — `nerva_actions.payloads.parse_action_payload` must succeed.
3. **Reference completeness** — `update_opportunity_stage` needs `Action.opportunity_id`
   to execute, but (deliberately, see `nerva_actions.payloads`) that id lives on the
   `Action` row, not inside the JSON payload, so `parse_action_payload` alone cannot
   catch a proposal that never set it. This is the one place that is checked, before
   `start_execution` is ever called — the same "fail fast, before touching the database"
   reasoning as the other two checks.

Neither check mutates anything, and neither is a substitute for the database transition.
`ActionRepository.get_for_update` + `ensure_transition` remain the actual, race-proof
guarantee that only one caller can ever move a given action into `executing` — see
`docs/ARCHITECTURE.md` §5. This module exists so a malformed or wrongly-timed execution
attempt is rejected with a clear, provider-neutral error before it ever reaches that far.
"""

from nerva.core.enums import ActionStatus, ActionType
from nerva.models import Action
from nerva.nerva_actions.errors import ActionExecutorUnavailableError, ActionNotExecutableError
from nerva.nerva_actions.executor import ActionExecutionInput
from nerva.nerva_actions.lifecycle import can_transition
from nerva.nerva_actions.payloads import ExecutablePayload, parse_action_payload


def _resolve_action_type(action: Action) -> ActionType:
    try:
        return ActionType(action.type)
    except ValueError as error:
        raise ActionExecutorUnavailableError(f"Unknown action type '{action.type}'.") from error


def ensure_executable(action: Action) -> ExecutablePayload:
    """Raise unless `action` is eligible for execution right now, and return its parsed,
    typed payload.

    Only `approved` is eligible: `proposed` and `rejected` have not been (or will never
    be) approved, and `executing` / `completed` / `failed` have already started or
    finished — re-executing a finished (or already-executing) action is never allowed. A
    retry is always a new proposal, so every attempt keeps its own approval and audit
    trail (see `nerva_actions.lifecycle`'s module docstring).

    Also raises `ActionNotExecutableError` if `action_type` is `update_opportunity_stage`
    and `action.opportunity_id` is unset — see check 3 in the module docstring.
    """
    if not can_transition(action.status, ActionStatus.EXECUTING):
        raise ActionNotExecutableError(
            f"An action that is {action.status.value} cannot be executed.",
            status=action.status,
        )
    action_type = _resolve_action_type(action)
    payload = parse_action_payload(action_type, action.payload)
    if action_type is ActionType.UPDATE_OPPORTUNITY_STAGE and action.opportunity_id is None:
        raise ActionNotExecutableError(
            "An 'update_opportunity_stage' action must have an opportunity_id."
        )
    return payload


def build_execution_input(action: Action) -> ActionExecutionInput:
    """`ensure_executable`, plus shaping the result into what an `ActionExecutor`
    actually receives. `idempotency_key` is the action's own id: see
    `nerva_actions.executor.ActionExecutionInput` for why that is sufficient without a
    separate key or bookkeeping table."""
    payload = ensure_executable(action)
    return ActionExecutionInput(
        action_type=_resolve_action_type(action),
        payload=payload,
        idempotency_key=str(action.id),
    )
