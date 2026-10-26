"""Actions: propose, list, approve, reject, execute."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, status

from nerva.api.deps import ActionExecutorsDep, PaginationDep, SessionDep, WorkspaceCtx
from nerva.core.enums import ActionStatus, MemberRole
from nerva.core.errors import AuthorizationError, ConflictError
from nerva.nerva_actions.engine import ActionExecutionEngine, safe_error_message
from nerva.nerva_actions.errors import ActionExecutorUnavailableError, ActionNotExecutableError
from nerva.schemas.actions import ActionCreate, ActionRead, ActionReject
from nerva.schemas.common import WORKSPACE_ERRORS, ErrorResponse
from nerva.services.actions import ActionService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/actions",
    tags=["actions"],
    responses=WORKSPACE_ERRORS,
)

TRANSITION_ERRORS: dict[int | str, dict[str, Any]] = {
    409: {"model": ErrorResponse, "description": "The action's current status does not allow this."}
}

EXECUTE_ERRORS: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Only the workspace owner can execute an action."},
    409: {
        "model": ErrorResponse,
        "description": (
            "The action is not approved, its payload is invalid, or no executor/connected "
            "integration is available for it."
        ),
    },
}


@router.post("", response_model=ActionRead, status_code=status.HTTP_201_CREATED)
def propose_action(
    workspace_id: uuid.UUID, body: ActionCreate, ctx: WorkspaceCtx, session: SessionDep
) -> ActionRead:
    """Propose an action. It starts as `proposed` and does nothing until a human approves it."""
    return ActionRead.model_validate(ActionService(session, ctx).propose(body))


@router.get("", response_model=list[ActionRead])
def list_actions(
    workspace_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    page: PaginationDep,
    status_filter: Annotated[
        ActionStatus | None, Query(alias="status", description="Only actions in this status.")
    ] = None,
    conversation_id: Annotated[
        uuid.UUID | None,
        Query(description="Only actions reasoned from this conversation."),
    ] = None,
) -> list[ActionRead]:
    """`conversation_id` composes with `status` (both narrow the same `AND`
    filter). An unknown or cross-workspace `conversation_id` is not an error:
    it is a plain equality filter on an already workspace-scoped column, so
    it simply yields an empty list — it never reveals whether a conversation
    exists in another tenant (see `nerva.repositories.base.ScopedRepository`).
    """
    actions = ActionService(session, ctx).list(
        status=status_filter,
        conversation_id=conversation_id,
        limit=page.limit,
        offset=page.offset,
    )
    return [ActionRead.model_validate(action) for action in actions]


@router.get("/{action_id}", response_model=ActionRead)
def read_action(
    workspace_id: uuid.UUID, action_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> ActionRead:
    return ActionRead.model_validate(ActionService(session, ctx).get(action_id))


@router.post("/{action_id}/approve", response_model=ActionRead, responses=TRANSITION_ERRORS)
def approve_action(
    workspace_id: uuid.UUID, action_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> ActionRead:
    """Approve a proposed action. Records the approver and time."""
    return ActionRead.model_validate(ActionService(session, ctx).approve(action_id))


@router.post("/{action_id}/reject", response_model=ActionRead, responses=TRANSITION_ERRORS)
def reject_action(
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    body: Annotated[ActionReject | None, Body()] = None,
) -> ActionRead:
    """Reject a proposed action, optionally with a reason."""
    reason = body.reason if body else None
    return ActionRead.model_validate(ActionService(session, ctx).reject(action_id, reason=reason))


@router.post("/{action_id}/execute", response_model=ActionRead, responses=EXECUTE_ERRORS)
def execute_action(
    workspace_id: uuid.UUID,
    action_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    executors: ActionExecutorsDep,
) -> ActionRead:
    """Owner-only: carry out an already-`approved` action (`update_opportunity_stage`
    or `send_email`) through `ActionExecutionEngine`, transitioning it to `completed`
    or `failed`.

    Deliberately **more restrictive than `approve`/`reject`**, which any workspace
    member may call: approving only records a decision, while executing is what
    actually spends external resources (sends a real email) — the same owner-only
    bar already used for connecting/syncing Gmail and running an AI backfill (see
    `services.integrations._require_owner`, `routes.analysis.backfill_analysis`).
    This does **not** restrict `approve`/`reject` themselves; a non-owner can still
    approve an action, they just cannot be the one to execute it.

    This never bypasses the status check: `ActionExecutionEngine.execute` only ever
    moves `approved -> executing -> completed|failed` (see `nerva_actions.lifecycle`)
    — an action that is `proposed`, `rejected`, already `executing`, or already
    terminal is rejected (409) before anything is called, exactly as it already is
    for every other caller of the engine (tests included). Nothing here, or anywhere
    in the engine/executor chain, can approve an action on its own: a human must have
    already called `/approve` first.
    """
    if ctx.role != MemberRole.OWNER:
        raise AuthorizationError("Only the workspace owner can execute an action.")

    engine = ActionExecutionEngine(session, ctx, executors)
    try:
        action = engine.execute(action_id)
    except (ActionNotExecutableError, ActionExecutorUnavailableError) as exc:
        # Raised by `build_execution_input`/executor resolution *before*
        # `start_execution` is ever called (see `ActionExecutionEngine.execute`'s
        # docstring) — the action's status is untouched. `safe_error_message` is the
        # exact same mapping the engine itself uses when one of these is raised
        # *during* `executor.execute()` instead, so the message a caller sees for a
        # given error type never depends on which of the two paths raised it.
        raise ConflictError(safe_error_message(exc), details={"code": "not_executable"}) from exc
    return ActionRead.model_validate(action)
