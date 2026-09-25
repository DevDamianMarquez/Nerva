"""API surface for AI conversation analysis: analyze one conversation, the
bounded owner-only backfill, and the "needs attention" query. Uses
`FakeAIClient` exclusively via `app.dependency_overrides` (see `conftest.py`'s
`ai_client` fixture) — no test here makes a real network call.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.enums import (
    ConversationChannel,
    ConversationPriority,
    MemberRole,
    MessageDirection,
)
from nerva.models import Conversation, Message, WorkspaceMember
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from tests.fakes.ai import FakeAIClient, make_analysis_result
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _add_member(db_session: Session, workspace_id: str, user: Account) -> None:
    """A minimal way to add a second member without an invitation flow (not
    yet implemented) — mirrors `test_integrations_management.py`."""
    db_session.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(workspace_id), user_id=uuid.UUID(user.id), role=MemberRole.MEMBER
        )
    )
    db_session.commit()


def _conversation(db_session: Session, workspace_id: str, subject: str = "Pricing") -> str:
    conversation = Conversation(
        workspace_id=uuid.UUID(workspace_id), channel=ConversationChannel.EMAIL, subject=subject
    )
    db_session.add(conversation)
    db_session.commit()
    return str(conversation.id)


def _message(
    db_session: Session,
    conversation_id: str,
    workspace_id: str,
    *,
    body_text: str = "Can you send pricing for 3 seats?",
    sent_at: datetime = NOW,
) -> None:
    message = Message(
        workspace_id=uuid.UUID(workspace_id),
        conversation_id=uuid.UUID(conversation_id),
        direction=MessageDirection.INBOUND,
        sender_email="priya@halcyon.example",
        sender_name="Priya Nair",
        recipients="alex@gmail.example",
        body_text=body_text,
        sent_at=sent_at,
        external_id=uuid.uuid4().hex,
    )
    db_session.add(message)
    db_session.commit()


# --- Analyze -----------------------------------------------------------------------


def test_analyzing_a_conversation_returns_the_completed_result(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(commercial_signal=True, confidence=0.9)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["commercial_signal"] is True
    assert body["confidence"] == 0.9
    assert ai_client.call_count == 1


def test_a_non_owner_member_can_also_trigger_analysis(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    """Unlike Gmail connect/sync, analysis touches no external credentials, so
    it is not owner-restricted."""
    owner, member = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    _add_member(db_session, workspace_id, member)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=member.cookies,
    )

    assert response.status_code == 200, response.text


def test_reanalyzing_unchanged_content_costs_no_extra_provider_call(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    url = f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze"

    first = client.post(url, cookies=owner.cookies)
    second = client.post(url, cookies=owner.cookies)

    assert first.status_code == second.status_code == 200
    assert ai_client.call_count == 1
    assert first.json()["id"] == second.json()["id"]


def test_the_conversation_detail_embeds_the_current_analysis(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    detail_url = f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}"

    before = client.get(detail_url, cookies=owner.cookies).json()
    assert before["analysis"] is None

    analyze_response = client.post(f"{detail_url}/analyze", cookies=owner.cookies)
    assert analyze_response.status_code == 200, analyze_response.text
    after_response = client.get(detail_url, cookies=owner.cookies)
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()

    assert after["analysis"] is not None
    assert after["analysis"]["status"] == "completed"


def test_analyzing_a_conversation_in_another_workspace_is_not_found(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    alice_conversation = _conversation(db_session, alice_workspace)
    _message(db_session, alice_conversation, alice_workspace)

    response = client.post(
        f"/v1/workspaces/{bob_workspace}/conversations/{alice_conversation}/analyze",
        cookies=bob.cookies,
    )

    assert response.status_code == 404


def test_a_non_member_cannot_analyze_in_a_workspace_they_do_not_belong_to(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
) -> None:
    alice, mallory = make_user("Alice"), make_user("Mallory")
    workspace_id = make_workspace(alice)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=mallory.cookies,
    )

    assert response.status_code == 404


def test_an_unauthenticated_request_is_rejected(
    client: TestClient, db_session: Session, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    response = client.post(f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze")

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (AIAuthError("bad key"), 503, "ai_not_configured"),
        (AIRateLimitError("slow down", retry_after=30), 429, "rate_limited"),
        (AIOutputValidationError("bad json"), 503, "invalid_ai_output"),
        (AIProviderError("connection reset"), 503, "ai_unavailable"),
    ],
)
def test_provider_failures_map_to_safe_responses(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
    error: AIError,
    expected_status: int,
    expected_code: str,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(
        db_session, conversation_id, workspace_id, body_text="A message with SECRET-XYZ inside."
    )
    ai_client.fail_with = error

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == expected_status
    body = response.json()
    assert body["error"]["details"] == {"code": expected_code}
    assert "SECRET-XYZ" not in response.text
    assert "connection reset" not in response.text
    assert "bad json" not in response.text
    assert "bad key" not in response.text
    assert "slow down" not in response.text


# --- Backfill ------------------------------------------------------------------------


def test_owner_can_run_a_bounded_backfill(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    for i in range(3):
        conversation_id = _conversation(db_session, workspace_id, subject=f"Conversation {i}")
        _message(db_session, conversation_id, workspace_id, body_text=f"message {i}")

    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"
    response = client.post(backfill_url, cookies=owner.cookies)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"considered": 3, "analyzed": 3, "cached": 0, "failed": 0, "signals_created": 0}
    assert ai_client.call_count == 3


def test_a_non_owner_member_cannot_run_backfill(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner, member = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    _add_member(db_session, workspace_id, member)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"
    response = client.post(backfill_url, cookies=member.cookies)

    assert response.status_code == 403
    assert ai_client.call_count == 0


def test_the_batch_size_maximum_is_enforced(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/analysis/backfill?batch_size=21", cookies=owner.cookies
    )

    assert response.status_code == 422


def test_one_failed_conversation_does_not_abort_the_batch(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    for i in range(3):
        conversation_id = _conversation(db_session, workspace_id, subject=f"Conversation {i}")
        _message(db_session, conversation_id, workspace_id, body_text=f"message {i}")
    ai_client.fail_for_subjects = {"Conversation 1"}

    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"
    response = client.post(backfill_url, cookies=owner.cookies)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["considered"] == 3
    assert body["analyzed"] == 2
    assert body["failed"] == 1
    assert ai_client.call_count == 3  # the failure did not stop the remaining conversations


def test_repeated_backfill_does_not_unnecessarily_call_the_provider_again(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    url = f"/v1/workspaces/{workspace_id}/analysis/backfill"

    first = client.post(url, cookies=owner.cookies)
    second = client.post(url, cookies=owner.cookies)

    assert first.json()["analyzed"] == 1
    assert ai_client.call_count == 1  # the pre-filter already excludes the up-to-date conversation
    assert second.json() == {
        "considered": 0,
        "analyzed": 0,
        "cached": 0,
        "failed": 0,
        "signals_created": 0,
    }


def test_a_stale_model_id_makes_a_completed_conversation_eligible_for_backfill_again(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    """A conversation whose content has not changed at all must still be
    re-offered to `AnalysisService` once the configured model changes — the
    pre-filter's job is to not silently skip that, even though this particular
    scenario also happens to trigger a real provider call (the fingerprint,
    which folds in `model_id`, is genuinely different)."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    url = f"/v1/workspaces/{workspace_id}/analysis/backfill"

    first = client.post(url, cookies=owner.cookies)
    ai_client.model_id = "fake-model-2"
    second = client.post(url, cookies=owner.cookies)

    assert first.json()["analyzed"] == 1
    assert second.json()["considered"] == 1
    assert second.json()["analyzed"] == 1
    assert ai_client.call_count == 2


