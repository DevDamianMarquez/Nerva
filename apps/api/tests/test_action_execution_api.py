"""Milestone 5 Block 6: production wiring for `ActionExecutionEngine` —
`POST /workspaces/{workspace_id}/actions/{action_id}/execute`, the first real HTTP
route that drives an approved `Action` through the engine (`nerva.api.deps.
get_action_executors`, `routes.actions.execute_action`).

No test here makes a real network call or sends a real email: `get_google_oauth_client`
and `get_gmail_sender` are overridden to fakes exactly like every other Gmail-touching
test suite (see `tests/conftest.py`'s `client` fixture).
"""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.crypto import encrypt_secret
from nerva.core.enums import IntegrationProvider, IntegrationStatus, MemberRole
from nerva.models import Integration, WorkspaceMember
from nerva.nerva_connect.google.errors import GoogleAuthError
from nerva.services.actions import ActionService
from tests.fakes.google import DEFAULT_SCOPE, READONLY_ONLY_SCOPE, FakeGmailSender
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

VALID_SEND_EMAIL_PAYLOAD = {
    "recipients": ["priya@halcyon.example"],
    "subject": "Pricing for 3 seats",
    "body": "Hi Priya, following up on pricing for a 3-seat plan.",
}
SEND_EMAIL_ACTION_BODY = {
    "type": "send_email",
    "summary": "Reply to Priya",
    "payload": VALID_SEND_EMAIL_PAYLOAD,
}


@dataclass
class Setup:
    owner: Account
    workspace_id: str

    @property
    def actions(self) -> str:
        return f"/v1/workspaces/{self.workspace_id}/actions"


@pytest.fixture
def setup(make_user: MakeUser, make_workspace: MakeWorkspace) -> Setup:
    owner = make_user("Alice")
    return Setup(owner, make_workspace(owner, "Northwind"))


def _add_member(db_session: Session, workspace_id: str, user: Account) -> None:
    """A minimal way to add a second member without an invitation flow (not yet
    implemented) — mirrors `test_analysis_api.py`/`test_integrations_management.py`."""
    db_session.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(workspace_id), user_id=uuid.UUID(user.id), role=MemberRole.MEMBER
        )
    )
    db_session.commit()


def _connect_integration(
    db_session: Session,
    workspace_id: str,
    *,
    scopes: str = DEFAULT_SCOPE,
    status: IntegrationStatus = IntegrationStatus.CONNECTED,
) -> None:
    """Inserts an `Integration` row directly, bypassing the OAuth HTTP flow — this
    file tests the execution endpoint, not OAuth connect (already covered by
    `test_integrations_oauth.py`), the same shortcut `test_gmail_send_executor.py`
    already takes."""
    db_session.add(
        Integration(
            workspace_id=uuid.UUID(workspace_id),
            provider=IntegrationProvider.GOOGLE,
            status=status,
            external_account_id="google-sub-1",
            external_account_email="alex@gmail.example",
            scopes=scopes,
            encrypted_refresh_token=encrypt_secret("fake-refresh-token"),
        )
    )
    db_session.commit()


