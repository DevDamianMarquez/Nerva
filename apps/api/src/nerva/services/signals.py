"""Turns a completed `ConversationAnalysis` into a persisted `BusinessSignal`
via the deterministic rules in `nerva_core.rules`, and resolves a signal only
through an explicit human decision.

`materialize_potential_opportunity` is the only code path that creates or
updates a `BusinessSignal`; `confirm` is the only code path that creates the
`Opportunity` a signal represents, and it always attributes that Opportunity to
the authenticated human in `WorkspaceContext` — there is no parameter that lets
a caller attribute it to Nerva instead, which is what guarantees the LLM never
creates an Opportunity directly (see `nerva_core.rules`'s module docstring).
"""

import uuid

from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import (
    ActivityEventType,
    AnalysisStatus,
    BusinessSignalStatus,
    BusinessSignalType,
    OpportunityStage,
)
from nerva.core.errors import NotFoundError
from nerva.db.base import utcnow
from nerva.models import BusinessSignal, ConversationAnalysis, Opportunity
from nerva.nerva_core.rules import (
    AnalysisSummary,
    ExistingSignalState,
    MaterializationDecision,
    PotentialOpportunitySignal,
    decide_materialization,
    evaluate_potential_opportunity,
)
from nerva.nerva_core.signal_lifecycle import ensure_transition
from nerva.repositories.workspace_data import (
    BusinessSignalRepository,
    ContactRepository,
    ConversationAnalysisRepository,
    ConversationRepository,
    OpportunityRepository,
)
from nerva.services.activity import ActivityService


def _summary_from_analysis(analysis: ConversationAnalysis) -> AnalysisSummary:
    # Guaranteed non-None on a COMPLETED row: these are the required fields of
    # ConversationAnalysisResult (see nerva_ai.schemas), and
    # AnalysisService._persist_success always sets all of them together.
    assert analysis.category is not None
    assert analysis.commercial_signal is not None
    assert analysis.confidence is not None
    assert analysis.summary is not None
    return AnalysisSummary(
        category=analysis.category,
        commercial_signal=analysis.commercial_signal,
        confidence=analysis.confidence,
        summary=analysis.summary,
        estimated_value_cents=analysis.estimated_value_cents,
        value_currency=analysis.value_currency,
        value_is_explicit=analysis.value_is_explicit,
    )


def _signal_payload(signal: PotentialOpportunitySignal) -> dict[str, object]:
    return {
        "title": signal.title,
        "summary": signal.summary,
        "estimated_value_cents": signal.estimated_value_cents,
        "currency": signal.currency,
        "value_is_explicit": signal.value_is_explicit,
        "confidence": signal.confidence,
    }


