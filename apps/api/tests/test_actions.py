"""Proposing, approving and rejecting actions through the API, plus the
executor-side transitions through the service."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionStatus, ActorType, MemberRole
from nerva.core.errors import InvalidStateTransitionError, NotFoundError
from nerva.schemas.actions import ActionCreate
from nerva.services.actions import ActionService
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


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


@pytest.fixture
def propose(client: TestClient, setup: Setup) -> Callable[..., dict[str, Any]]:
    def _propose(**overrides: Any) -> dict[str, Any]:
        body = {
            "type": "send_email",
            "summary": "Reply to Priya about 3-seat pricing",
            "payload": {"to": "priya@halcyon.example", "subject": "Pricing for 3 seats"},
            **overrides,
        }
        response = client.post(setup.actions, json=body, cookies=setup.owner.cookies)
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    return _propose


def test_a_proposed_action_waits_for_a_human(
    propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()

    assert action["status"] == "proposed"
    assert action["type"] == "send_email"
    assert action["payload"]["to"] == "priya@halcyon.example"
    assert action["created_by_type"] == "user"
    assert action["created_by_user_id"] == setup.owner.id
    assert action["workspace_id"] == setup.workspace_id
    assert action["approved_by_user_id"] is None
    assert action["approved_at"] is None
    assert action["result"] is None


def test_approving_records_who_and_when(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()

    response = client.post(f"{setup.actions}/{action['id']}/approve", cookies=setup.owner.cookies)

    assert response.status_code == 200
    approved = response.json()
    assert approved["status"] == "approved"
    assert approved["approved_by_user_id"] == setup.owner.id
    assert approved["approved_at"] is not None
    assert approved["rejected_by_user_id"] is None
    assert (
        client.get(f"{setup.actions}/{action['id']}", cookies=setup.owner.cookies).json()["status"]
        == "approved"
    )


def test_rejecting_records_who_when_and_why(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()

    response = client.post(
        f"{setup.actions}/{action['id']}/reject",
        json={"reason": "Priya already got a quote yesterday."},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 200
    rejected = response.json()
    assert rejected["status"] == "rejected"
    assert rejected["rejected_by_user_id"] == setup.owner.id
    assert rejected["rejected_at"] is not None
    assert rejected["rejection_reason"] == "Priya already got a quote yesterday."
    assert rejected["approved_by_user_id"] is None


def test_rejecting_does_not_require_a_reason(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()

    response = client.post(f"{setup.actions}/{action['id']}/reject", cookies=setup.owner.cookies)

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert response.json()["rejection_reason"] is None


def test_an_action_cannot_be_approved_twice(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()
    client.post(f"{setup.actions}/{action['id']}/approve", cookies=setup.owner.cookies)

    again = client.post(f"{setup.actions}/{action['id']}/approve", cookies=setup.owner.cookies)

    assert again.status_code == 409
    error = again.json()["error"]
    assert error["code"] == "invalid_state_transition"
    assert error["details"] == {"current_status": "approved", "requested_status": "approved"}


@pytest.mark.parametrize(
    ("first", "second"), [("approve", "reject"), ("reject", "approve"), ("reject", "reject")]
)
def test_a_decided_action_cannot_be_decided_again(
    client: TestClient,
    propose: Callable[..., dict[str, Any]],
    setup: Setup,
    first: str,
    second: str,
) -> None:
    action = propose()
    assert (
        client.post(
            f"{setup.actions}/{action['id']}/{first}", cookies=setup.owner.cookies
        ).status_code
        == 200
    )

    response = client.post(f"{setup.actions}/{action['id']}/{second}", cookies=setup.owner.cookies)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state_transition"


def test_a_failed_decision_leaves_the_action_untouched(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()
    client.post(
        f"{setup.actions}/{action['id']}/reject", json={"reason": "No"}, cookies=setup.owner.cookies
    )

    client.post(f"{setup.actions}/{action['id']}/approve", cookies=setup.owner.cookies)

    current = client.get(f"{setup.actions}/{action['id']}", cookies=setup.owner.cookies).json()
    assert current["status"] == "rejected"
    assert current["approved_by_user_id"] is None


def test_deciding_on_an_unknown_action_is_a_404(client: TestClient, setup: Setup) -> None:
    response = client.post(f"{setup.actions}/{uuid.uuid4()}/approve", cookies=setup.owner.cookies)

    assert response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"type": "wire_money", "summary": "Not a known action type"},
        {"type": "send_email", "summary": ""},
        {"type": "send_email"},
        {"type": "send_email", "summary": "x", "payload": "not-an-object"},
        {"type": "send_email", "summary": "x", "status": "approved"},
    ],
    ids=[
        "unknown-type",
        "empty-summary",
        "missing-summary",
        "payload-not-object",
        "client-sets-status",
    ],
)
def test_invalid_proposals_are_rejected(
    client: TestClient, setup: Setup, body: dict[str, Any]
) -> None:
    response = client.post(setup.actions, json=body, cookies=setup.owner.cookies)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert client.get(setup.actions, cookies=setup.owner.cookies).json() == []


def test_actions_can_be_filtered_by_status(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    kept = propose(summary="Stays proposed")
    approved = propose(summary="Gets approved")
    client.post(f"{setup.actions}/{approved['id']}/approve", cookies=setup.owner.cookies)

    proposed_only = client.get(
        f"{setup.actions}?status=proposed", cookies=setup.owner.cookies
    ).json()
    everything = client.get(setup.actions, cookies=setup.owner.cookies).json()
    bad_filter = client.get(f"{setup.actions}?status=bogus", cookies=setup.owner.cookies)

    assert [a["id"] for a in proposed_only] == [kept["id"]]
    assert {a["id"] for a in everything} == {kept["id"], approved["id"]}
    assert bad_filter.status_code == 422


def test_each_step_is_recorded_in_the_activity_feed(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()
    client.post(f"{setup.actions}/{action['id']}/approve", cookies=setup.owner.cookies)

    feed = client.get(
        f"/v1/workspaces/{setup.workspace_id}/activity?entity_type=action&entity_id={action['id']}",
        cookies=setup.owner.cookies,
    ).json()

    assert [event["event_type"] for event in feed] == ["action.approved", "action.proposed"]
    assert feed[0]["actor_user_id"] == setup.owner.id
    assert feed[0]["data"] == {"from_status": "proposed", "to_status": "approved"}


def test_a_lost_decision_does_not_write_an_activity_event(
    client: TestClient, propose: Callable[..., dict[str, Any]], setup: Setup
) -> None:
    action = propose()
    client.post(f"{setup.actions}/{action['id']}/approve", cookies=setup.owner.cookies)
    client.post(f"{setup.actions}/{action['id']}/approve", cookies=setup.owner.cookies)  # 409

    feed = client.get(
        f"/v1/workspaces/{setup.workspace_id}/activity?entity_id={action['id']}",
        cookies=setup.owner.cookies,
    ).json()

    assert [event["event_type"] for event in feed].count("action.approved") == 1


class TestExecutorTransitions:
    """`start_execution`, `complete` and `fail` are for Nerva's executor (not exposed via
    HTTP). Exercised here through the service, since v0.1 has no executor yet."""

    @pytest.fixture
    def service(self, db_session: Session, setup: Setup) -> ActionService:
        ctx = WorkspaceContext(
            workspace_id=uuid.UUID(setup.workspace_id),
            user_id=uuid.UUID(setup.owner.id),
            role=MemberRole.OWNER,
        )
        return ActionService(db_session, ctx)

    @pytest.fixture
    def proposed_id(self, service: ActionService) -> uuid.UUID:
        return service.propose(
            ActionCreate.model_validate({"type": "create_task", "summary": "Call Tomás"})
        ).id

    def test_the_happy_path_runs_through_every_state(
        self, service: ActionService, proposed_id: uuid.UUID
    ) -> None:
        service.approve(proposed_id)
        executing = service.start_execution(proposed_id)
        assert executing.status is ActionStatus.EXECUTING
        assert executing.started_at is not None

        done = service.complete(proposed_id, result={"task_id": "t_123"})

        assert done.status is ActionStatus.COMPLETED
        assert done.result == {"task_id": "t_123"}
        assert done.finished_at is not None

    def test_a_failure_keeps_the_error(
        self, service: ActionService, proposed_id: uuid.UUID
    ) -> None:
        service.approve(proposed_id)
        service.start_execution(proposed_id)

        failed = service.fail(proposed_id, error="Mailbox quota exceeded")

        assert failed.status is ActionStatus.FAILED
        assert failed.result == {"error": "Mailbox quota exceeded"}
        assert failed.finished_at is not None

    def test_nothing_executes_without_approval(
        self, service: ActionService, proposed_id: uuid.UUID
    ) -> None:
        with pytest.raises(InvalidStateTransitionError):
            service.start_execution(proposed_id)

        assert service.get(proposed_id).status is ActionStatus.PROPOSED

    def test_a_rejected_action_can_never_execute(
        self, service: ActionService, proposed_id: uuid.UUID
    ) -> None:
        service.reject(proposed_id, reason="Not now")

        with pytest.raises(InvalidStateTransitionError):
            service.start_execution(proposed_id)

    def test_completion_requires_that_execution_started(
        self, service: ActionService, proposed_id: uuid.UUID
    ) -> None:
        service.approve(proposed_id)

        with pytest.raises(InvalidStateTransitionError):
            service.complete(proposed_id)

    @pytest.mark.parametrize("terminal", ["complete", "fail"])
    def test_finished_actions_are_final(
        self, service: ActionService, proposed_id: uuid.UUID, terminal: str
    ) -> None:
        service.approve(proposed_id)
        service.start_execution(proposed_id)
        if terminal == "complete":
            service.complete(proposed_id)
        else:
            service.fail(proposed_id, error="x")

        with pytest.raises(InvalidStateTransitionError):
            service.start_execution(proposed_id)
        with pytest.raises(InvalidStateTransitionError):
            service.approve(proposed_id)

    def test_executor_steps_are_attributed_to_nerva_not_the_approver(
        self, service: ActionService, proposed_id: uuid.UUID
    ) -> None:
        service.approve(proposed_id)
        service.start_execution(proposed_id)
        service.complete(proposed_id)

        events = service.activity.list(entity_type="action", entity_id=proposed_id)

        by_type = {event.event_type: event for event in events}
        assert by_type["action.approved"].actor_type is ActorType.USER
        assert by_type["action.executing"].actor_type is ActorType.NERVA
        assert by_type["action.executing"].actor_user_id is None
        assert by_type["action.completed"].actor_type is ActorType.NERVA

    def test_unknown_actions_are_not_found(self, service: ActionService) -> None:
        with pytest.raises(NotFoundError):
            service.approve(uuid.uuid4())
