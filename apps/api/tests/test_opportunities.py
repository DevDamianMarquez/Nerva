from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]
Post = Callable[..., dict[str, Any]]


@dataclass
class Setup:
    owner: Account
    workspace_id: str

    def url(self, collection: str) -> str:
        return f"/v1/workspaces/{self.workspace_id}/{collection}"


@pytest.fixture
def setup(make_user: MakeUser, make_workspace: MakeWorkspace) -> Setup:
    owner = make_user("Alice")
    return Setup(owner, make_workspace(owner, "Northwind"))


def test_an_opportunity_can_be_created_with_just_a_title(client: TestClient, setup: Setup) -> None:
    response = client.post(
        setup.url("opportunities"),
        json={"title": "Halcyon Freight: 3 seats"},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["title"] == "Halcyon Freight: 3 seats"
    assert body["stage"] == "new"
    assert body["currency"] == "USD"
    assert body["estimated_value_cents"] is None
    assert body["workspace_id"] == setup.workspace_id
    assert body["contact_id"] is None


def test_an_opportunity_can_link_a_contact_and_company_from_its_workspace(
    client: TestClient, setup: Setup, post: Post
) -> None:
    company = post(
        setup.owner,
        setup.url("companies"),
        {"name": "Halcyon Freight", "domain": "HALCYON.example"},
    )
    contact = post(
        setup.owner,
        setup.url("contacts"),
        {"email": "Priya.Nair@halcyon.example", "name": "Priya Nair", "company_id": company["id"]},
    )

    response = client.post(
        setup.url("opportunities"),
        json={
            "title": "Halcyon Freight: 3 seats",
            "summary": "Asked for pricing on three seats and an annual discount.",
            "stage": "qualified",
            "estimated_value_cents": 1_800_000,
            "currency": "EUR",
            "contact_id": contact["id"],
            "company_id": company["id"],
        },
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 201
    body = response.json()
    assert (body["stage"], body["estimated_value_cents"], body["currency"]) == (
        "qualified",
        1_800_000,
        "EUR",
    )
    assert (body["contact_id"], body["company_id"]) == (contact["id"], company["id"])
    assert company["domain"] == "halcyon.example"
    assert contact["email"] == "priya.nair@halcyon.example"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"title": ""},
        {"title": "x" * 201},
        {"title": "Deal", "stage": "negotiating"},
        {"title": "Deal", "estimated_value_cents": -1},
        {"title": "Deal", "currency": "usd"},
        {"title": "Deal", "contact_id": "not-a-uuid"},
    ],
    ids=[
        "empty-body",
        "empty-title",
        "title-too-long",
        "bad-stage",
        "negative-value",
        "bad-currency",
        "bad-uuid",
    ],
)
def test_invalid_opportunities_are_rejected_with_details(
    client: TestClient, setup: Setup, body: dict[str, Any]
) -> None:
    response = client.post(setup.url("opportunities"), json=body, cookies=setup.owner.cookies)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["details"], "validation errors should say which field is wrong"
    assert client.get(setup.url("opportunities"), cookies=setup.owner.cookies).json() == []


def test_an_unknown_contact_is_an_invalid_reference_not_a_server_error(
    client: TestClient, setup: Setup
) -> None:
    response = client.post(
        setup.url("opportunities"),
        json={"title": "Deal", "contact_id": "6f1c7ec5-93a5-4b0e-9c6a-0d6f9f7a7e11"},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"


def test_creating_an_opportunity_is_recorded_in_the_activity_feed(
    client: TestClient, setup: Setup, post: Post
) -> None:
    opportunity = post(setup.owner, setup.url("opportunities"), {"title": "Ledgerly renewal"})

    feed = client.get(
        setup.url("activity") + f"?entity_id={opportunity['id']}", cookies=setup.owner.cookies
    ).json()

    assert len(feed) == 1
    assert feed[0]["event_type"] == "opportunity.created"
    assert feed[0]["entity_type"] == "opportunity"
    assert feed[0]["actor_user_id"] == setup.owner.id
    assert "Ledgerly renewal" in feed[0]["summary"]


def test_opportunities_can_be_listed_filtered_and_paginated(
    client: TestClient, setup: Setup, post: Post
) -> None:
    for title, stage in [("A", "new"), ("B", "won"), ("C", "new")]:
        post(setup.owner, setup.url("opportunities"), {"title": title, "stage": stage})
    cookies = setup.owner.cookies

    everything = client.get(setup.url("opportunities"), cookies=cookies).json()
    only_new = client.get(setup.url("opportunities") + "?stage=new", cookies=cookies).json()
    first_page = client.get(setup.url("opportunities") + "?limit=2", cookies=cookies).json()
    second_page = client.get(
        setup.url("opportunities") + "?limit=2&offset=2", cookies=cookies
    ).json()

    assert [o["title"] for o in everything] == ["C", "B", "A"]  # newest first
    assert sorted(o["title"] for o in only_new) == ["A", "C"]
    assert [o["title"] for o in first_page + second_page] == ["C", "B", "A"]
    assert client.get(setup.url("opportunities") + "?limit=0", cookies=cookies).status_code == 422
    assert client.get(setup.url("opportunities") + "?limit=101", cookies=cookies).status_code == 422


def test_an_opportunity_can_be_fetched_by_id(client: TestClient, setup: Setup, post: Post) -> None:
    created = post(setup.owner, setup.url("opportunities"), {"title": "Fetch me"})

    found = client.get(
        setup.url("opportunities") + f"/{created['id']}", cookies=setup.owner.cookies
    )
    missing = client.get(
        setup.url("opportunities") + "/6f1c7ec5-93a5-4b0e-9c6a-0d6f9f7a7e11",
        cookies=setup.owner.cookies,
    )

    assert found.status_code == 200
    assert found.json() == created
    assert missing.status_code == 404


def test_contacts_reject_duplicate_emails_within_a_workspace(
    client: TestClient, setup: Setup, post: Post
) -> None:
    post(setup.owner, setup.url("contacts"), {"email": "Tomas@Ledgerly.example"})

    duplicate = client.post(
        setup.url("contacts"), json={"email": "tomas@ledgerly.example"}, cookies=setup.owner.cookies
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


def test_contact_email_must_be_valid(client: TestClient, setup: Setup) -> None:
    response = client.post(
        setup.url("contacts"), json={"email": "not-an-email"}, cookies=setup.owner.cookies
    )

    assert response.status_code == 422
