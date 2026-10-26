"""M8.1: `POST /workspaces/{workspace_id}/conversations/{conversation_id}/reason` —
the first production HTTP trigger for `AgentReasoningService.reason_and_propose`
(`routes.conversations.reason_about_conversation`).

Conversations/messages/analyses have no HTTP create route (see
`routes.conversations`'s own module docstring), so every scenario here inserts
those rows directly via `db_session`, the same shortcut
`test_action_execution_api.py` already takes for `Integration` rows — only the
`/reason` call itself, and workspace/user creation, go through the real HTTP
API. `agent_reasoning_client` (`tests/fakes/agent_reasoning.py`, wired into
`client` by `tests/conftest.py`) stands in for the provider: no test here ever
reaches `UnconfiguredAgentReasoningClient`, Groq, or OpenAI.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.enums import (
    ActionType,
    AnalysisStatus,
    ConversationCategory,
    ConversationPriority,
    MemberRole,
    MessageDirection,
)
from nerva.models import Contact, Conversation, ConversationAnalysis, Message, WorkspaceMember
from nerva.nerva_ai.agent_client import ActionSuggestion
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from tests.fakes.agent_reasoning import FakeAgentReasoningClient, make_no_action_decision
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
CONTACT_EMAIL = "priya@halcyon.example"


@dataclass
class Setup:
    owner: Account
    workspace_id: str
    contact_id: uuid.UUID
    conversation_id: uuid.UUID

    @property
    def reason_url(self) -> str:
        return f"/v1/workspaces/{self.workspace_id}/conversations/{self.conversation_id}/reason"

    @property
    def actions_url(self) -> str:
        return f"/v1/workspaces/{self.workspace_id}/actions"


def _add_member(db_session: Session, workspace_id: str, user: Account) -> None:
    db_session.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(workspace_id), user_id=uuid.UUID(user.id), role=MemberRole.MEMBER
        )
    )
    db_session.commit()


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
    db_session: Session,
    workspace_id: str,
    conversation_id: uuid.UUID,
    *,
    status: AnalysisStatus = AnalysisStatus.COMPLETED,
) -> ConversationAnalysis:
    completed = status == AnalysisStatus.COMPLETED
    analysis = ConversationAnalysis(
        workspace_id=uuid.UUID(workspace_id),
        conversation_id=conversation_id,
        status=status,
        attempts=1,
        schema_version=1,
        prompt_version="v1",
        provider="fake",
        model_id="fake-model-1",
        input_fingerprint="a" * 64,
        category=ConversationCategory.SALES_LEAD if completed else None,
        priority=ConversationPriority.MEDIUM if completed else None,
        sentiment=None,
        requires_response=True if completed else None,
        commercial_signal=True if completed else None,
        confidence=0.8 if completed else None,
        summary="Prospect asked about pricing for 3 seats." if completed else None,
        intent="request_quote",
        suggested_next_step="Send pricing information." if completed else None,
        analyzed_at=NOW,
    )
    db_session.add(analysis)
    db_session.commit()
    return analysis


@pytest.fixture
def setup(
    make_user: MakeUser, make_workspace: MakeWorkspace, db_session: Session
) -> Setup:
    """A complete, reasonable conversation: a contact, one inbound message, and
    a completed analysis — the minimum a valid `/reason` call needs."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner, "Northwind")
    contact = _contact(db_session, workspace_id)
    conversation = _conversation(db_session, workspace_id, contact_id=contact.id)
    _message(db_session, workspace_id, conversation.id)
    _analysis(db_session, workspace_id, conversation.id)
    return Setup(owner, workspace_id, contact.id, conversation.id)


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


def _reason(client: TestClient, setup: Setup, *, as_account: Account | None = None) -> "object":
    return client.post(setup.reason_url, cookies=(as_account or setup.owner).cookies)


# ---------------------------------------------------------------------------
# A. Auth / membership / tenant isolation
# ---------------------------------------------------------------------------


def test_an_unauthenticated_request_is_rejected(client: TestClient, setup: Setup) -> None:
    response = client.post(setup.reason_url)  # no cookies

    assert response.status_code == 401


def test_a_non_member_cannot_reason_about_a_conversation(
    client: TestClient, make_user: MakeUser, setup: Setup
) -> None:
    stranger = make_user("Eve")

    response = _reason(client, setup, as_account=stranger)

    assert response.status_code == 404


def test_a_conversation_from_another_workspace_returns_not_found(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    setup: Setup,
) -> None:
    other_owner = make_user("Bob")
    other_workspace_id = make_workspace(other_owner, "Acme")

    response = client.post(
        f"/v1/workspaces/{other_workspace_id}/conversations/{setup.conversation_id}/reason",
        cookies=other_owner.cookies,
    )

    assert response.status_code == 404


