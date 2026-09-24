"""Multi-tenancy: one workspace's data must never be visible to, or linkable from,
another workspace, and a `workspace_id` from the client is never trusted."""

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.enums import ActorType
from nerva.models import Company, Contact, Opportunity, Workspace
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]
Post = Callable[..., dict[str, Any]]


@dataclass
class Tenant:
    owner: Account
    workspace_id: str
    company_id: str
    contact_id: str
    opportunity_id: str
    action_id: str

    @property
    def base(self) -> str:
        return f"/v1/workspaces/{self.workspace_id}"


@pytest.fixture
def make_tenant(
    make_user: MakeUser, make_workspace: MakeWorkspace, post: Post
) -> Callable[[str], Tenant]:
    def _make(name: str) -> Tenant:
        owner = make_user(name)
        workspace_id = make_workspace(owner, f"{name} Co")
        base = f"/v1/workspaces/{workspace_id}"
        company = post(
            owner, f"{base}/companies", {"name": f"{name} Customer", "domain": "acme.io"}
        )
        contact = post(
            owner, f"{base}/contacts", {"email": "buyer@acme.io", "company_id": company["id"]}
        )
        opportunity = post(
            owner, f"{base}/opportunities", {"title": "Annual plan", "contact_id": contact["id"]}
        )
        action = post(
            owner,
            f"{base}/actions",
            {
                "type": "send_email",
                "summary": "Follow up with buyer",
                "opportunity_id": opportunity["id"],
            },
        )
        return Tenant(
            owner, workspace_id, company["id"], contact["id"], opportunity["id"], action["id"]
        )

    return _make


@pytest.fixture
def alice_tenant(make_tenant: Callable[[str], Tenant]) -> Tenant:
    return make_tenant("Alice")


@pytest.fixture
def bob_tenant(make_tenant: Callable[[str], Tenant]) -> Tenant:
    return make_tenant("Bob")


COLLECTIONS = ["companies", "contacts", "opportunities", "actions", "activity"]


@pytest.mark.parametrize("collection", COLLECTIONS)
def test_non_members_cannot_list_another_workspaces_data(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant, collection: str
) -> None:
    response = client.get(f"{alice_tenant.base}/{collection}", cookies=bob_tenant.owner.cookies)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize("collection", ["companies", "contacts", "opportunities", "actions"])
def test_non_members_cannot_create_in_another_workspace(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant, collection: str
) -> None:
    bodies = {
        "companies": {"name": "Intruder"},
        "contacts": {"email": "intruder@example.com"},
        "opportunities": {"title": "Intruder"},
        "actions": {"type": "send_email", "summary": "Intruder"},
    }
    before = client.get(
        f"{alice_tenant.base}/{collection}", cookies=alice_tenant.owner.cookies
    ).json()

    response = client.post(
        f"{alice_tenant.base}/{collection}",
        json=bodies[collection],
        cookies=bob_tenant.owner.cookies,
    )

    after = client.get(
        f"{alice_tenant.base}/{collection}", cookies=alice_tenant.owner.cookies
    ).json()
    assert response.status_code == 404
    assert after == before


