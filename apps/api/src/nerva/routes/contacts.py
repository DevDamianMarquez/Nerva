"""Contacts in a workspace."""

import uuid

from fastapi import APIRouter, status

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.schemas.common import WORKSPACE_ERRORS
from nerva.schemas.crm import ContactCreate, ContactRead
from nerva.services.crm import ContactService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/contacts", tags=["contacts"], responses=WORKSPACE_ERRORS
)


@router.post("", response_model=ContactRead, status_code=status.HTTP_201_CREATED)
def create_contact(
    workspace_id: uuid.UUID, body: ContactCreate, ctx: WorkspaceCtx, session: SessionDep
) -> ContactRead:
    """Create a contact. Emails are unique per workspace (409 on duplicates)."""
    return ContactRead.model_validate(ContactService(session, ctx).create(body))


@router.get("", response_model=list[ContactRead])
def list_contacts(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep, page: PaginationDep
) -> list[ContactRead]:
    contacts = ContactService(session, ctx).list(limit=page.limit, offset=page.offset)
    return [ContactRead.model_validate(contact) for contact in contacts]


@router.get("/{contact_id}", response_model=ContactRead)
def read_contact(
    workspace_id: uuid.UUID, contact_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> ContactRead:
    return ContactRead.model_validate(ContactService(session, ctx).get(contact_id))