class SignalService:
    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        self.session = session
        self.ctx = ctx
        self.conversations = ConversationRepository(session, ctx.workspace_id)
        self.contacts = ContactRepository(session, ctx.workspace_id)
        self.analyses = ConversationAnalysisRepository(session, ctx.workspace_id)
        self.signals = BusinessSignalRepository(session, ctx.workspace_id)
        self.opportunities = OpportunityRepository(session, ctx.workspace_id)
        self.activity = ActivityService(session, ctx.workspace_id)

    def get(self, signal_id: uuid.UUID) -> BusinessSignal:
        signal = self.signals.get(signal_id)
        if signal is None:
            raise NotFoundError("Signal not found.")
        return signal

    def materialize_potential_opportunity(
        self, conversation_id: uuid.UUID
    ) -> BusinessSignal | None:
        """Evaluates the conversation's current completed analysis against the
        potential-opportunity rule and creates, refreshes, or leaves alone the
        signal accordingly (see `nerva_core.rules.decide_materialization` for
        the full decision table). Returns the current signal, or `None` if
        none exists and none was created. Safe to call repeatedly."""
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.")

        analysis = self.analyses.get_for_conversation(conversation_id)
        if analysis is None or analysis.status != AnalysisStatus.COMPLETED:
            return None

        rule_result = evaluate_potential_opportunity(_summary_from_analysis(analysis))
        existing = self.signals.get_for_conversation_and_type(
            conversation_id, BusinessSignalType.POTENTIAL_OPPORTUNITY, for_update=True
        )
        existing_state = (
            ExistingSignalState(
                status=existing.status, source_fingerprint=existing.source_fingerprint
            )
            if existing is not None
            else None
        )
        decision = decide_materialization(
            has_qualifying_signal=rule_result is not None,
            existing=existing_state,
            current_fingerprint=analysis.input_fingerprint,
        )

        if decision == MaterializationDecision.NOOP:
            return existing
        assert rule_result is not None  # CREATE/REPLACE only ever follow a qualifying result

        if decision == MaterializationDecision.CREATE:
            signal = self.signals.add(
                BusinessSignal(
                    conversation_id=conversation_id,
                    analysis_id=analysis.id,
                    type=BusinessSignalType.POTENTIAL_OPPORTUNITY,
                    status=BusinessSignalStatus.PENDING,
                    source_fingerprint=analysis.input_fingerprint,
                    data=_signal_payload(rule_result),
                )
            )
        else:  # REPLACE: reuse the one row this conversation+type may ever have
            assert existing is not None
            signal = existing
            signal.status = BusinessSignalStatus.PENDING
            signal.analysis_id = analysis.id
            signal.source_fingerprint = analysis.input_fingerprint
            signal.data = _signal_payload(rule_result)
            signal.dismissed_by_user_id = None
            signal.dismissed_at = None

        self.activity.record(
            actor=Actor.nerva(),  # deterministic materialization, not a human decision
            event_type=ActivityEventType.BUSINESS_SIGNAL_CREATED,
            entity_type="business_signal",
            entity_id=signal.id,
            summary=(
                f"A potential opportunity was identified in "
                f'"{conversation.subject or "(no subject)"}".'
            ),
            data={"type": signal.type.value, "conversation_id": str(conversation_id)},
        )
        self.session.commit()
        return signal

    def confirm(self, signal_id: uuid.UUID) -> Opportunity:
        """Human-only: the confirming actor is always `self.ctx.actor` — a
        member from an authorized `WorkspaceContext`, never `Actor.nerva()`.
        Creates exactly one `Opportunity`, linked to the signal's conversation.
        Raises `NotFoundError` if the signal is not in this workspace, or
        `InvalidStateTransitionError` (409) if it is not `pending` — including
        on a repeated confirm, which therefore can never create a second
        Opportunity."""
        signal = self.signals.get_for_update(signal_id)
        if signal is None:
            raise NotFoundError("Signal not found.")
        ensure_transition(signal.status, BusinessSignalStatus.CONFIRMED)

        conversation = self.conversations.get(signal.conversation_id)
        assert conversation is not None  # guaranteed by the tenant_fk to conversations

        company_id = None
        if conversation.contact_id is not None:
            contact = self.contacts.get(conversation.contact_id)
            company_id = contact.company_id if contact is not None else None

        payload = signal.data
        opportunity = self.opportunities.add(
            Opportunity(
                title=str(payload.get("title") or "Untitled opportunity"),
                summary=payload.get("summary"),
                stage=OpportunityStage.NEW,
                estimated_value_cents=payload.get("estimated_value_cents"),
                currency=str(payload.get("currency") or "USD"),
                contact_id=conversation.contact_id,
                company_id=company_id,
                conversation_id=conversation.id,
                created_by_type=self.ctx.actor.type,
                created_by_user_id=self.ctx.actor.user_id,
            )
        )

        now = utcnow()
        signal.status = BusinessSignalStatus.CONFIRMED
        signal.confirmed_by_user_id = self.ctx.user_id
        signal.confirmed_at = now
        signal.created_opportunity_id = opportunity.id

        self.activity.record(
            actor=self.ctx.actor,
            event_type=ActivityEventType.OPPORTUNITY_CREATED,
            entity_type="opportunity",
            entity_id=opportunity.id,
            summary=f'Opportunity "{opportunity.title}" was created.',
            data={"stage": opportunity.stage.value, "source_signal_id": str(signal.id)},
        )
        self.activity.record(
            actor=self.ctx.actor,
            event_type=ActivityEventType.BUSINESS_SIGNAL_CONFIRMED,
            entity_type="business_signal",
            entity_id=signal.id,
            summary="A potential opportunity signal was confirmed.",
            data={"opportunity_id": str(opportunity.id)},
        )
        self.session.commit()
        return opportunity

    def dismiss(self, signal_id: uuid.UUID) -> BusinessSignal:
        """Human-only, mirroring `confirm`. Never creates an Opportunity.
        Raises `InvalidStateTransitionError` (409) on a signal that is not
        `pending`, including a repeated dismiss."""
        signal = self.signals.get_for_update(signal_id)
        if signal is None:
            raise NotFoundError("Signal not found.")
        ensure_transition(signal.status, BusinessSignalStatus.DISMISSED)

        signal.status = BusinessSignalStatus.DISMISSED
        signal.dismissed_by_user_id = self.ctx.user_id
        signal.dismissed_at = utcnow()

        self.activity.record(
            actor=self.ctx.actor,
            event_type=ActivityEventType.BUSINESS_SIGNAL_DISMISSED,
            entity_type="business_signal",
            entity_id=signal.id,
            summary="A potential opportunity signal was dismissed.",
            data={},
        )
        self.session.commit()
        return signal
