"""M8.1: `Action.conversation_id` — the persisted Conversation -> Action
relationship.

Covers three layers: the model/DB constraint itself (composite tenant FK,
direct `IntegrityError` test), the materialization invariant
(`ProposalProvenance.conversation_id` is the only source for an automatic
Action), and the HTTP-level filter (`GET /actions?conversation_id=`). Human
`POST /actions` behavior is asserted unchanged throughout.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import (
    ActionType,
    AnalysisStatus,
    ConversationCategory,
    ConversationPriority,
    MemberRole,
    MessageDirection,
)
from nerva.models import Action, Contact, Conversation, ConversationAnalysis, Message
from nerva.nerva_ai.agent_client import ActionSuggestion
from nerva.schemas.actions import ActionCreate
from nerva.services.actions import ActionService
from tests.fakes.agent_reasoning import FakeAgentReasoningClient
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
CONTACT_EMAIL = "priya@halcyon.example"


# ---------------------------------------------------------------------------
# Row builders (direct DB insertion — conversations/messages/analyses have no
# HTTP create route; see `routes.conversations`'s own module docstring).
# ---------------------------------------------------------------------------


def _contact(db_session: Session, workspace_id: str, *, email: str = CONTACT_EMAIL) -> Contact:
    contact = Contact(workspace_id=uuid.UUID(workspace_id), email=email, name="Priya Nair")
    db_session.add(contact)
    db_session.commit()
    return contact


def _conversation(
    db_session: Session, workspace_id: str, *, contact_id: uuid.UUID | None
) -> Conversation:
    conversation = Conversation(
        workspace_id=uuid.UUID(workspace_id), subject="Pricing question", contact_id=contact_id
    )
    db_session.add(conversation)
    db_session.commit()
    return conversation


def _message(db_session: Session, workspace_id: str, conversation_id: uuid.UUID) -> Message:
    message = Message(
        workspace_id=uuid.UUID(workspace_id),
        conversation_id=conversation_id,
        direction=MessageDirection.INBOUND,
        sender_email=CONTACT_EMAIL,
        sender_name="Priya Nair",
        body_text="Can you send pricing for 3 seats?",
        sent_at=NOW,
    )
    db_session.add(message)
    db_session.commit()
    return message


def _analysis(
    db_session: Session, workspace_id: str, conversation_id: uuid.UUID
) -> ConversationAnalysis:
    analysis = ConversationAnalysis(
        workspace_id=uuid.UUID(workspace_id),
        conversation_id=conversation_id,
        status=AnalysisStatus.COMPLETED,
        attempts=1,
        schema_version=1,
        prompt_version="v1",
        provider="fake",
        model_id="fake-model-1",
        input_fingerprint="a" * 64,
        category=ConversationCategory.SALES_LEAD,
        priority=ConversationPriority.MEDIUM,
        sentiment=None,
        requires_response=True,
        commercial_signal=True,
        confidence=0.8,
        summary="Prospect asked about pricing for 3 seats.",
        intent="request_quote",
        suggested_next_step="Send pricing information.",
        analyzed_at=NOW,
    )
    db_session.add(analysis)
    db_session.commit()
    return analysis


def _suggestion(**overrides: object) -> ActionSuggestion:
    defaults: dict[str, object] = {
        "action_type": ActionType.SEND_EMAIL,
        "summary": "Reply with pricing for 3 seats.",
        "payload": {
            "recipients": [CONTACT_EMAIL],
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        },
        "confidence": 0.8,
        "reason": "The prospect explicitly asked for pricing and has not received it yet.",
        "evidence": ["message_1"],
    }
    defaults.update(overrides)
    return ActionSuggestion.model_validate(defaults)


@dataclass
class Setup:
    owner: Account
    workspace_id: str
    conversation_id: uuid.UUID

    @property
    def actions_url(self) -> str:
        return f"/v1/workspaces/{self.workspace_id}/actions"

    @property
    def reason_url(self) -> str:
        return f"/v1/workspaces/{self.workspace_id}/conversations/{self.conversation_id}/reason"


@pytest.fixture
def setup(make_user: MakeUser, make_workspace: MakeWorkspace, db_session: Session) -> Setup:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner, "Northwind")
    contact = _contact(db_session, workspace_id)
    conversation = _conversation(db_session, workspace_id, contact_id=contact.id)
    _message(db_session, workspace_id, conversation.id)
    _analysis(db_session, workspace_id, conversation.id)
    return Setup(owner, workspace_id, conversation.id)


# ---------------------------------------------------------------------------
# A. Automatic Actions carry conversation_id; duplicates preserve it
# ---------------------------------------------------------------------------


def test_an_automatic_action_carries_its_conversation_id(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()

    response = client.post(setup.reason_url, cookies=setup.owner.cookies)

    assert response.status_code == 200, response.text
    assert response.json()["action"]["conversation_id"] == str(setup.conversation_id)


def test_a_duplicate_automatic_proposal_preserves_the_same_conversation_id(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()

    first = client.post(setup.reason_url, cookies=setup.owner.cookies)
    second = client.post(setup.reason_url, cookies=setup.owner.cookies)

    assert second.json()["outcome"] == "action_duplicate"
    assert (
        second.json()["action"]["conversation_id"]
        == first.json()["action"]["conversation_id"]
        == str(setup.conversation_id)
    )


# ---------------------------------------------------------------------------
# B. Human Actions remain NULL; ActionCreate cannot forge conversation_id
# ---------------------------------------------------------------------------


def test_a_human_proposed_action_has_a_null_conversation_id(
    client: TestClient, setup: Setup
) -> None:
    response = client.post(
        setup.actions_url,
        json={"type": "send_email", "summary": "Reply to Priya", "payload": {}},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 201, response.text
    assert response.json()["conversation_id"] is None


def test_action_create_rejects_a_client_supplied_conversation_id(
    client: TestClient, setup: Setup
) -> None:
    """`ActionCreate`'s `extra='forbid'` (see `schemas.common.RequestModel`)
    rejects the field outright — a human cannot forge a conversation link by
    simply adding it to the request body."""
    response = client.post(
        setup.actions_url,
        json={
            "type": "send_email",
            "summary": "Reply to Priya",
            "payload": {},
            "conversation_id": str(setup.conversation_id),
        },
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 422


def test_action_service_propose_without_conversation_id_defaults_to_null(
    db_session: Session, setup: Setup
) -> None:
    """Direct service-level confirmation that the default for every existing
    caller (which never passes the new keyword-only parameter) is
    unchanged."""
    ctx = WorkspaceContext(
        workspace_id=uuid.UUID(setup.workspace_id), user_id=uuid.UUID(setup.owner.id),
        role=MemberRole.OWNER,
    )
    action = ActionService(db_session, ctx).propose(
        ActionCreate(type=ActionType.SEND_EMAIL, summary="Reply to Priya", payload={})
    )

    assert action.conversation_id is None


# ---------------------------------------------------------------------------
# C. Multi-tenant integrity at the DB level
# ---------------------------------------------------------------------------


def test_the_composite_fk_rejects_a_conversation_id_from_another_workspace(
    db_session: Session, setup: Setup, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    """A direct, bypass-the-service attempt to set `Action.conversation_id` to
    a conversation in a *different* workspace must fail at the database
    level — the composite `(workspace_id, conversation_id)` FK has no row to
    match, exactly like `Action.opportunity_id` already behaves."""
    other_owner = make_user("Bob")
    other_workspace_id = make_workspace(other_owner, "Acme")

    action = Action(
        workspace_id=uuid.UUID(other_workspace_id),
        type=ActionType.SEND_EMAIL.value,
        summary="Cross-tenant attempt",
        payload={},
        conversation_id=setup.conversation_id,  # belongs to `setup.workspace_id`, not this one
        created_by_type=Actor.nerva().type,
        created_by_user_id=Actor.nerva().user_id,
    )
    db_session.add(action)

    with pytest.raises(IntegrityError):
        db_session.flush()
    db_session.rollback()


# ---------------------------------------------------------------------------
# D. Filtering
# ---------------------------------------------------------------------------


def test_listing_actions_filtered_by_conversation_id(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    client.post(
        setup.actions_url,
        json={"type": "send_email", "summary": "Unrelated human action", "payload": {}},
        cookies=setup.owner.cookies,
    )
    agent_reasoning_client.next_decision = _suggestion()
    client.post(setup.reason_url, cookies=setup.owner.cookies)

    response = client.get(
        f"{setup.actions_url}?conversation_id={setup.conversation_id}", cookies=setup.owner.cookies
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["conversation_id"] == str(setup.conversation_id)


def test_status_and_conversation_id_filter_together(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()
    created = client.post(setup.reason_url, cookies=setup.owner.cookies).json()["action"]

    proposed = client.get(
        f"{setup.actions_url}?conversation_id={setup.conversation_id}&status=proposed",
        cookies=setup.owner.cookies,
    ).json()
    assert [a["id"] for a in proposed] == [created["id"]]

    approved = client.get(
        f"{setup.actions_url}?conversation_id={setup.conversation_id}&status=approved",
        cookies=setup.owner.cookies,
    ).json()
    assert approved == []


def test_an_unknown_conversation_id_filter_returns_empty(
    client: TestClient, setup: Setup
) -> None:
    response = client.get(
        f"{setup.actions_url}?conversation_id={uuid.uuid4()}", cookies=setup.owner.cookies
    )

    assert response.status_code == 200
    assert response.json() == []


def test_a_cross_workspace_conversation_id_filter_returns_empty(
    client: TestClient,
    setup: Setup,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
) -> None:
    """`setup.conversation_id` belongs to `setup.workspace_id`; querying it
    from a *different* workspace must behave exactly like an unknown id —
    never reveal that the conversation exists elsewhere."""
    other_owner = make_user("Bob")
    other_workspace_id = make_workspace(other_owner, "Acme")

    response = client.get(
        f"/v1/workspaces/{other_workspace_id}/actions?conversation_id={setup.conversation_id}",
        cookies=other_owner.cookies,
    )

    assert response.status_code == 200
    assert response.json() == []


def test_no_conversation_id_filter_leaves_existing_behavior_unchanged(
    client: TestClient, setup: Setup
) -> None:
    client.post(
        setup.actions_url,
        json={"type": "send_email", "summary": "A human action", "payload": {}},
        cookies=setup.owner.cookies,
    )

    response = client.get(setup.actions_url, cookies=setup.owner.cookies)

    assert response.status_code == 200
    assert len(response.json()) == 1


# ---------------------------------------------------------------------------
# E. ActionRead exposes conversation_id
# ---------------------------------------------------------------------------


def test_action_read_exposes_conversation_id_on_get_by_id(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()
    created = client.post(setup.reason_url, cookies=setup.owner.cookies).json()["action"]

    response = client.get(f"{setup.actions_url}/{created['id']}", cookies=setup.owner.cookies)

    assert response.status_code == 200
    assert response.json()["conversation_id"] == str(setup.conversation_id)
