"""Workspace-scoped repositories for business data."""

import uuid
from collections.abc import Sequence

from nerva.core.enums import ActionStatus, OpportunityStage
from nerva.models import (
    Action,
    ActivityEvent,
    Company,
    Contact,
    Conversation,
    Opportunity,
)
from nerva.repositories.base import ScopedRepository


class CompanyRepository(ScopedRepository[Company]):
    model = Company


class ContactRepository(ScopedRepository[Contact]):
    model = Contact

    def get_by_email(self, email: str) -> Contact | None:
        return self.session.scalars(self._select().where(Contact.email == email)).one_or_none()


class ConversationRepository(ScopedRepository[Conversation]):
    model = Conversation


class OpportunityRepository(ScopedRepository[Opportunity]):
    model = Opportunity

    def list_filtered(
        self, *, stage: OpportunityStage | None, limit: int, offset: int
    ) -> Sequence[Opportunity]:
        statement = self._select()
        if stage is not None:
            statement = statement.where(Opportunity.stage == stage)
        return self._page(statement, limit=limit, offset=offset)


class ActionRepository(ScopedRepository[Action]):
    model = Action

    def get_for_update(self, action_id: uuid.UUID) -> Action | None:
        """Row-locking read used for status transitions, so two concurrent approvals
        of the same action cannot both succeed."""
        return self.session.scalars(
            self._select().where(Action.id == action_id).with_for_update()
        ).one_or_none()

    def list_filtered(
        self, *, status: ActionStatus | None, limit: int, offset: int
    ) -> Sequence[Action]:
        statement = self._select()
        if status is not None:
            statement = statement.where(Action.status == status)
        return self._page(statement, limit=limit, offset=offset)


class ActivityEventRepository(ScopedRepository[ActivityEvent]):
    """Append-only: exposes `add` and reads, and no way to update or delete."""

    model = ActivityEvent

    def list_filtered(
        self,
        *,
        entity_type: str | None,
        entity_id: uuid.UUID | None,
        limit: int,
        offset: int,
    ) -> Sequence[ActivityEvent]:
        statement = self._select()
        if entity_type is not None:
            statement = statement.where(ActivityEvent.entity_type == entity_type)
        if entity_id is not None:
            statement = statement.where(ActivityEvent.entity_id == entity_id)
        return self._page(statement, limit=limit, offset=offset)
