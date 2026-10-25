"""Workspace-scoped repositories for business data."""

import uuid
from collections.abc import Sequence

from sqlalchemy import Row, and_, or_, select

from nerva.core.enums import (
    ActionStatus,
    AnalysisStatus,
    BusinessSignalStatus,
    BusinessSignalType,
    ConversationChannel,
    ConversationPriority,
    IntegrationProvider,
    MemoryKind,
    MemoryOrigin,
    MemoryStatus,
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
    Memory,
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
        a concurrent analysis attempt for the same conversation.

        `for_update` also forces `populate_existing`: `with_for_update()` alone
        guarantees the *database* blocks on, then returns, the row's current
        committed values — it says nothing about what the *ORM* hands back. If
        this session already holds a (non-expired) `ConversationAnalysis` for
        this id — e.g. it mutated and committed this very row earlier in its own
        lifetime — SQLAlchemy's identity map returns that cached Python object
        as-is rather than repopulating it from the fresh row data, regardless of
        what the lock just waited on. Both the production session factory
        (`db.session.make_session_factory`) and the test fixture (`tests.
        conftest.db_session`) set `expire_on_commit=False`, so nothing else ever
        forces that repopulation on its own. `populate_existing` is the
        documented way to force it: only a locking read needs this (a caller
        deciding something based on "the current row" must not silently act on
        a stale in-memory copy); a plain, non-locking read is unaffected."""
        statement = self._select().where(ConversationAnalysis.conversation_id == conversation_id)
        if for_update:
            # `populate_existing()` is a `Query`-only method; `Select` (2.0-style,
            # used throughout this module) takes the same effect as an execution
            # option instead.
            statement = statement.with_for_update().execution_options(populate_existing=True)
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


class MemoryRepository(ScopedRepository[Memory]):
    model = Memory

    def get_for_update(self, memory_id: uuid.UUID) -> Memory | None:
        """Row-locking read used for status transitions, mirroring
        `ActionRepository.get_for_update`."""
        return self.session.scalars(
            self._select().where(Memory.id == memory_id).with_for_update()
        ).one_or_none()

    def get_active_for_analysis(
        self, source_analysis_id: uuid.UUID, *, kind: MemoryKind
    ) -> Sequence[Memory]:
        """Every currently-active memory of `kind` sourced from this analysis,
        strictly isolated to this repository's workspace (via `_select`) —
        the read `nerva_core.memory_rules.decide_memory_formation` is built
        from (see that module's docstring for why `kind=FACT` plus this
        analysis id is enough to mean "the estimated_value memory" in M6.3,
        and why that would stop being true for a second automated fact).

        `source_analysis_id` is `ConversationAnalysis.id` — stable across
        re-analyses of the same conversation, since that row is overwritten in
        place rather than appended (see `nerva.models.analysis`'s module
        docstring). It therefore identifies "memories derived from analyzing
        this conversation", not a specific analysis *run*'s content; that
        finer-grained identity is `Memory.source_fingerprint`, which a caller
        compares separately (idempotency against retries is a decision, not a
        query filter — see `nerva_core.memory_rules`).

        Returns a `Sequence`, not a single optional row: nothing in the schema
        enforces at most one active memory per `(source_analysis_id, kind)` —
        there is no unique constraint on `source_analysis_id` (see
        `nerva.models.memory`) — so a caller must not assume uniqueness the
        database does not. Upholding "at most one active estimated_value
        memory per source" is this rule layer's job going forward, not a
        guarantee this method can make by construction.
        """
        return self.session.scalars(
            self._select().where(
                Memory.source_analysis_id == source_analysis_id,
                Memory.kind == kind,
                Memory.status == MemoryStatus.ACTIVE,
            )
        ).all()

    def list_active(
        self,
        *,
        kind: MemoryKind | None,
        contact_id: uuid.UUID | None,
        company_id: uuid.UUID | None,
        opportunity_id: uuid.UUID | None,
        limit: int,
        offset: int,
    ) -> Sequence[Memory]:
        """The clear, dedicated way to see only what Nerva currently believes —
        `MemoryService.list`'s default policy goes through this, via `list_filtered`
        below, rather than every caller remembering to pass `status=active` itself."""
        return self.list_filtered(
            kind=kind,
            origin=None,
            status=MemoryStatus.ACTIVE,
            contact_id=contact_id,
            company_id=company_id,
            opportunity_id=opportunity_id,
            limit=limit,
            offset=offset,
        )

    def list_filtered(
        self,
        *,
        kind: MemoryKind | None,
        origin: MemoryOrigin | None,
        status: MemoryStatus | None,
        contact_id: uuid.UUID | None,
        company_id: uuid.UUID | None,
        opportunity_id: uuid.UUID | None,
        limit: int,
        offset: int,
    ) -> Sequence[Memory]:
        """General-purpose, explicitly-typed query underlying both `list_active` and
        `MemoryService.list`. `status=None` applies no status filter at all — the same
        convention as `ActionRepository.list_filtered`/`BusinessSignalRepository.
        list_filtered` — so a caller that genuinely needs every lifecycle state at once
        (e.g. a future audit view) is not blocked; the "active by default" policy that
        most callers want lives one layer up, in the service, not hidden in here.

        Stable order and limits come from `ScopedRepository._page`: newest (`created_at`)
        first, `id` as a deterministic tiebreaker, same as every other listing in this
        module.
        """
        statement = self._select()
        if kind is not None:
            statement = statement.where(Memory.kind == kind)
        if origin is not None:
            statement = statement.where(Memory.origin == origin)
        if status is not None:
            statement = statement.where(Memory.status == status)
        if contact_id is not None:
            statement = statement.where(Memory.contact_id == contact_id)
        if company_id is not None:
            statement = statement.where(Memory.company_id == company_id)
        if opportunity_id is not None:
            statement = statement.where(Memory.opportunity_id == opportunity_id)
        return self._page(statement, limit=limit, offset=offset)

    def list_candidates_for_context(
        self,
        *,
        contact_id: uuid.UUID | None,
        company_id: uuid.UUID | None,
        opportunity_id: uuid.UUID | None,
    ) -> Sequence[Memory]:
        """Every `ACTIVE` memory in this workspace that could possibly be
        relevant to an already-resolved subject chain — a workspace-level
        memory (no subject at all) plus any memory whose subject exactly
        matches one of `contact_id`/`company_id`/`opportunity_id` — expressed
        as a single workspace-scoped query (via `_select()`, which already
        filters by `workspace_id`) rather than loading every memory in the
        workspace and filtering in Python.

        Deliberately wider than any final selection, and deliberately not
        ranked here: `nerva_core.memory_context.select_context_memories`
        remains the sole authority on which of these candidates is actually
        used and in what order (including self-reference exclusion, which
        this method does not apply) — this only narrows what reaches it, by
        subject and status, never by relevance.

        Deliberately **no `limit`**: an earlier version of this method took
        one as a "safety ceiling" independent of a caller's `max_items`, but
        that was wrong — this method's `created_at` ordering does not match
        the selector's own ranking criteria (`scope`, then `origin`, then
        `observed_at`, then `id`; see `nerva_core.memory_context`), so any cap
        applied *before* the selector runs could silently drop a
        higher-priority candidate (e.g. an opportunity-scoped fact) in favor
        of a merely more recently-recorded, lower-priority one (e.g. an old
        workspace-level note) that the selector would otherwise have ranked
        below it or dropped itself. `select_context_memories` must see the
        *complete* eligible candidate set to be the authority its own module
        docstring claims it is — truncating upstream of it, for any reason,
        reintroduces exactly the bug this docstring is now warning against.
        See `nerva.services.memory_context`'s module docstring for the full
        argument and why no cap is reintroduced there either, for now.
        """
        no_subject = and_(
            Memory.contact_id.is_(None),
            Memory.company_id.is_(None),
            Memory.opportunity_id.is_(None),
        )
        subject_filters = [no_subject]
        if contact_id is not None:
            subject_filters.append(Memory.contact_id == contact_id)
        if company_id is not None:
            subject_filters.append(Memory.company_id == company_id)
        if opportunity_id is not None:
            subject_filters.append(Memory.opportunity_id == opportunity_id)

        statement = (
            self._select()
            .where(Memory.status == MemoryStatus.ACTIVE, or_(*subject_filters))
            .order_by(Memory.created_at.desc(), Memory.id)
        )
        return self.session.scalars(statement).all()


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

    def get_for_provider(self, provider: IntegrationProvider) -> Integration | None:
        """The workspace's most recently connected integration for `provider`.
        `send_email` has no `integration_id` of its own to resolve by (see
        `nerva_actions.payloads`'s module docstring) — this gives
        `nerva_connect.google.send_executor.GmailSendExecutor` the same single
        "the Google connection for this workspace" that the rest of Nerva Connect's
        UI already treats as the normal case; `ensure_connected_integration` is what
        actually decides whether the row it returns is usable."""
        return self.session.scalars(
            self._select()
            .where(Integration.provider == provider)
            .order_by(Integration.created_at.desc())
        ).first()


class OpportunityRepository(ScopedRepository[Opportunity]):
    model = Opportunity

    def list_filtered(
        self, *, stage: OpportunityStage | None, limit: int, offset: int
    ) -> Sequence[Opportunity]:
        statement = self._select()
        if stage is not None:
            statement = statement.where(Opportunity.stage == stage)
        return self._page(statement, limit=limit, offset=offset)

    def get_by_conversation(self, conversation_id: uuid.UUID) -> Sequence[Opportunity]:
        """Every `Opportunity` in this workspace whose `conversation_id`
        names this conversation as where it was identified. Returns a
        `Sequence`, not a single optional row, for the same reason
        `MemoryRepository.get_active_for_analysis` does: `Opportunity.
        conversation_id` (`nerva.models.crm`) carries no unique constraint —
        a human can link more than one `Opportunity` to the same conversation
        (e.g. two independent deals surfaced from the same thread, or two
        separate `POST /opportunities` calls naming the same conversation),
        so "the opportunity for this conversation" is not something this
        method, or the schema, can promise is singular. A caller that needs
        "exactly one, or none" must decide that for itself from this result,
        never assume it."""
        return self.session.scalars(
            self._select().where(Opportunity.conversation_id == conversation_id)
        ).all()


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

    def get_by_proposal_fingerprint(self, proposal_fingerprint: str) -> Action | None:
        """The automatic proposal with this exact identity in this workspace,
        if one exists — used by `services.proposal_materialization.
        ProposalMaterializationService` *after* an insert attempt's unique
        violation has already been rolled back to a savepoint (M7.1c), to
        hand the caller the existing row rather than just "it already
        exists." At most one row can ever match: `Action`'s partial unique
        index on `(workspace_id, proposal_fingerprint)` (see
        `nerva.models.actions.PROPOSAL_FINGERPRINT_UNIQUE_INDEX`) guarantees
        it at the database level — this method does not re-derive or assume
        that guarantee, it relies on it."""
        return self.session.scalars(
            self._select().where(Action.proposal_fingerprint == proposal_fingerprint)
        ).one_or_none()


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
