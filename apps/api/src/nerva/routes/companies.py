"""Companies in a workspace."""

import uuid

from fastapi import APIRouter, status

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.schemas.common import WORKSPACE_ERRORS
from nerva.schemas.crm import CompanyCreate, CompanyRead
from nerva.services.crm import CompanyService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/companies", tags=["companies"], responses=WORKSPACE_ERRORS
)


@router.post("", response_model=CompanyRead, status_code=status.HTTP_201_CREATED)
def create_company(
    workspace_id: uuid.UUID, body: CompanyCreate, ctx: WorkspaceCtx, session: SessionDep
) -> CompanyRead:
    return CompanyRead.model_validate(CompanyService(session, ctx).create(body))


@router.get("", response_model=list[CompanyRead])
def list_companies(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep, page: PaginationDep
) -> list[CompanyRead]:
    companies = CompanyService(session, ctx).list(limit=page.limit, offset=page.offset)
    return [CompanyRead.model_validate(company) for company in companies]


@router.get("/{company_id}", response_model=CompanyRead)
def read_company(
    workspace_id: uuid.UUID, company_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> CompanyRead:
    return CompanyRead.model_validate(CompanyService(session, ctx).get(company_id))
