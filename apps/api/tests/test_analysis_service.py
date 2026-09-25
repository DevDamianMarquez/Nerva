"""`AnalysisService`: orchestration, idempotency/caching, failure handling and
the activity trail — against a real database and `FakeAIClient`. No real LLM
call is ever made; see `tests/fakes/ai.py`."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import (
    ActivityEventType,
    AnalysisStatus,
    ConversationCategory,
    ConversationChannel,
    ConversationPriority,
    MemberRole,
    MessageDirection,
)
from nerva.core.errors import NotFoundError
from nerva.models import Conversation, Message, Workspace
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from nerva.repositories.workspace_data import ConversationAnalysisRepository
from nerva.services.activity import ActivityService
from nerva.services.analysis import AnalysisAuthError, AnalysisProviderError, AnalysisService
from tests.fakes.ai import FakeAIClient, make_analysis_result

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _workspace(db_session: Session, name: str = "Acme") -> Workspace:
    workspace = Workspace(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    db_session.add(workspace)
    db_session.flush()
    return workspace


def _conversation(
    db_session: Session, workspace: Workspace, subject: str = "Pricing"
) -> Conversation:
    conversation = Conversation(
        workspace_id=workspace.id, channel=ConversationChannel.EMAIL, subject=subject
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _message(
    db_session: Session,
    conversation: Conversation,
    *,
    direction: MessageDirection = MessageDirection.INBOUND,
    sender_email: str = "priya@halcyon.example",
    sender_name: str | None = "Priya Nair",
    body_text: str = "Can you send pricing for 3 seats?",
    sent_at: datetime = NOW,
    external_id: str | None = None,
) -> Message:
    message = Message(
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        direction=direction,
        sender_email=sender_email,
        sender_name=sender_name,
        recipients="alex@gmail.example",
        body_text=body_text,
        sent_at=sent_at,
        external_id=external_id or uuid.uuid4().hex,
    )
    db_session.add(message)
    db_session.flush()
    return message


def _ctx(workspace: Workspace) -> WorkspaceContext:
    return WorkspaceContext(workspace_id=workspace.id, user_id=uuid.uuid4(), role=MemberRole.OWNER)


MakeAnalysis = Callable[[Workspace, FakeAIClient], AnalysisService]


@pytest.fixture
def make_service(db_session: Session) -> Callable[[Workspace, FakeAIClient], AnalysisService]:
    def _make(workspace: Workspace, ai_client: FakeAIClient) -> AnalysisService:
        return AnalysisService(db_session, _ctx(workspace), ai_client=ai_client)

    return _make


# --- Successful analysis --------------------------------------------------------


def test_a_successful_analysis_persists_a_completed_row(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient(
        result=make_analysis_result(
            category=ConversationCategory.SALES_LEAD,
            priority=ConversationPriority.HIGH,
            commercial_signal=True,
            requires_response=True,
            confidence=0.9,
        )
    )
    service = make_service(workspace, fake)

    analysis = service.analyze(conversation.id)

    assert analysis.status == AnalysisStatus.COMPLETED
    assert analysis.category == ConversationCategory.SALES_LEAD
    assert analysis.priority == ConversationPriority.HIGH
    assert analysis.commercial_signal is True
    assert analysis.confidence == 0.9
    assert analysis.attempts == 1
    assert analysis.provider == fake.provider
    assert analysis.model_id == fake.model_id
    assert analysis.input_tokens == 42
    assert analysis.output_tokens == 17
    assert analysis.latency_ms == 5
    assert analysis.analyzed_at is not None
    assert analysis.last_error is None
    assert fake.call_count == 1


def test_an_activity_event_is_recorded_for_a_successful_analysis(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace, subject="Pricing for 3 seats")
    _message(db_session, conversation)
    service = make_service(workspace, FakeAIClient())

    service.analyze(conversation.id)

    events = ActivityService(db_session, workspace.id).list(entity_type="conversation")
    assert len(events) == 1
    assert events[0].event_type == ActivityEventType.CONVERSATION_ANALYZED.value
    assert events[0].entity_id == conversation.id
    assert events[0].actor_type.value == "nerva"
    assert events[0].actor_user_id is None
    assert "Pricing for 3 seats" in events[0].summary


def test_analyzing_an_unknown_or_cross_workspace_conversation_is_not_found(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    _message(db_session, bob_conversation)
    service = make_service(alice_workspace, FakeAIClient())

    with pytest.raises(NotFoundError):
        service.analyze(bob_conversation.id)

    with pytest.raises(NotFoundError):
        service.analyze(uuid.uuid4())


def test_no_analysis_row_is_created_in_the_foreign_workspace_by_a_failed_lookup(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    _message(db_session, bob_conversation)
    service = make_service(alice_workspace, FakeAIClient())

    with pytest.raises(NotFoundError):
        service.analyze(bob_conversation.id)

    assert ConversationAnalysisRepository(db_session, bob_workspace.id).list() == []


# --- Idempotency / caching -------------------------------------------------------


def test_reanalyzing_unchanged_content_makes_zero_additional_provider_calls(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    service = make_service(workspace, fake)

    first = service.analyze(conversation.id)
    second = service.analyze(conversation.id)

    assert fake.call_count == 1
    assert second.id == first.id
    assert second.analyzed_at == first.analyzed_at


def test_force_true_bypasses_the_cache_and_calls_the_provider_again(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    service = make_service(workspace, fake)
    first = service.analyze(conversation.id)

    second = service.analyze(conversation.id, force=True)

    assert fake.call_count == 2
    assert second.id == first.id  # still the one current row, not a duplicate


def test_a_new_message_arriving_triggers_reanalysis(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation, body_text="Can you send pricing?")
    fake = FakeAIClient()
    service = make_service(workspace, fake)
    first = service.analyze(conversation.id)
    first_id, first_fingerprint = first.id, first.input_fingerprint

    _message(
        db_session,
        conversation,
        direction=MessageDirection.OUTBOUND,
        body_text="Sure, here it is.",
        sent_at=NOW,
    )
    second = service.analyze(conversation.id)

    assert fake.call_count == 2
    assert second.id == first_id
    assert second.input_fingerprint != first_fingerprint


def test_a_changed_model_identifier_triggers_reanalysis(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    service = make_service(workspace, fake)
    first = service.analyze(conversation.id)
    first_fingerprint = first.input_fingerprint

    fake.model_id = "fake-model-2"
    second = service.analyze(conversation.id)

    assert fake.call_count == 2
    assert second.model_id == "fake-model-2"
    assert second.input_fingerprint != first_fingerprint


def test_calling_analyze_repeatedly_never_creates_a_second_row(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    service = make_service(workspace, fake)

    service.analyze(conversation.id)
    service.analyze(conversation.id)  # cache hit
    service.analyze(conversation.id, force=True)  # forced re-run

    assert len(ConversationAnalysisRepository(db_session, workspace.id).list()) == 1


# --- Uncertainty fields persist through the service -----------------------------


def test_nullable_uncertain_fields_persist_as_null_through_the_service(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    # make_analysis_result() defaults leave every uncertain field unset.
    service = make_service(workspace, FakeAIClient())

    analysis = service.analyze(conversation.id)

    assert analysis.intent is None
    assert analysis.sentiment is None
    assert analysis.estimated_value_cents is None
    assert analysis.value_currency is None
    assert analysis.value_is_explicit is None
    assert analysis.deadline_at is None
    assert analysis.deadline_is_explicit is None
    assert analysis.suggested_next_step is None
    assert analysis.mentioned_people == []
    assert analysis.mentioned_companies == []


# --- Failure handling -------------------------------------------------------------


def test_a_provider_failure_persists_a_coherent_failed_state(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    fake.fail_with = AIProviderError("connection reset")
    service = make_service(workspace, fake)

    with pytest.raises(AnalysisProviderError):
        service.analyze(conversation.id)

    analysis = ConversationAnalysisRepository(db_session, workspace.id).get_for_conversation(
        conversation.id
    )
    assert analysis is not None
    assert analysis.status == AnalysisStatus.FAILED
    assert analysis.attempts == 1
    assert analysis.last_error == "Could not reach the AI provider."
    assert analysis.category is None
    assert analysis.analyzed_at is None


def test_a_failed_analysis_can_be_retried_successfully(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    fake.fail_with = AIProviderError("connection reset")
    service = make_service(workspace, fake)
    with pytest.raises(AnalysisProviderError):
        service.analyze(conversation.id)

    fake.fail_with = None
    analysis = service.analyze(conversation.id)

    assert analysis.status == AnalysisStatus.COMPLETED
    assert analysis.attempts == 2  # the failed attempt counts too
    assert analysis.last_error is None
    assert len(ConversationAnalysisRepository(db_session, workspace.id).list()) == 1


def test_attempts_increment_across_repeated_failures(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    fake.fail_with = AIProviderError("connection reset")
    service = make_service(workspace, fake)

    for _ in range(2):
        with pytest.raises(AnalysisProviderError):
            service.analyze(conversation.id)

    analysis = ConversationAnalysisRepository(db_session, workspace.id).get_for_conversation(
        conversation.id
    )
    assert analysis is not None
    assert analysis.attempts == 2


def test_a_retry_after_success_preserves_the_prior_result_if_it_fails_again(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    """A re-analysis attempt that fails must not erase a previously successful
    result: stale-but-real is better than silently discarded."""
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient(result=make_analysis_result(category=ConversationCategory.SALES_LEAD))
    service = make_service(workspace, fake)
    service.analyze(conversation.id)

    fake.fail_with = AIProviderError("connection reset")
    with pytest.raises(AnalysisProviderError):
        service.analyze(conversation.id, force=True)

    analysis = ConversationAnalysisRepository(db_session, workspace.id).get_for_conversation(
        conversation.id
    )
    assert analysis is not None
    assert analysis.status == AnalysisStatus.FAILED
    assert analysis.category == ConversationCategory.SALES_LEAD  # preserved, not wiped


@pytest.mark.parametrize(
    ("error", "expected_message", "expect_auth_error"),
    [
        (AIAuthError("bad key"), "The AI provider rejected our credentials.", True),
        (
            AIRateLimitError("slow down", retry_after=30),
            "The AI provider is rate-limiting requests.",
            False,
        ),
        (AIProviderError("network blip"), "Could not reach the AI provider.", False),
        (
            AIOutputValidationError("bad json"),
            "The AI provider's output did not match the expected schema.",
            False,
        ),
    ],
)
def test_each_failure_type_maps_to_a_fixed_safe_message(
    db_session: Session,
    make_service: MakeAnalysis,
    error: AIError,
    expected_message: str,
    expect_auth_error: bool,
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _message(db_session, conversation)
    fake = FakeAIClient()
    fake.fail_with = error
    service = make_service(workspace, fake)

    expected_exc = AnalysisAuthError if expect_auth_error else AnalysisProviderError
    with pytest.raises(expected_exc):
        service.analyze(conversation.id)

    analysis = ConversationAnalysisRepository(db_session, workspace.id).get_for_conversation(
        conversation.id
    )
    assert analysis is not None
    assert analysis.last_error == expected_message


def test_no_private_message_content_reaches_the_safe_error_field(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    secret = "SuperSecretProjectCodename123"
    _message(db_session, conversation, body_text=f"Confidential: {secret}")
    fake = FakeAIClient()
    # Worst case: the raised error's own message happens to echo something
    # sensitive-looking (e.g. a provider quoting back part of the request in an
    # error body). The service must never persist the exception's own text.
    fake.fail_with = AIProviderError(f"timeout while processing: {secret}")
    service = make_service(workspace, fake)

    with pytest.raises(AnalysisProviderError):
        service.analyze(conversation.id)

    analysis = ConversationAnalysisRepository(db_session, workspace.id).get_for_conversation(
        conversation.id
    )
    assert analysis is not None
    assert secret not in (analysis.last_error or "")
    assert analysis.last_error == "Could not reach the AI provider."


def test_a_failed_analysis_activity_event_is_recorded_with_only_safe_data(
    db_session: Session, make_service: MakeAnalysis
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace, subject="Pricing for 3 seats")
    _message(db_session, conversation)
    fake = FakeAIClient()
    fake.fail_with = AIProviderError("connection reset")
    service = make_service(workspace, fake)

    with pytest.raises(AnalysisProviderError):
        service.analyze(conversation.id)

    events = ActivityService(db_session, workspace.id).list(entity_type="conversation")
    assert len(events) == 1
    assert events[0].event_type == ActivityEventType.CONVERSATION_ANALYSIS_FAILED.value
    assert events[0].entity_id == conversation.id
    assert events[0].data == {"error": "Could not reach the AI provider.", "attempts": 1}
    assert "connection reset" not in str(events[0].data)
