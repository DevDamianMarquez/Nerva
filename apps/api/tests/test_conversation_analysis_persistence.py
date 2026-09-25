"""`ConversationAnalysis` persistence: the model and repository only.

No `AnalysisService`, no provider calls, and no API routes exist yet (Milestone 4
Block 2 is persistence-only) — these tests talk to the database directly, the
same way `TestDatabaseEnforcesTenantBoundaries` in `test_workspace_isolation.py`
tests the composite-foreign-key layer for existing tables.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.enums import (
    AnalysisStatus,
    ConversationCategory,
    ConversationChannel,
    ConversationPriority,
    SentimentLabel,
)
from nerva.models import Conversation, ConversationAnalysis, Workspace
from nerva.repositories.workspace_data import ConversationAnalysisRepository


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


def _pending_analysis(
    *, conversation_id: uuid.UUID, fingerprint: str = "fingerprint-a"
) -> ConversationAnalysis:
    """A minimal, valid row — as if a service had just claimed it before ever
    calling a provider. `workspace_id` is deliberately omitted: `ScopedRepository.add`
    stamps it, the same as every other repository."""
    return ConversationAnalysis(
        conversation_id=conversation_id,
        status=AnalysisStatus.PENDING,
        schema_version=1,
        prompt_version="v1",
        provider="fake",
        model_id="fake-model-1",
        input_fingerprint=fingerprint,
    )


# --- Create / read / update --------------------------------------------------


def test_create_and_read_a_pending_analysis(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)

    created = repo.add(_pending_analysis(conversation_id=conversation.id))
    db_session.flush()

    found = repo.get_for_conversation(conversation.id)
    assert found is not None
    assert found.id == created.id
    assert found.workspace_id == workspace.id
    assert found.status == AnalysisStatus.PENDING
    assert found.attempts == 0  # column default
    assert found.mentioned_people == []  # column default
    assert found.mentioned_companies == []


def test_get_for_conversation_returns_none_when_nothing_has_been_analyzed(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)

    assert repo.get_for_conversation(conversation.id) is None


def test_updating_the_existing_row_in_place_persists_and_stays_a_single_row(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)
    created = repo.add(_pending_analysis(conversation_id=conversation.id))
    db_session.flush()

    fetched = repo.get_for_conversation(conversation.id)
    assert fetched is not None
    fetched.status = AnalysisStatus.COMPLETED
    fetched.category = ConversationCategory.SALES_LEAD
    fetched.confidence = 0.9
    fetched.analyzed_at = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    db_session.flush()

    reread = repo.get_for_conversation(conversation.id)
    assert reread is not None
    assert reread.id == created.id  # same row, not a new one
    assert reread.status == AnalysisStatus.COMPLETED
    assert reread.category == ConversationCategory.SALES_LEAD
    assert reread.confidence == 0.9
    assert len(repo.list()) == 1


def test_get_for_conversation_for_update_locks_without_changing_the_result(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)
    repo.add(_pending_analysis(conversation_id=conversation.id))
    db_session.flush()

    locked = repo.get_for_conversation(conversation.id, for_update=True)

    assert locked is not None
    assert locked.status == AnalysisStatus.PENDING


# --- Uniqueness: at most one current analysis per conversation ---------------


def test_a_second_row_for_the_same_conversation_is_rejected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)
    repo.add(_pending_analysis(conversation_id=conversation.id, fingerprint="first"))
    db_session.flush()
    unique_constraint = "uq_conversation_analyses_workspace_id_conversation_id"

    with pytest.raises(IntegrityError, match=unique_constraint):
        repo.add(_pending_analysis(conversation_id=conversation.id, fingerprint="second"))


# --- Tenant isolation ---------------------------------------------------------


def test_one_workspaces_repository_cannot_see_anothers_analysis(db_session: Session) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    alice_conversation = _conversation(db_session, alice_workspace)
    alice_repo = ConversationAnalysisRepository(db_session, alice_workspace.id)
    bob_repo = ConversationAnalysisRepository(db_session, bob_workspace.id)
    alice_repo.add(_pending_analysis(conversation_id=alice_conversation.id))
    db_session.flush()

    assert bob_repo.get_for_conversation(alice_conversation.id) is None
    assert bob_repo.list() == []
    assert len(alice_repo.list()) == 1


def test_cross_workspace_conversation_reference_is_rejected_by_the_database(
    db_session: Session,
) -> None:
    """Defense in depth: even code that skipped the repository entirely and built
    the row by hand cannot link an analysis to another workspace's conversation —
    the composite foreign key rejects it in PostgreSQL itself, mirroring
    `TestDatabaseEnforcesTenantBoundaries` for the existing tables."""
    first, second = _workspace(db_session, "One"), _workspace(db_session, "Two")
    conversation_in_first = _conversation(db_session, first)

    db_session.add(
        ConversationAnalysis(
            workspace_id=second.id,  # mismatched on purpose
            conversation_id=conversation_in_first.id,
            status=AnalysisStatus.PENDING,
            schema_version=1,
            prompt_version="v1",
            provider="fake",
            model_id="fake-model-1",
            input_fingerprint="fp",
        )
    )

    with pytest.raises(IntegrityError, match="fk_conversation_analyses"):
        db_session.flush()


def test_a_random_conversation_id_is_rejected(db_session: Session) -> None:
    workspace = _workspace(db_session)

    db_session.add(
        ConversationAnalysis(
            workspace_id=workspace.id,
            conversation_id=uuid.uuid4(),
            status=AnalysisStatus.PENDING,
            schema_version=1,
            prompt_version="v1",
            provider="fake",
            model_id="fake-model-1",
            input_fingerprint="fp",
        )
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


# --- Uncertainty: nullable result fields --------------------------------------


def test_uncertainty_fields_can_be_persisted_as_null(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)

    analysis = _pending_analysis(conversation_id=conversation.id)
    analysis.status = AnalysisStatus.COMPLETED
    analysis.category = ConversationCategory.OTHER
    analysis.priority = ConversationPriority.LOW
    analysis.requires_response = False
    analysis.commercial_signal = False
    analysis.confidence = 0.4
    # Everything the model may decline to fill in stays unset.
    repo.add(analysis)
    db_session.flush()

    found = repo.get_for_conversation(conversation.id)
    assert found is not None
    assert found.intent is None
    assert found.sentiment is None
    assert found.estimated_value_cents is None
    assert found.value_currency is None
    assert found.value_is_explicit is None
    assert found.deadline_at is None
    assert found.deadline_is_explicit is None
    assert found.suggested_next_step is None
    assert found.mentioned_people == []
    assert found.mentioned_companies == []


# --- Completed / failed metadata round trip -----------------------------------


def test_a_fully_populated_completed_analysis_round_trips_exactly(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)
    analyzed_at = datetime(2026, 1, 15, 12, 30, tzinfo=UTC)

    repo.add(
        ConversationAnalysis(
            conversation_id=conversation.id,
            status=AnalysisStatus.COMPLETED,
            attempts=1,
            last_attempted_at=analyzed_at,
            schema_version=1,
            prompt_version="v1",
            provider="anthropic",
            model_id="claude-sonnet-5",
            input_fingerprint="fp-completed",
            category=ConversationCategory.SALES_LEAD,
            priority=ConversationPriority.HIGH,
            sentiment=SentimentLabel.POSITIVE,
            requires_response=True,
            commercial_signal=True,
            confidence=0.87,
            estimated_value_cents=150_000,
            value_currency="USD",
            value_is_explicit=True,
            deadline_at=date(2026, 6, 5),
            deadline_is_explicit=False,
            summary="Asked about pricing for 3 seats.",
            intent="request_quote",
            suggested_next_step="Send a quote for 3 seats.",
            mentioned_people=["Priya Nair"],
            mentioned_companies=["Halcyon Freight"],
            input_tokens=512,
            output_tokens=128,
            latency_ms=940,
            analyzed_at=analyzed_at,
        )
    )
    db_session.flush()

    found = repo.get_for_conversation(conversation.id)
    assert found is not None
    assert found.status == AnalysisStatus.COMPLETED
    assert found.attempts == 1
    assert found.provider == "anthropic"
    assert found.model_id == "claude-sonnet-5"
    assert found.input_fingerprint == "fp-completed"
    assert found.category == ConversationCategory.SALES_LEAD
    assert found.priority == ConversationPriority.HIGH
    assert found.sentiment == SentimentLabel.POSITIVE
    assert found.requires_response is True
    assert found.commercial_signal is True
    assert found.confidence == 0.87
    assert found.estimated_value_cents == 150_000
    assert found.value_currency == "USD"
    assert found.value_is_explicit is True
    assert found.deadline_at == date(2026, 6, 5)
    assert found.deadline_is_explicit is False
    assert found.summary == "Asked about pricing for 3 seats."
    assert found.intent == "request_quote"
    assert found.suggested_next_step == "Send a quote for 3 seats."
    assert found.mentioned_people == ["Priya Nair"]
    assert found.mentioned_companies == ["Halcyon Freight"]
    assert found.input_tokens == 512
    assert found.output_tokens == 128
    assert found.latency_ms == 940
    assert found.analyzed_at == analyzed_at
    assert found.last_error is None


def test_a_failed_analysis_persists_a_safe_error_and_never_fabricates_a_result(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)

    repo.add(
        ConversationAnalysis(
            conversation_id=conversation.id,
            status=AnalysisStatus.FAILED,
            attempts=2,
            last_attempted_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            last_error="Could not reach the AI provider.",
            schema_version=1,
            prompt_version="v1",
            provider="anthropic",
            model_id="claude-sonnet-5",
            input_fingerprint="fp-failed",
        )
    )
    db_session.flush()

    found = repo.get_for_conversation(conversation.id)
    assert found is not None
    assert found.status == AnalysisStatus.FAILED
    assert found.attempts == 2
    assert found.last_error == "Could not reach the AI provider."
    # A failed attempt never claims a result it does not have.
    assert found.category is None
    assert found.confidence is None
    assert found.analyzed_at is None


# --- Cascade behavior ----------------------------------------------------------


def test_deleting_the_conversation_cascades_to_its_analysis(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    repo = ConversationAnalysisRepository(db_session, workspace.id)
    repo.add(_pending_analysis(conversation_id=conversation.id))
    db_session.flush()

    db_session.delete(conversation)
    db_session.flush()

    assert repo.get_for_conversation(conversation.id) is None
    assert repo.list() == []