def test_an_unknown_conversation_id_returns_not_found(client: TestClient, setup: Setup) -> None:
    response = client.post(
        f"/v1/workspaces/{setup.workspace_id}/conversations/{uuid.uuid4()}/reason",
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# B. Role — member or owner, never owner-only
# ---------------------------------------------------------------------------


def test_a_non_owner_member_can_invoke_reason(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    setup: Setup,
    agent_reasoning_client: FakeAgentReasoningClient,
) -> None:
    member = make_user("Bob")
    _add_member(db_session, setup.workspace_id, member)
    agent_reasoning_client.next_decision = make_no_action_decision()

    response = _reason(client, setup, as_account=member)

    assert response.status_code == 200, response.text


def test_the_owner_can_invoke_reason(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = make_no_action_decision()

    response = _reason(client, setup)

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# C. Domain outcomes — all normal 200s
# ---------------------------------------------------------------------------


def test_no_action_is_a_normal_200_outcome(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = make_no_action_decision()

    response = _reason(client, setup)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "no_action"
    assert body["action"] is None


def test_policy_rejected_is_a_normal_200_outcome(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    """One message exists, so `message_2` cannot be grounded —
    `ProposalPolicyReason.INVALID_EVIDENCE`, reported as `POLICY_REJECTED`."""
    agent_reasoning_client.next_decision = _suggestion(evidence=["message_2"])

    response = _reason(client, setup)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "policy_rejected"
    assert body["action"] is None


def test_action_created_returns_the_new_action(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()

    response = _reason(client, setup)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "action_created"
    assert body["action"] is not None
    assert body["action"]["type"] == "send_email"
    assert body["action"]["status"] == "proposed"
    assert body["action"]["conversation_id"] == str(setup.conversation_id)


def test_action_duplicate_returns_the_existing_action(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()

    first = _reason(client, setup)
    second = _reason(client, setup)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["outcome"] == "action_created"
    assert second.json()["outcome"] == "action_duplicate"
    assert second.json()["action"]["id"] == first.json()["action"]["id"]
    # Two provider calls, exactly one Action — see the module docstring's
    # "Repeated requests" (M8.1 accepts this cost; see M7.1's own tests).
    assert agent_reasoning_client.call_count == 2
    listing = client.get(setup.actions_url, cookies=setup.owner.cookies).json()
    assert len(listing) == 1


# ---------------------------------------------------------------------------
# D. Provider failures — safe mapping, never raw provider text
# ---------------------------------------------------------------------------


def test_provider_unconfigured_returns_503(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.fail_with = AIAuthError("fake: invalid API key, sk-super-secret")

    response = _reason(client, setup)

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"code": "ai_not_configured"}
    assert "sk-super-secret" not in response.text


def test_provider_unavailable_returns_a_safe_503(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.fail_with = AIProviderError("fake: connection reset, internal trace xyz")

    response = _reason(client, setup)

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"code": "ai_unavailable"}
    assert "internal trace xyz" not in response.text


def test_provider_rate_limited_returns_429(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.fail_with = AIRateLimitError("fake: too many requests")

    response = _reason(client, setup)

    assert response.status_code == 429
    assert response.json()["error"]["details"] == {"code": "rate_limited"}


def test_malformed_provider_output_returns_a_safe_503(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.fail_with = AIOutputValidationError(
        "fake: could not parse raw model output {garbage}"
    )

    response = _reason(client, setup)

    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"code": "invalid_ai_output"}
    assert "garbage" not in response.text


# ---------------------------------------------------------------------------
# E. Preconditions — 409, never a 5xx or a silent empty success
# ---------------------------------------------------------------------------


def test_analysis_not_ready_returns_409(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
) -> None:
    owner = make_user("Carol")
    workspace_id = make_workspace(owner, "Globex")
    contact = _contact(db_session, workspace_id)
    conversation = _conversation(db_session, workspace_id, contact_id=contact.id)
    _message(db_session, workspace_id, conversation.id)
    # No analysis at all.

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation.id}/reason",
        cookies=owner.cookies,
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"code": "analysis_not_ready"}


def test_recipient_not_resolvable_returns_409(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
) -> None:
    owner = make_user("Dana")
    workspace_id = make_workspace(owner, "Initech")
    conversation = _conversation(db_session, workspace_id, contact_id=None)  # no contact at all
    _message(db_session, workspace_id, conversation.id)
    _analysis(db_session, workspace_id, conversation.id)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation.id}/reason",
        cookies=owner.cookies,
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"code": "recipient_not_resolvable"}


# ---------------------------------------------------------------------------
# F. No internal reasoning details ever reach the client
# ---------------------------------------------------------------------------


def test_the_response_excludes_internal_reasoning_fingerprint_provider_and_model(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()

    response = _reason(client, setup)

    body = response.json()
    assert set(body.keys()) == {"outcome", "action", "detail"}
    assert "reasoning_fingerprint" not in response.text
    assert "fake-reasoning-model-1" not in response.text  # FakeAgentReasoningClient.model_id


# ---------------------------------------------------------------------------
# G. /reason never approves and never executes
# ---------------------------------------------------------------------------


def test_reason_never_approves_the_created_action(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()

    response = _reason(client, setup)

    action = response.json()["action"]
    assert action["status"] == "proposed"
    assert action["approved_at"] is None
    assert action["approved_by_user_id"] is None


def test_reason_never_executes_the_created_action(
    client: TestClient, setup: Setup, agent_reasoning_client: FakeAgentReasoningClient
) -> None:
    agent_reasoning_client.next_decision = _suggestion()

    response = _reason(client, setup)

    action = response.json()["action"]
    assert action["status"] == "proposed"
    assert action["started_at"] is None
    assert action["finished_at"] is None
    assert action["result"] is None
