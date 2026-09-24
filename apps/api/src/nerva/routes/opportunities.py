"""Opportunities in a workspace."""

import uuid

from fastapi import APIRouter, status

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.core.enums import OpportunityStage
from nerva.schemas.common import WORKSPACE_ERRORS
from nerva.schemas.crm import OpportunityCreate, OpportunityRead
from nerva.services.crm import OpportunityService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/opportunities",
    tags=["opportunities"],
    responses=WORKSPACE_ERRORS,
)


@router.post("", response_model=OpportunityRead, status_code=status.HTTP_201_CREATED)
def create_opportunity(
    workspace_id: uuid.UUID, body: OpportunityCreate, ctx: WorkspaceCtx, session: SessionDep
) -> OpportunityRead:
    return OpportunityRead.model_validate(OpportunityService(session, ctx).create(body))


@router.get("", response_model=list[OpportunityRead])
def list_opportunities(
    workspace_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    page: PaginationDep,
    stage: OpportunityStage | None = None,
) -> list[OpportunityRead]:
    opportunities = OpportunityService(session, ctx).list(
        stage=stage, limit=page.limit, offset=page.offset
    )
    return [OpportunityRead.model_validate(opportunity) for opportunity in opportunities]


@router.get("/{opportunity_id}", response_model=OpportunityRead)
def read_opportunity(
    workspace_id: uuid.UUID, opportunity_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> OpportunityRead:
    return OpportunityRead.model_validate(OpportunityService(session, ctx).get(opportunity_id))
