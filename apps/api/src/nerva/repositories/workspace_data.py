"""Workspace-scoped repositories for business data."""

import uuid
from collections.abc import Sequence

from sqlalchemy import Row, or_, select

from nerva.core.enums import (
    ActionStatus,
    AnalysisStatus,
    BusinessSignalStatus,
    BusinessSignalType,
    ConversationChannel,
    ConversationPriority,
    IntegrationProvider,
    OpportunityStage,
)
from nerva.models import (
    Action,
    ActivityEvent,
    BusinessSignal,
    Company,
    Contact,
    Conversation,
    ConversationAnalysis,
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

    def list_needing_analysis(
        self,
        *,
        limit: int,
        current_schema_version: int,
        current_prompt_version: str,
        current_model_id: str,
    ) -> Sequence[Conversation]:
        """A cheap, deliberately imperfect pre-filter for backfill: no analysis
        yet, a previously failed one, one whose conversation has a newer
        message than the analysis's `analyzed_at`, or one whose completed
        analysis was produced by an older schema/prompt/model than is
        currently configured — a conversation that has not changed at all
        still needs re-analysis once any of those three change, since they
        change what an analysis of the same content would say. This is NOT
        the authoritative idempotency check — `AnalysisService.analyze`'s
        fingerprint comparison is (it already folds all three version stamps
        into the fingerprint) — so a false positive here (a conversation this
        pre-filter includes but whose fingerprint has not actually changed)
        still costs zero provider calls, just one cheap re-check. Most
        recently active conversations are prioritized. Never calls the AI
        provider itself: `current_schema_version`/`current_prompt_version`
        are module constants and `current_model_id` is a configured client's
        attribute, all already known to the caller before this query runs."""
        stale = or_(
            ConversationAnalysis.id.is_(None),
            ConversationAnalysis.status != AnalysisStatus.COMPLETED,
            Conversation.last_message_at > ConversationAnalysis.analyzed_at,
            ConversationAnalysis.schema_version != current_schema_version,
            ConversationAnalysis.prompt_version != current_prompt_version,
            ConversationAnalysis.model_id != current_model_id,
        )
        statement = (
            select(Conversation)
            .outerjoin(
                ConversationAnalysis,
                (ConversationAnalysis.conversation_id == Conversation.id)
                & (ConversationAnalysis.workspace_id == Conversation.workspace_id),
            )
            .where(Conversation.workspace_id == self.workspace_id, stale)
            .order_by(
                Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc()
            )
            .limit(limit)
        )
        return self.session.scalars(statement).all()

    def list_needing_attention(
        self, *, limit: int, offset: int
    ) -> Sequence[Row[tuple[Conversation, ConversationAnalysis]]]:
        """Conversations whose current *completed* analysis flagged them as
        needing a reply or as high/urgent priority — a read-only, deterministic
        query over already-persisted analysis fields, for the future Today/Inbox
        "needs attention" view. Creates no new signal type (see
        `nerva_core.rules`'s module docstring on why `requires_response` stays a
        queryable property rather than a persistent decision like
        `potential_opportunity`)."""
        statement = (
            select(Conversation, ConversationAnalysis)
            .join(
                ConversationAnalysis,
                (ConversationAnalysis.conversation_id == Conversation.id)
                & (ConversationAnalysis.workspace_id == Conversation.workspace_id),
            )
            .where(
                Conversation.workspace_id == self.workspace_id,
                ConversationAnalysis.status == AnalysisStatus.COMPLETED,
                or_(
                    ConversationAnalysis.requires_response.is_(True),
                    ConversationAnalysis.priority.in_(
                        [ConversationPriority.HIGH, ConversationPriority.URGENT]
                    ),
                ),
            )
            .order_by(Conversation.last_message_at.desc().nulls_last())
            .limit(limit)
            .offset(offset)
        )
        return self.session.execute(statement).all()


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


class ConversationAnalysisRepository(ScopedRepository[ConversationAnalysis]):
    model = ConversationAnalysis

    def get_for_conversation(
        self, conversation_id: uuid.UUID, *, for_update: bool = False
    ) -> ConversationAnalysis | None:
        """The current analysis for a conversation, if one exists — there is at
        most one per conversation (see the model's docstring). `for_update` locks
        the row so a future `AnalysisService` can check-then-write without racing
        a concurrent analysis attempt for the same conversation."""
        statement = self._select().where(ConversationAnalysis.conversation_id == conversation_id)
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalars(statement).one_or_none()


class BusinessSignalRepository(ScopedRepository[BusinessSignal]):
    model = BusinessSignal

    def get_for_conversation_and_type(
        self,
        conversation_id: uuid.UUID,
        signal_type: BusinessSignalType,
        *,
        for_update: bool = False,
    ) -> BusinessSignal | None:
        """The current signal of `signal_type` for a conversation, if one
        exists — there is at most one per (conversation, type), ever (see the
        model's docstring). `for_update` locks the row so two concurrent
        materialization attempts for the same conversation cannot race."""
        statement = self._select().where(
            BusinessSignal.conversation_id == conversation_id, BusinessSignal.type == signal_type
        )
        if for_update:
            statement = statement.with_for_update()
        return self.session.scalars(statement).one_or_none()

    def get_for_update(self, signal_id: uuid.UUID) -> BusinessSignal | None:
        """Row-locking read used for confirm/dismiss, mirroring
        `ActionRepository.get_for_update`."""
        return self.session.scalars(
            self._select().where(BusinessSignal.id == signal_id).with_for_update()
        ).one_or_none()

    def list_filtered(
        self,
        *,
        status: BusinessSignalStatus | None,
        signal_type: BusinessSignalType | None,
        limit: int,
        offset: int,
    ) -> Sequence[BusinessSignal]:
        statement = self._select()
        if status is not None:
            statement = statement.where(BusinessSignal.status == status)
        if signal_type is not None:
            statement = statement.where(BusinessSignal.type == signal_type)
        return self._page(statement, limit=limit, offset=offset)


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