def test_ids_from_another_workspace_cannot_be_read_through_your_own(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    cookies = bob_tenant.owner.cookies
    foreign = {
        "companies": alice_tenant.company_id,
        "contacts": alice_tenant.contact_id,
        "opportunities": alice_tenant.opportunity_id,
        "actions": alice_tenant.action_id,
    }

    for collection, foreign_id in foreign.items():
        response = client.get(f"{bob_tenant.base}/{collection}/{foreign_id}", cookies=cookies)
        assert response.status_code == 404, f"{collection} leaked across workspaces"


def test_lists_only_contain_the_workspaces_own_data(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    for collection, own_id in {
        "contacts": bob_tenant.contact_id,
        "opportunities": bob_tenant.opportunity_id,
        "actions": bob_tenant.action_id,
    }.items():
        items = client.get(
            f"{bob_tenant.base}/{collection}", cookies=bob_tenant.owner.cookies
        ).json()
        assert [item["id"] for item in items] == [own_id]
        assert {item["workspace_id"] for item in items} == {bob_tenant.workspace_id}


def test_the_activity_feed_never_mixes_workspaces(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    events = client.get(f"{bob_tenant.base}/activity", cookies=bob_tenant.owner.cookies).json()

    assert events
    assert {event["workspace_id"] for event in events} == {bob_tenant.workspace_id}
    assert alice_tenant.action_id not in {event["entity_id"] for event in events}


def test_a_non_member_cannot_decide_on_another_workspaces_action(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    path = f"{alice_tenant.base}/actions/{alice_tenant.action_id}"

    approve = client.post(f"{path}/approve", cookies=bob_tenant.owner.cookies)
    reject = client.post(f"{path}/reject", cookies=bob_tenant.owner.cookies)

    assert approve.status_code == reject.status_code == 404
    unchanged = client.get(path, cookies=alice_tenant.owner.cookies).json()
    assert unchanged["status"] == "proposed"
    assert unchanged["approved_by_user_id"] is None


def test_the_action_id_of_another_workspace_cannot_be_decided_via_your_own_path(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    # Bob is authorized for his own workspace, but names Alice's action id.
    response = client.post(
        f"{bob_tenant.base}/actions/{alice_tenant.action_id}/approve",
        cookies=bob_tenant.owner.cookies,
    )

    assert response.status_code == 404
    unchanged = client.get(
        f"{alice_tenant.base}/actions/{alice_tenant.action_id}", cookies=alice_tenant.owner.cookies
    ).json()
    assert unchanged["status"] == "proposed"


@pytest.mark.parametrize(
    ("collection", "body"),
    [
        ("companies", {"name": "Sneaky"}),
        ("contacts", {"email": "sneaky@example.com"}),
        ("opportunities", {"title": "Sneaky"}),
        ("actions", {"type": "send_email", "summary": "Sneaky"}),
    ],
)
def test_a_workspace_id_in_the_request_body_is_rejected_not_trusted(
    client: TestClient,
    alice_tenant: Tenant,
    bob_tenant: Tenant,
    collection: str,
    body: dict[str, Any],
) -> None:
    # Bob tries to plant a record in Alice's workspace by naming it in the body of a
    # request to his own workspace.
    response = client.post(
        f"{bob_tenant.base}/{collection}",
        json={**body, "workspace_id": alice_tenant.workspace_id},
        cookies=bob_tenant.owner.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    for tenant in (alice_tenant, bob_tenant):
        listed = client.get(f"{tenant.base}/{collection}", cookies=tenant.owner.cookies).json()
        assert "sneaky" not in json.dumps(listed).lower()


def test_records_cannot_reference_entities_of_another_workspace(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    cookies = bob_tenant.owner.cookies
    attempts = [
        ("contacts", {"email": "x@example.com", "company_id": alice_tenant.company_id}),
        ("opportunities", {"title": "X", "contact_id": alice_tenant.contact_id}),
        ("opportunities", {"title": "X", "company_id": alice_tenant.company_id}),
        (
            "actions",
            {"type": "send_email", "summary": "X", "opportunity_id": alice_tenant.opportunity_id},
        ),
    ]

    for collection, body in attempts:
        response = client.post(f"{bob_tenant.base}/{collection}", json=body, cookies=cookies)
        assert response.status_code == 422, (collection, body)
        assert response.json()["error"]["code"] == "invalid_reference"


def test_the_same_contact_email_can_exist_in_different_workspaces(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    # Both tenants already have buyer@acme.io (see make_tenant): uniqueness is per workspace.
    duplicate_in_same_workspace = client.post(
        f"{bob_tenant.base}/contacts",
        json={"email": "buyer@acme.io"},
        cookies=bob_tenant.owner.cookies,
    )

    assert duplicate_in_same_workspace.status_code == 409
    assert alice_tenant.contact_id != bob_tenant.contact_id


def test_a_user_in_two_workspaces_sees_each_one_separately(
    client: TestClient, alice_tenant: Tenant, bob_tenant: Tenant
) -> None:
    # Give Alice a second workspace of her own; her data stays partitioned by workspace.
    other = client.post(
        "/v1/workspaces", json={"name": "Alice Side Project"}, cookies=alice_tenant.owner.cookies
    ).json()

    in_other = client.get(
        f"/v1/workspaces/{other['id']}/contacts", cookies=alice_tenant.owner.cookies
    ).json()
    in_first = client.get(
        f"{alice_tenant.base}/contacts", cookies=alice_tenant.owner.cookies
    ).json()

    assert in_other == []
    assert len(in_first) == 1


class TestDatabaseEnforcesTenantBoundaries:
    """Defense in depth: even a buggy service cannot link rows across workspaces,
    because the composite foreign keys reject it in PostgreSQL itself."""

    def _workspaces(self, db_session: Session) -> tuple[Workspace, Workspace]:
        first, second = Workspace(name="One", slug="one"), Workspace(name="Two", slug="two")
        db_session.add_all([first, second])
        db_session.flush()
        return first, second

    def test_contact_cannot_point_at_a_company_in_another_workspace(
        self, db_session: Session
    ) -> None:
        first, second = self._workspaces(db_session)
        company = Company(workspace_id=first.id, name="Acme")
        db_session.add(company)
        db_session.flush()

        db_session.add(Contact(workspace_id=second.id, email="a@b.co", company_id=company.id))

        with pytest.raises(IntegrityError, match="fk_contacts"):
            db_session.flush()

    def test_opportunity_cannot_point_at_a_contact_in_another_workspace(
        self, db_session: Session
    ) -> None:
        first, second = self._workspaces(db_session)
        contact = Contact(workspace_id=first.id, email="a@b.co")
        db_session.add(contact)
        db_session.flush()

        db_session.add(
            Opportunity(
                workspace_id=second.id,
                title="Cross-tenant",
                contact_id=contact.id,
                created_by_type=ActorType.NERVA,
            )
        )

        with pytest.raises(IntegrityError, match="fk_opportunities"):
            db_session.flush()

    def test_a_reference_within_the_same_workspace_is_accepted(self, db_session: Session) -> None:
        first, _ = self._workspaces(db_session)
        company = Company(workspace_id=first.id, name="Acme")
        db_session.add(company)
        db_session.flush()

        db_session.add(Contact(workspace_id=first.id, email="a@b.co", company_id=company.id))
        db_session.flush()  # does not raise

    def test_a_random_company_id_is_rejected(self, db_session: Session) -> None:
        first, _ = self._workspaces(db_session)

        db_session.add(Contact(workspace_id=first.id, email="a@b.co", company_id=uuid.uuid4()))

        with pytest.raises(IntegrityError):
            db_session.flush()
