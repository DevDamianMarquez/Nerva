"""Shared plumbing for workspace-scoped services."""

import uuid

from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.errors import InvalidReferenceError
from nerva.repositories.workspace_data import (
    CompanyRepository,
    ContactRepository,
    ConversationRepository,
    OpportunityRepository,
)
from nerva.services.activity import ActivityService


class ScopedService:
    """A service bound to an authorized workspace context.

    Services own the transaction boundary: they commit once, at the end of a use case.
    """

    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        self.session = session
        self.ctx = ctx
        self.activity = ActivityService(session, ctx.workspace_id)

    def _require_company(self, company_id: uuid.UUID | None) -> None:
        if (
            company_id is not None
            and CompanyRepository(self.session, self.ctx.workspace_id).get(company_id) is None
        ):
            raise InvalidReferenceError("company_id does not refer to a company in this workspace.")

    def _require_contact(self, contact_id: uuid.UUID | None) -> None:
        if (
            contact_id is not None
            and ContactRepository(self.session, self.ctx.workspace_id).get(contact_id) is None
        ):
            raise InvalidReferenceError("contact_id does not refer to a contact in this workspace.")

    def _require_conversation(self, conversation_id: uuid.UUID | None) -> None:
        if (
            conversation_id is not None
            and ConversationRepository(self.session, self.ctx.workspace_id).get(conversation_id)
            is None
        ):
            raise InvalidReferenceError(
                "conversation_id does not refer to a conversation in this workspace."
            )

    def _require_opportunity(self, opportunity_id: uuid.UUID | None) -> None:
        if (
            opportunity_id is not None
            and OpportunityRepository(self.session, self.ctx.workspace_id).get(opportunity_id)
            is None
        ):
            raise InvalidReferenceError(
                "opportunity_id does not refer to an opportunity in this workspace."
            )