def _create_opportunity(client: TestClient, setup: Setup, *, title: str = "Acme deal") -> str:
    response = client.post(
        f"/v1/workspaces/{setup.workspace_id}/opportunities",
        json={"title": title},
        cookies=setup.owner.cookies,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def _propose(
    client: TestClient, setup: Setup, body: dict[str, Any], *, as_account: Account | None = None
) -> dict[str, Any]:
    response = client.post(
        setup.actions, json=body, cookies=(as_account or setup.owner).cookies
    )
    assert response.status_code == 201, response.text
    action: dict[str, Any] = response.json()
    return action


def _approve(
    client: TestClient, setup: Setup, action_id: str, *, as_account: Account | None = None
) -> None:
    response = client.post(
        f"{setup.actions}/{action_id}/approve", cookies=(as_account or setup.owner).cookies
    )
    assert response.status_code == 200, response.text


def _execute(
    client: TestClient, setup: Setup, action_id: str, *, as_account: Account | None = None
) -> Any:
    return client.post(
        f"{setup.actions}/{action_id}/execute", cookies=(as_account or setup.owner).cookies
    )


# ---------------------------------------------------------------------------
# A. Happy path — both registered executors, through the real HTTP endpoint
# ---------------------------------------------------------------------------


def test_the_owner_can_execute_an_approved_update_opportunity_stage_action(
    client: TestClient, setup: Setup
) -> None:
    opportunity_id = _create_opportunity(client, setup)
    action = _propose(
        client,
        setup,
        {
            "type": "update_opportunity_stage",
            "summary": "Move Acme to qualified",
            "payload": {"target_stage": "qualified"},
            "opportunity_id": opportunity_id,
        },
    )
    _approve(client, setup, action["id"])

    response = _execute(client, setup, action["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["result"]["new_stage"] == "qualified"

    opportunity = client.get(
        f"/v1/workspaces/{setup.workspace_id}/opportunities/{opportunity_id}",
        cookies=setup.owner.cookies,
    ).json()
    assert opportunity["stage"] == "qualified"


def test_the_owner_can_execute_an_approved_send_email_action(
    client: TestClient, db_session: Session, setup: Setup, gmail_sender: FakeGmailSender
) -> None:
    _connect_integration(db_session, setup.workspace_id)
    action = _propose(
        client,
        setup,
        {
            "type": "send_email",
            "summary": "Reply to Priya",
            "payload": VALID_SEND_EMAIL_PAYLOAD,
        },
    )
    _approve(client, setup, action["id"])

    response = _execute(client, setup, action["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["result"]["external_reference_id"] == gmail_sender.next_result.message_id
    assert len(gmail_sender.calls) == 1
    # `result` is executor-defined output (see `GmailSendExecutor.execute`), unlike
    # `payload`, which legitimately echoes the original request body back to the
    # caller — so the leak check must target `result` alone, not the whole response.
    assert "Hi Priya" not in json.dumps(body["result"])


def test_execution_is_attributed_to_nerva_not_the_executing_owner(
    client: TestClient, setup: Setup
) -> None:
    """`approved_by_user_id` stays the human who approved; the completion event is
    recorded as `Actor.nerva()` (see `ActionService.start_execution`/`.complete`) —
    executing is not a second human decision, it is Nerva carrying one out."""
    opportunity_id = _create_opportunity(client, setup)
    action = _propose(
        client,
        setup,
        {
            "type": "update_opportunity_stage",
            "summary": "Move Acme to qualified",
            "payload": {"target_stage": "qualified"},
            "opportunity_id": opportunity_id,
        },
    )
    _approve(client, setup, action["id"])
    _execute(client, setup, action["id"])

    feed = client.get(
        f"/v1/workspaces/{setup.workspace_id}/activity?entity_id={action['id']}",
        cookies=setup.owner.cookies,
    ).json()
    completed_event = next(e for e in feed if e["event_type"] == "action.completed")
    assert completed_event["actor_type"] == "nerva"
    assert completed_event["actor_user_id"] is None

    reloaded = client.get(f"{setup.actions}/{action['id']}", cookies=setup.owner.cookies).json()
    assert reloaded["approved_by_user_id"] == setup.owner.id


# ---------------------------------------------------------------------------
# B. Permissions — execute is owner-only, stricter than approve/reject
# ---------------------------------------------------------------------------


def test_a_non_owner_member_cannot_execute_an_action(
    client: TestClient, db_session: Session, make_user: MakeUser, setup: Setup
) -> None:
    member = make_user("Bob")
    _add_member(db_session, setup.workspace_id, member)
    opportunity_id = _create_opportunity(client, setup)
    action = _propose(
        client,
        setup,
        {
            "type": "update_opportunity_stage",
            "summary": "Move Acme to qualified",
            "payload": {"target_stage": "qualified"},
            "opportunity_id": opportunity_id,
        },
    )
    _approve(client, setup, action["id"])

    response = _execute(client, setup, action["id"], as_account=member)

    assert response.status_code == 403
    reloaded = client.get(f"{setup.actions}/{action['id']}", cookies=setup.owner.cookies).json()
    assert reloaded["status"] == "approved"  # untouched


def test_a_non_owner_member_can_still_approve_but_not_execute_send_email(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    setup: Setup,
    gmail_sender: FakeGmailSender,
) -> None:
    """Confirms the deliberate asymmetry: `approve`/`reject` stay open to any member
    (unchanged, out of scope for this block); only `execute` is owner-only."""
    member = make_user("Bob")
    _add_member(db_session, setup.workspace_id, member)
    _connect_integration(db_session, setup.workspace_id)
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)

    approve_response = client.post(
        f"{setup.actions}/{action['id']}/approve", cookies=member.cookies
    )
    assert approve_response.status_code == 200, approve_response.text  # any member may approve

    execute_response = _execute(client, setup, action["id"], as_account=member)
    assert execute_response.status_code == 403
    assert gmail_sender.calls == []


# ---------------------------------------------------------------------------
# C. Invalid states — never execute proposed/rejected/executing/completed; no
#    auto-approval shortcut
# ---------------------------------------------------------------------------


def test_a_proposed_action_cannot_be_executed_without_approval(
    client: TestClient, setup: Setup
) -> None:
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)

    response = _execute(client, setup, action["id"])

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"code": "not_executable"}
    reloaded = client.get(f"{setup.actions}/{action['id']}", cookies=setup.owner.cookies).json()
    assert reloaded["status"] == "proposed"
    assert reloaded["approved_at"] is None  # never silently auto-approved


def test_a_rejected_action_cannot_be_executed(client: TestClient, setup: Setup) -> None:
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)
    client.post(f"{setup.actions}/{action['id']}/reject", cookies=setup.owner.cookies)

    response = _execute(client, setup, action["id"])

    assert response.status_code == 409


def test_an_already_executing_action_cannot_be_executed_again(
    client: TestClient, db_session: Session, setup: Setup
) -> None:
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)
    _approve(client, setup, action["id"])
    ctx = WorkspaceContext(
        workspace_id=uuid.UUID(setup.workspace_id),
        user_id=uuid.UUID(setup.owner.id),
        role=MemberRole.OWNER,
    )
    ActionService(db_session, ctx).start_execution(uuid.UUID(action["id"]))

    response = _execute(client, setup, action["id"])

    assert response.status_code == 409


