"""API surface for business signals: list/read, and the two human-only
decisions — confirm and dismiss. Uses `FakeAIClient` to produce a qualifying
analysis; no test here makes a real network call.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.enums import (
    ConversationCategory,
    ConversationChannel,
    MemberRole,
    MessageDirection,
)
from nerva.models import Conversation, Message, WorkspaceMember
from tests.fakes.ai import FakeAIClient, make_analysis_result
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

QUALIFYING_RESULT = make_analysis_result(
    category=ConversationCategory.SALES_LEAD,
    commercial_signal=True,
    confidence=0.9,
    summary="Asked about pricing for 3 seats.",
)


def _add_member(db_session: Session, workspace_id: str, user: Account) -> None:
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


def _message(db_session: Session, conversation_id: str, workspace_id: str) -> None:
    message = Message(
        workspace_id=uuid.UUID(workspace_id),
        conversation_id=uuid.UUID(conversation_id),
        direction=MessageDirection.INBOUND,
        sender_email="priya@halcyon.example",
        sender_name="Priya Nair",
        recipients="alex@gmail.example",
        body_text="Can you send pricing for 3 seats?",
        sent_at=NOW,
        external_id=uuid.uuid4().hex,
    )
    db_session.add(message)
    db_session.commit()


def _make_pending_signal(
    client: TestClient,
    owner: Account,
    workspace_id: str,
    db_session: Session,
    ai_client: FakeAIClient,
) -> tuple[str, str]:
    """Analyzes a fresh, qualifying conversation and returns
    `(conversation_id, signal_id)`."""
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = QUALIFYING_RESULT
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )
    signals = client.get(f"/v1/workspaces/{workspace_id}/signals", cookies=owner.cookies).json()
    signal_id: str = next(s["id"] for s in signals if s["conversation_id"] == conversation_id)
    return conversation_id, signal_id


# --- List / read ---------------------------------------------------------------------


def test_pending_signals_can_be_listed(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id, signal_id = _make_pending_signal(
        client, owner, workspace_id, db_session, ai_client
    )

    response = client.get(
        f"/v1/workspaces/{workspace_id}/signals?status=pending", cookies=owner.cookies
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == signal_id
    assert body[0]["conversation_id"] == conversation_id
    assert body[0]["status"] == "pending"
    assert body[0]["type"] == "potential_opportunity"
    assert body[0]["title"] == "Asked about pricing for 3 seats."


def test_a_non_qualifying_analysis_produces_no_signal(
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
    ai_client.next_result = make_analysis_result(commercial_signal=False)

    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )
    response = client.get(f"/v1/workspaces/{workspace_id}/signals", cookies=owner.cookies)

    assert response.json() == []


def test_signals_in_another_workspace_are_hidden(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    _make_pending_signal(client, alice, alice_workspace, db_session, ai_client)

    bob_list = client.get(f"/v1/workspaces/{bob_workspace}/signals", cookies=bob.cookies)
    bob_reads_alice_workspace = client.get(
        f"/v1/workspaces/{alice_workspace}/signals", cookies=bob.cookies
    )

    assert bob_list.json() == []
    assert bob_reads_alice_workspace.status_code == 404


def test_reading_a_signal_from_another_workspace_by_id_is_rejected(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    _, alice_signal_id = _make_pending_signal(client, alice, alice_workspace, db_session, ai_client)

    response = client.get(
        f"/v1/workspaces/{bob_workspace}/signals/{alice_signal_id}", cookies=bob.cookies
    )

    assert response.status_code == 404


# --- Confirm ---------------------------------------------------------------------------


def test_a_human_can_confirm_a_pending_signal(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id, signal_id = _make_pending_signal(
        client, owner, workspace_id, db_session, ai_client
    )

    response = client.post(
        f"/v1/workspaces/{workspace_id}/signals/{signal_id}/confirm", cookies=owner.cookies
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["signal"]["status"] == "confirmed"
    assert body["signal"]["id"] == signal_id
    assert body["opportunity"]["conversation_id"] == conversation_id
    assert body["opportunity"]["title"] == "Asked about pricing for 3 seats."
    assert body["signal"]["created_opportunity_id"] == body["opportunity"]["id"]


def test_confirming_creates_exactly_one_opportunity(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _, signal_id = _make_pending_signal(client, owner, workspace_id, db_session, ai_client)

    client.post(f"/v1/workspaces/{workspace_id}/signals/{signal_id}/confirm", cookies=owner.cookies)
    opportunities = client.get(
        f"/v1/workspaces/{workspace_id}/opportunities", cookies=owner.cookies
    ).json()

    assert len(opportunities) == 1


def test_repeated_confirmation_is_rejected_and_does_not_duplicate_the_opportunity(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _, signal_id = _make_pending_signal(client, owner, workspace_id, db_session, ai_client)
    confirm_url = f"/v1/workspaces/{workspace_id}/signals/{signal_id}/confirm"
    client.post(confirm_url, cookies=owner.cookies)

    second = client.post(confirm_url, cookies=owner.cookies)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"
    opportunities = client.get(
        f"/v1/workspaces/{workspace_id}/opportunities", cookies=owner.cookies
    ).json()
    assert len(opportunities) == 1


def test_confirming_a_signal_in_another_workspace_is_rejected(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    _, alice_signal_id = _make_pending_signal(client, alice, alice_workspace, db_session, ai_client)

    response = client.post(
        f"/v1/workspaces/{bob_workspace}/signals/{alice_signal_id}/confirm", cookies=bob.cookies
    )

    assert response.status_code == 404
    assert (
        client.get(f"/v1/workspaces/{alice_workspace}/opportunities", cookies=alice.cookies).json()
        == []
    )


def test_a_non_member_cannot_confirm(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    alice, mallory = make_user("Alice"), make_user("Mallory")
    workspace_id = make_workspace(alice)
    _, signal_id = _make_pending_signal(client, alice, workspace_id, db_session, ai_client)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/signals/{signal_id}/confirm", cookies=mallory.cookies
    )

    assert response.status_code == 404


# --- Dismiss -----------------------------------------------------------------------------


def test_a_human_can_dismiss_a_pending_signal(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _, signal_id = _make_pending_signal(client, owner, workspace_id, db_session, ai_client)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/signals/{signal_id}/dismiss", cookies=owner.cookies
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "dismissed"
    opportunities = client.get(
        f"/v1/workspaces/{workspace_id}/opportunities", cookies=owner.cookies
    ).json()
    assert opportunities == []


def test_repeated_dismissal_is_rejected(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _, signal_id = _make_pending_signal(client, owner, workspace_id, db_session, ai_client)
    dismiss_url = f"/v1/workspaces/{workspace_id}/signals/{signal_id}/dismiss"
    client.post(dismiss_url, cookies=owner.cookies)

    second = client.post(dismiss_url, cookies=owner.cookies)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"


def test_dismissing_an_already_confirmed_signal_is_rejected(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _, signal_id = _make_pending_signal(client, owner, workspace_id, db_session, ai_client)
    client.post(f"/v1/workspaces/{workspace_id}/signals/{signal_id}/confirm", cookies=owner.cookies)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/signals/{signal_id}/dismiss", cookies=owner.cookies
    )

    assert response.status_code == 409


def test_a_non_member_cannot_dismiss(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    alice, mallory = make_user("Alice"), make_user("Mallory")
    workspace_id = make_workspace(alice)
    _, signal_id = _make_pending_signal(client, alice, workspace_id, db_session, ai_client)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/signals/{signal_id}/dismiss", cookies=mallory.cookies
    )

    assert response.status_code == 404


# --- Security --------------------------------------------------------------------------


def test_unauthenticated_requests_are_rejected(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _, signal_id = _make_pending_signal(client, owner, workspace_id, db_session, ai_client)

    assert client.get(f"/v1/workspaces/{workspace_id}/signals").status_code == 401
    assert (
        client.post(f"/v1/workspaces/{workspace_id}/signals/{signal_id}/confirm").status_code == 401
    )
    assert (
        client.post(f"/v1/workspaces/{workspace_id}/signals/{signal_id}/dismiss").status_code == 401
    )


def test_no_raw_provider_or_prompt_fields_appear_in_signal_responses(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    _make_pending_signal(client, owner, workspace_id, db_session, ai_client)

    response = client.get(f"/v1/workspaces/{workspace_id}/signals", cookies=owner.cookies)

    body = response.json()
    assert len(body) == 1
    forbidden_keys = {"prompt", "raw_output", "raw_response", "provider_response"}
    assert forbidden_keys.isdisjoint(body[0].keys())