# --- Needs attention -------------------------------------------------------------------


def test_attention_lists_high_priority_and_requires_response_conversations(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    urgent_conversation = _conversation(db_session, workspace_id, subject="Server is down")
    _message(db_session, urgent_conversation, workspace_id, body_text="Urgent issue")
    quiet_conversation = _conversation(db_session, workspace_id, subject="Newsletter")
    _message(
        db_session,
        quiet_conversation,
        workspace_id,
        body_text="Just a newsletter",
        sent_at=NOW + timedelta(minutes=5),
    )

    ai_client.next_result = make_analysis_result(
        priority=ConversationPriority.URGENT, requires_response=True
    )
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{urgent_conversation}/analyze",
        cookies=owner.cookies,
    )
    ai_client.next_result = make_analysis_result(
        priority=ConversationPriority.LOW, requires_response=False
    )
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{quiet_conversation}/analyze",
        cookies=owner.cookies,
    )

    response = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/attention", cookies=owner.cookies
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["conversation_id"] for row in body] == [urgent_conversation]
    assert body[0]["priority"] == "urgent"
    assert body[0]["requires_response"] is True


def test_conversations_without_a_qualifying_analysis_are_not_surfaced(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    unanalyzed = _conversation(db_session, workspace_id, subject="Never analyzed")
    _message(db_session, unanalyzed, workspace_id)
    low_priority = _conversation(db_session, workspace_id, subject="Low priority")
    _message(db_session, low_priority, workspace_id, sent_at=NOW + timedelta(minutes=5))
    ai_client.next_result = make_analysis_result(
        priority=ConversationPriority.LOW, requires_response=False
    )
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{low_priority}/analyze", cookies=owner.cookies
    )

    response = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/attention", cookies=owner.cookies
    )

    assert response.json() == []
