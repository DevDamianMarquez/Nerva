"""Workspace-scoped repositories for business data."""

import uuid
from collections.abc import Sequence

from nerva.core.enums import (
    ActionStatus,
    ConversationChannel,
    IntegrationProvider,
    OpportunityStage,
)
from nerva.models import (
    Action,
    ActivityEvent,
    Company,
    Contact,
    Conversation,
    Integration,
    Message,
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

    def get_by_external_id(
        self, channel: ConversationChannel, external_id: str
    ) -> Conversation | None:
        return self.session.scalars(
            self._select().where(
                Conversation.channel == channel, Conversation.external_id == external_id
            )
        ).one_or_none()

    def list_recent(self, *, limit: int, offset: int) -> Sequence[Conversation]:
        statement = self._select().order_by(
            Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc()
        )
        return self.session.scalars(statement.limit(limit).offset(offset)).all()


class MessageRepository(ScopedRepository[Message]):
    model = Message

    def get_by_external_id(self, external_id: str) -> Message | None:
        return self.session.scalars(
            self._select().where(Message.external_id == external_id)
        ).one_or_none()

    def list_for_conversation(self, conversation_id: uuid.UUID) -> Sequence[Message]:
        return self.session.scalars(
            self._select()
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sent_at)
        ).all()

    def get_latest_for_conversation(self, conversation_id: uuid.UUID) -> Message | None:
        return self.session.scalars(
            self._select()
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.sent_at.desc())
            .limit(1)
        ).one_or_none()


class IntegrationRepository(ScopedRepository[Integration]):
    model = Integration

    def get_by_external_account(
        self, provider: IntegrationProvider, external_account_id: str
    ) -> Integration | None:
        return self.session.scalars(
            self._select().where(
                Integration.provider == provider,
                Integration.external_account_id == external_account_id,
            )
        ).one_or_none()

    def list_all(self) -> Sequence[Integration]:
        return self.session.scalars(self._select().order_by(Integration.created_at)).all()


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
