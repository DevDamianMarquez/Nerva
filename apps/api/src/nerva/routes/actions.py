"""Actions: propose, list, approve, reject."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query, status

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.core.enums import ActionStatus
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
) -> list[ActionRead]:
    actions = ActionService(session, ctx).list(
        status=status_filter, limit=page.limit, offset=page.offset
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
