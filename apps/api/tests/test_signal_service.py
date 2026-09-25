"""`SignalService`: materialization idempotency, human confirmation/dismissal,
and re-analysis behavior — against a real database. No LLM call is involved;
completed analyses are constructed directly (see `_completed_analysis`), the
same way `test_conversation_analysis_persistence.py` builds rows directly.
One end-to-end test at the bottom instead runs the real `AnalysisService`
(with `FakeAIClient`) to prove the full Block 3 -> Block 4 pipeline works
together, not just each half in isolation."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import (
    ActivityEventType,
    AnalysisStatus,
    BusinessSignalStatus,
    BusinessSignalType,
    ConversationCategory,
    ConversationChannel,
    ConversationPriority,
    MemberRole,
    MessageDirection,
)
from nerva.core.errors import InvalidStateTransitionError, NotFoundError
from nerva.models import (
    BusinessSignal,
    Conversation,
    ConversationAnalysis,
    Message,
    User,
    Workspace,
)
from nerva.repositories.workspace_data import (
    BusinessSignalRepository,
    ConversationAnalysisRepository,
    OpportunityRepository,
)
from nerva.services.activity import ActivityService
from nerva.services.analysis import AnalysisService
from nerva.services.signals import SignalService
from tests.fakes.ai import FakeAIClient, make_analysis_result

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _workspace(db_session: Session, name: str = "Acme") -> Workspace:
    workspace = Workspace(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    db_session.add(workspace)
    db_session.flush()
    return workspace


def _conversation(
    db_session: Session, workspace: Workspace, subject: str = "Pricing for 3 seats"
) -> Conversation:
    conversation = Conversation(
        workspace_id=workspace.id, channel=ConversationChannel.EMAIL, subject=subject
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _completed_analysis(
    db_session: Session,
    workspace: Workspace,
    conversation: Conversation,
    *,
    category: ConversationCategory = ConversationCategory.SALES_LEAD,
    commercial_signal: bool = True,
    confidence: float = 0.9,
    summary: str = "Asked about pricing for 3 seats.",
    estimated_value_cents: int | None = None,
    value_currency: str | None = None,
    value_is_explicit: bool | None = None,
    fingerprint: str = "fp-1",
) -> ConversationAnalysis:
    repo = ConversationAnalysisRepository(db_session, workspace.id)
    analysis = repo.add(
        ConversationAnalysis(
            conversation_id=conversation.id,
            status=AnalysisStatus.COMPLETED,
            schema_version=1,
            prompt_version="v1",
            provider="fake",
            model_id="fake-model-1",
            input_fingerprint=fingerprint,
            category=category,
            priority=ConversationPriority.MEDIUM,
            requires_response=True,
            commercial_signal=commercial_signal,
            confidence=confidence,
            summary=summary,
            estimated_value_cents=estimated_value_cents,
            value_currency=value_currency,
            value_is_explicit=value_is_explicit,
            analyzed_at=NOW,
        )
    )
    db_session.flush()
    return analysis


def _user(db_session: Session, name: str = "Alex") -> User:
    # confirm/dismiss legitimately attribute to a real human, so their FK
    # targets (created_by_user_id, confirmed_by_user_id, ...) need a real row —
    # unlike AnalysisService/materialization, which only ever act as Nerva.
    email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, name=name, password_hash="x")
    db_session.add(user)
    db_session.flush()
    return user


MakeSignalService = Callable[[Workspace], SignalService]


@pytest.fixture
def make_service(db_session: Session) -> MakeSignalService:
    def _make(workspace: Workspace) -> SignalService:
        user = _user(db_session)
        ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
        return SignalService(db_session, ctx)

    return _make


# --- Materialization ------------------------------------------------------------


def test_a_qualifying_analysis_creates_a_pending_signal(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    signal = service.materialize_potential_opportunity(conversation.id)

    assert signal is not None
    assert signal.status == BusinessSignalStatus.PENDING
    assert signal.type == BusinessSignalType.POTENTIAL_OPPORTUNITY
    assert signal.conversation_id == conversation.id
    assert signal.analysis_id == analysis.id
    assert signal.source_fingerprint == analysis.input_fingerprint
    assert signal.data["title"] == analysis.summary
    assert signal.confirmed_at is None
    assert signal.dismissed_at is None


def test_a_non_qualifying_analysis_creates_no_signal(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation, commercial_signal=False)
    service = make_service(workspace)

    signal = service.materialize_potential_opportunity(conversation.id)

    assert signal is None
    assert BusinessSignalRepository(db_session, workspace.id).list() == []


def test_materializing_with_no_completed_analysis_returns_none(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    service = make_service(workspace)

    assert service.materialize_potential_opportunity(conversation.id) is None


def test_materializing_an_unknown_conversation_is_not_found(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)

    with pytest.raises(NotFoundError):
        service.materialize_potential_opportunity(uuid.uuid4())


def test_repeated_materialization_with_unchanged_analysis_is_idempotent(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    first = service.materialize_potential_opportunity(conversation.id)
    second = service.materialize_potential_opportunity(conversation.id)

    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert len(BusinessSignalRepository(db_session, workspace.id).list()) == 1


def test_an_activity_event_is_recorded_when_a_signal_is_created(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    signal = service.materialize_potential_opportunity(conversation.id)

    assert signal is not None
    events = ActivityService(db_session, workspace.id).list(entity_type="business_signal")
    assert len(events) == 1
    assert events[0].event_type == ActivityEventType.BUSINESS_SIGNAL_CREATED.value
    assert events[0].entity_id == signal.id


def test_materialization_is_isolated_by_workspace(
    db_session: Session, make_service: MakeSignalService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    _completed_analysis(db_session, bob_workspace, bob_conversation)
    alice_service = make_service(alice_workspace)

    with pytest.raises(NotFoundError):
        alice_service.materialize_potential_opportunity(bob_conversation.id)

    assert BusinessSignalRepository(db_session, bob_workspace.id).list() == []


def test_database_rejects_a_signal_referencing_a_conversation_in_another_workspace(
    db_session: Session,
) -> None:
    first, second = _workspace(db_session, "One"), _workspace(db_session, "Two")
    conversation_in_first = _conversation(db_session, first)
    analysis_in_first = _completed_analysis(db_session, first, conversation_in_first)

    db_session.add(
        BusinessSignal(
            workspace_id=second.id,  # mismatched on purpose
            conversation_id=conversation_in_first.id,
            analysis_id=analysis_in_first.id,
            type=BusinessSignalType.POTENTIAL_OPPORTUNITY,
            status=BusinessSignalStatus.PENDING,
            source_fingerprint="fp-1",
            data={},
        )
    )

    with pytest.raises(IntegrityError, match="fk_business_signals"):
        db_session.flush()


# --- Confirmation -----------------------------------------------------------------


def test_confirming_creates_exactly_one_opportunity_linked_to_the_conversation(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None

    opportunity = service.confirm(signal.id)

    assert opportunity.conversation_id == conversation.id
    assert len(OpportunityRepository(db_session, workspace.id).list()) == 1


def test_the_opportunity_is_attributed_to_the_confirming_human(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None

    opportunity = service.confirm(signal.id)

    assert opportunity.created_by_type.value == "user"
    assert opportunity.created_by_user_id == service.ctx.user_id


def test_explicit_estimated_value_is_copied_onto_the_opportunity(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(
        db_session,
        workspace,
        conversation,
        estimated_value_cents=150_000,
        value_currency="USD",
        value_is_explicit=True,
    )
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None

    opportunity = service.confirm(signal.id)

    assert opportunity.estimated_value_cents == 150_000
    assert opportunity.currency == "USD"


def test_a_missing_estimated_value_leaves_the_opportunity_value_unset(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)  # no estimated value
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None

    opportunity = service.confirm(signal.id)

    assert opportunity.estimated_value_cents is None


def test_confirming_updates_the_signal_and_records_activity_events(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None

    opportunity = service.confirm(signal.id)

    updated = BusinessSignalRepository(db_session, workspace.id).get(signal.id)
    assert updated is not None
    assert updated.status == BusinessSignalStatus.CONFIRMED
    assert updated.confirmed_by_user_id == service.ctx.user_id
    assert updated.confirmed_at is not None
    assert updated.created_opportunity_id == opportunity.id

    events = ActivityService(db_session, workspace.id).list()
    event_types = {event.event_type for event in events}
    assert ActivityEventType.OPPORTUNITY_CREATED.value in event_types
    assert ActivityEventType.BUSINESS_SIGNAL_CONFIRMED.value in event_types


def test_repeated_confirmation_cannot_create_a_second_opportunity(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None
    service.confirm(signal.id)

    with pytest.raises(InvalidStateTransitionError):
        service.confirm(signal.id)

    assert len(OpportunityRepository(db_session, workspace.id).list()) == 1


def test_confirming_a_signal_in_another_workspace_is_rejected(
    db_session: Session, make_service: MakeSignalService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    _completed_analysis(db_session, bob_workspace, bob_conversation)
    bob_service = make_service(bob_workspace)
    bob_signal = bob_service.materialize_potential_opportunity(bob_conversation.id)
    assert bob_signal is not None
    alice_service = make_service(alice_workspace)

    with pytest.raises(NotFoundError):
        alice_service.confirm(bob_signal.id)

    assert OpportunityRepository(db_session, alice_workspace.id).list() == []
    assert OpportunityRepository(db_session, bob_workspace.id).list() == []


# --- Dismissal ----------------------------------------------------------------------


def test_dismissing_creates_no_opportunity(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None

    dismissed = service.dismiss(signal.id)

    assert dismissed.status == BusinessSignalStatus.DISMISSED
    assert dismissed.dismissed_by_user_id == service.ctx.user_id
    assert dismissed.dismissed_at is not None
    assert OpportunityRepository(db_session, workspace.id).list() == []


def test_dismissing_records_an_activity_event(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None

    service.dismiss(signal.id)

    events = ActivityService(db_session, workspace.id).list(entity_type="business_signal")
    dismissed_type = ActivityEventType.BUSINESS_SIGNAL_DISMISSED.value
    dismissed_events = [e for e in events if e.event_type == dismissed_type]
    assert len(dismissed_events) == 1


def test_repeated_dismissal_is_rejected(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None
    service.dismiss(signal.id)

    with pytest.raises(InvalidStateTransitionError):
        service.dismiss(signal.id)


def test_dismissing_an_already_confirmed_signal_is_rejected(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None
    service.confirm(signal.id)

    with pytest.raises(InvalidStateTransitionError):
        service.dismiss(signal.id)


def test_dismissing_a_signal_in_another_workspace_is_rejected(
    db_session: Session, make_service: MakeSignalService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    _completed_analysis(db_session, bob_workspace, bob_conversation)
    bob_service = make_service(bob_workspace)
    bob_signal = bob_service.materialize_potential_opportunity(bob_conversation.id)
    assert bob_signal is not None
    alice_service = make_service(alice_workspace)

    with pytest.raises(NotFoundError):
        alice_service.dismiss(bob_signal.id)


# --- Re-analysis behavior -----------------------------------------------------------


def test_a_confirmed_signal_is_not_modified_by_a_later_reanalysis(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None
    opportunity = service.confirm(signal.id)

    # Simulate a later re-analysis producing different content.
    analysis.input_fingerprint = "fp-2"
    analysis.summary = "A completely different summary."
    db_session.flush()

    result = service.materialize_potential_opportunity(conversation.id)

    assert result is not None
    assert result.id == signal.id
    assert result.status == BusinessSignalStatus.CONFIRMED
    assert result.created_opportunity_id == opportunity.id
    assert len(OpportunityRepository(db_session, workspace.id).list()) == 1  # no second one


def test_same_fingerprint_reanalysis_after_dismissal_does_not_recreate_the_signal(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None
    service.dismiss(signal.id)

    result = service.materialize_potential_opportunity(conversation.id)

    assert result is not None
    assert result.id == signal.id
    assert result.status == BusinessSignalStatus.DISMISSED


def test_a_materially_different_reanalysis_after_dismissal_produces_a_fresh_pending_signal(
    db_session: Session, make_service: MakeSignalService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    signal = service.materialize_potential_opportunity(conversation.id)
    assert signal is not None
    service.dismiss(signal.id)

    # A genuinely new, still-qualifying analysis (new fingerprint = new content).
    analysis.input_fingerprint = "fp-2"
    analysis.summary = "A new pricing question came in."
    db_session.flush()

    result = service.materialize_potential_opportunity(conversation.id)

    assert result is not None
    assert result.id == signal.id  # the one row is reused, not duplicated
    assert result.status == BusinessSignalStatus.PENDING
    assert result.source_fingerprint == "fp-2"
    assert result.dismissed_at is None
    assert result.dismissed_by_user_id is None
    assert len(BusinessSignalRepository(db_session, workspace.id).list()) == 1


# --- End-to-end: the real AnalysisService feeding SignalService --------------------


def test_the_full_pipeline_from_a_real_analysis_to_a_confirmed_opportunity(
    db_session: Session, make_service: MakeSignalService
) -> None:
    """No email content in this file is ever printed — this only asserts on
    structured fields. Runs `AnalysisService` for real (against `FakeAIClient`,
    never a live provider) to prove Block 3's output is exactly what Block 4's
    rules expect, not just what a hand-built `ConversationAnalysis` row is."""
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    message = Message(
        workspace_id=workspace.id,
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        sender_email="priya@halcyon.example",
        sender_name="Priya Nair",
        recipients="alex@gmail.example",
        body_text="Can you send pricing for 3 seats?",
        sent_at=NOW,
        external_id=uuid.uuid4().hex,
    )
    db_session.add(message)
    db_session.flush()

    signal_service = make_service(workspace)
    analysis_service = AnalysisService(
        db_session,
        signal_service.ctx,
        ai_client=FakeAIClient(
            result=make_analysis_result(
                category=ConversationCategory.SALES_LEAD,
                commercial_signal=True,
                confidence=0.85,
                summary="Priya asked for pricing on 3 seats.",
            )
        ),
    )

    analysis_service.analyze(conversation.id)
    signal = signal_service.materialize_potential_opportunity(conversation.id)
    assert signal is not None
    opportunity = signal_service.confirm(signal.id)

    assert opportunity.conversation_id == conversation.id
    assert opportunity.created_by_type.value == "user"
    updated_signal = BusinessSignalRepository(db_session, workspace.id).get(signal.id)
    assert updated_signal is not None
    assert updated_signal.status == BusinessSignalStatus.CONFIRMED
    assert updated_signal.created_opportunity_id == opportunity.id
