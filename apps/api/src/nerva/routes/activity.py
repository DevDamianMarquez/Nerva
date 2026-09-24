"""The activity feed. Read-only over HTTP: events are recorded by the system as a side
effect of operations, never posted by clients, so the audit trail can be trusted."""

import uuid

from fastapi import APIRouter

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.schemas.actions import ActivityEventRead
from nerva.schemas.common import WORKSPACE_ERRORS
from nerva.services.activity import ActivityService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/activity", tags=["activity"], responses=WORKSPACE_ERRORS
)


@router.get("", response_model=list[ActivityEventRead])
def list_activity(
    workspace_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    page: PaginationDep,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> list[ActivityEventRead]:
    """Most recent events first."""
    events = ActivityService(session, ctx.workspace_id).list(
        entity_type=entity_type, entity_id=entity_id, limit=page.limit, offset=page.offset
    )
    return [ActivityEventRead.model_validate(event) for event in events]