def test_a_duplicate_execute_request_after_completion_is_rejected_not_repeated(
    client: TestClient, db_session: Session, setup: Setup, gmail_sender: FakeGmailSender
) -> None:
    """Simulates a duplicate/retried client request: the second call must not send a
    second email. True concurrent races are covered at the service layer by
    `ActionRepository.get_for_update`'s row lock (see `nerva_actions.engine`'s module
    docstring); this is the observable HTTP-level guarantee that falls out of it."""
    _connect_integration(db_session, setup.workspace_id)
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)
    _approve(client, setup, action["id"])

    first = _execute(client, setup, action["id"])
    second = _execute(client, setup, action["id"])

    assert first.status_code == 200
    assert first.json()["status"] == "completed"
    assert second.status_code == 409
    assert len(gmail_sender.calls) == 1  # never sent twice


# ---------------------------------------------------------------------------
# D. No executor registered for the action's type (create_task)
# ---------------------------------------------------------------------------


def test_an_action_type_with_no_registered_executor_fails_safely_with_409(
    client: TestClient, setup: Setup
) -> None:
    action = _propose(client, setup, {"type": "create_task", "summary": "Call Tomás"})
    _approve(client, setup, action["id"])

    response = _execute(client, setup, action["id"])

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"code": "not_executable"}


# ---------------------------------------------------------------------------
# E. Gmail integration unavailable — a *safe* 200/failed, not an HTTP error
# ---------------------------------------------------------------------------


def test_send_email_without_a_connected_integration_fails_safely_as_200(
    client: TestClient, setup: Setup, gmail_sender: FakeGmailSender
) -> None:
    """No `Integration` at all: the action still reaches `completed`/`failed`
    normally through the engine (the failure happens *inside* the executor, after
    `start_execution`) — not a 409. This mirrors `test_gmail_send_executor.py`'s
    equivalent service-level test, now verified at the HTTP boundary."""
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)
    _approve(client, setup, action["id"])

    response = _execute(client, setup, action["id"])

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert gmail_sender.calls == []


def test_send_email_through_a_readonly_only_integration_fails_safely_as_200(
    client: TestClient, db_session: Session, setup: Setup, gmail_sender: FakeGmailSender
) -> None:
    _connect_integration(db_session, setup.workspace_id, scopes=READONLY_ONLY_SCOPE)
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)
    _approve(client, setup, action["id"])

    response = _execute(client, setup, action["id"])

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "failed"
    assert gmail_sender.calls == []


# ---------------------------------------------------------------------------
# F. Tenant isolation
# ---------------------------------------------------------------------------


def test_an_action_from_another_workspace_cannot_be_executed(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace, setup: Setup
) -> None:
    opportunity_id = _create_opportunity(client, setup)
    action = _propose(
        client,
        setup,
        {
            "type": "update_opportunity_stage",
            "summary": "Move Acme to qualified",
            "payload": {"target_stage": "qualified"},
            "opportunity_id": opportunity_id,
        },
    )
    _approve(client, setup, action["id"])

    other_owner = make_user("Bob")
    other_workspace_id = make_workspace(other_owner, "Acme")
    response = client.post(
        f"/v1/workspaces/{other_workspace_id}/actions/{action['id']}/execute",
        cookies=other_owner.cookies,
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# G. No sensitive content ever reaches the HTTP response
# ---------------------------------------------------------------------------


def test_a_provider_auth_failure_never_leaks_into_the_http_response(
    client: TestClient, db_session: Session, setup: Setup, gmail_sender: FakeGmailSender
) -> None:
    _connect_integration(db_session, setup.workspace_id)
    gmail_sender.fail_with = GoogleAuthError("fake: invalid_grant, token=super-secret-value")
    action = _propose(client, setup, SEND_EMAIL_ACTION_BODY)
    _approve(client, setup, action["id"])

    response = _execute(client, setup, action["id"])

    assert response.status_code == 200
    assert "super-secret-value" not in response.text
    assert "invalid_grant" not in response.text
