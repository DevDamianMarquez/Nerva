"""`ConversationRepository.list_needing_analysis`: the cheap pre-filter behind
the analysis backfill. This is NOT the authoritative idempotency check — that
stays `AnalysisService.analyze`'s fingerprint comparison — but a conversation
this pre-filter fails to select is never even offered to that check, so every
condition that should make a conversation eligible must be covered here:

1. no analysis at all
2. a previous analysis that is not `completed`
3. the conversation has a newer message than the analysis's `analyzed_at`
4. the completed analysis's `schema_version` is older than what is configured now
5. its `prompt_version` is older than what is configured now
6. its `model_id` is older than what is configured now

Never calls an AI provider — this is a database query over already-persisted rows.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from nerva.core.enums import AnalysisStatus, ConversationChannel
from nerva.models import Conversation, ConversationAnalysis, Workspace
from nerva.repositories.workspace_data import ConversationAnalysisRepository, ConversationRepository

CURRENT_SCHEMA_VERSION = 2
CURRENT_PROMPT_VERSION = "v2"
CURRENT_MODEL_ID = "gpt-4o-mini"

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _workspace(db_session: Session, name: str = "Acme") -> Workspace:
    workspace = Workspace(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    db_session.add(workspace)
    db_session.flush()
    return workspace


def _conversation(
    db_session: Session,
    workspace: Workspace,
    *,
    subject: str = "Pricing",
    last_message_at: datetime = NOW,
) -> Conversation:
    conversation = Conversation(
        workspace_id=workspace.id,
        channel=ConversationChannel.EMAIL,
        subject=subject,
        last_message_at=last_message_at,
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _add_analysis(
    db_session: Session,
    workspace: Workspace,
    conversation: Conversation,
    *,
    status: AnalysisStatus = AnalysisStatus.COMPLETED,
    schema_version: int = CURRENT_SCHEMA_VERSION,
    prompt_version: str = CURRENT_PROMPT_VERSION,
    model_id: str = CURRENT_MODEL_ID,
    analyzed_at: datetime = NOW,
) -> ConversationAnalysis:
    analysis = ConversationAnalysis(
        conversation_id=conversation.id,
        status=status,
        schema_version=schema_version,
        prompt_version=prompt_version,
        provider="openai",
        model_id=model_id,
        input_fingerprint="fp",
        analyzed_at=analyzed_at if status == AnalysisStatus.COMPLETED else None,
    )
    created = ConversationAnalysisRepository(db_session, workspace.id).add(analysis)
    db_session.flush()
    return created


def _list_needing_analysis(
    db_session: Session, workspace: Workspace, *, limit: int = 20
) -> list[Conversation]:
    repo = ConversationRepository(db_session, workspace.id)
    return list(
        repo.list_needing_analysis(
            limit=limit,
            current_schema_version=CURRENT_SCHEMA_VERSION,
            current_prompt_version=CURRENT_PROMPT_VERSION,
            current_model_id=CURRENT_MODEL_ID,
        )
    )


# --- Selected: needs (re-)analysis ----------------------------------------------


def test_a_conversation_with_no_analysis_is_selected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)

    assert _list_needing_analysis(db_session, workspace) == [conversation]


def test_a_failed_analysis_is_selected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _add_analysis(db_session, workspace, conversation, status=AnalysisStatus.FAILED)

    assert _list_needing_analysis(db_session, workspace) == [conversation]


def test_a_pending_non_completed_analysis_is_selected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _add_analysis(db_session, workspace, conversation, status=AnalysisStatus.PENDING)

    assert _list_needing_analysis(db_session, workspace) == [conversation]


def test_a_conversation_with_a_newer_message_than_its_analysis_is_selected(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace, last_message_at=NOW + timedelta(hours=1))
    _add_analysis(db_session, workspace, conversation, analyzed_at=NOW)

    assert _list_needing_analysis(db_session, workspace) == [conversation]


def test_a_completed_analysis_with_a_stale_schema_version_is_selected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _add_analysis(db_session, workspace, conversation, schema_version=CURRENT_SCHEMA_VERSION - 1)

    assert _list_needing_analysis(db_session, workspace) == [conversation]


def test_a_completed_analysis_with_a_stale_prompt_version_is_selected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _add_analysis(db_session, workspace, conversation, prompt_version="v1")

    assert _list_needing_analysis(db_session, workspace) == [conversation]


def test_a_completed_analysis_with_a_stale_model_id_is_selected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _add_analysis(db_session, workspace, conversation, model_id="gpt-4o-mini-old")

    assert _list_needing_analysis(db_session, workspace) == [conversation]


# --- Not selected: nothing has changed ------------------------------------------


def test_a_fully_current_completed_analysis_is_not_selected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace, last_message_at=NOW)
    _add_analysis(db_session, workspace, conversation, analyzed_at=NOW)

    assert _list_needing_analysis(db_session, workspace) == []


# --- Preserved behavior: bound, ordering, tenant isolation ----------------------


def test_the_limit_is_respected(db_session: Session) -> None:
    workspace = _workspace(db_session)
    for i in range(5):
        _conversation(db_session, workspace, subject=f"Conversation {i}")

    assert len(_list_needing_analysis(db_session, workspace, limit=2)) == 2


def test_most_recently_active_conversations_are_prioritized(db_session: Session) -> None:
    workspace = _workspace(db_session)
    older = _conversation(db_session, workspace, subject="Older", last_message_at=NOW)
    newer = _conversation(
        db_session, workspace, subject="Newer", last_message_at=NOW + timedelta(hours=1)
    )

    assert _list_needing_analysis(db_session, workspace) == [newer, older]


def test_one_workspaces_query_does_not_see_anothers_conversations(db_session: Session) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    _conversation(db_session, alice_workspace)

    assert _list_needing_analysis(db_session, bob_workspace) == []
