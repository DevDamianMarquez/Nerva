from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from nerva.core.security import SESSION_COOKIE_NAME
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


@pytest.mark.parametrize(
    "cookies",
    [
        {},
        {SESSION_COOKIE_NAME: "not-a-real-token"},
        {SESSION_COOKIE_NAME: "0" * 43},
    ],
    ids=["missing", "malformed", "unknown-session"],
)
def test_workspace_routes_require_a_known_session(
    client: TestClient, cookies: dict[str, str]
) -> None:
    response = client.get("/v1/workspaces", cookies=cookies)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_creating_a_workspace_makes_the_creator_its_owner(
    client: TestClient, make_user: MakeUser
) -> None:
    alice = make_user("Alice")

    response = client.post(
        "/v1/workspaces", json={"name": "Northwind Studio"}, cookies=alice.cookies
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Northwind Studio"
    assert body["slug"] == "northwind-studio"
    assert body["role"] == "owner"
    members = client.get(f"/v1/workspaces/{body['id']}/members", cookies=alice.cookies).json()
    assert [(m["user_id"], m["role"]) for m in members] == [(alice.id, "owner")]


def test_workspaces_with_the_same_name_get_distinct_slugs(
    make_user: MakeUser, client: TestClient
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")

    first = client.post("/v1/workspaces", json={"name": "Acme"}, cookies=alice.cookies).json()
    second = client.post("/v1/workspaces", json={"name": "Acme"}, cookies=bob.cookies).json()

    assert first["slug"] == "acme"
    assert second["slug"].startswith("acme-")
    assert first["slug"] != second["slug"]


def test_listing_workspaces_only_returns_those_the_user_belongs_to(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_ws = make_workspace(alice, "Alice Co")
    make_workspace(bob, "Bob Co")

    listed = client.get("/v1/workspaces", cookies=alice.cookies).json()
    me = client.get("/v1/auth/me", cookies=alice.cookies).json()

    assert [ws["id"] for ws in listed] == [alice_ws]
    assert me["user"]["id"] == alice.id
    assert [ws["id"] for ws in me["workspaces"]] == [alice_ws]


def test_reading_a_workspace_you_do_not_belong_to_looks_like_it_does_not_exist(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, mallory = make_user("Alice"), make_user("Mallory")
    workspace_id = make_workspace(alice)

    response = client.get(f"/v1/workspaces/{workspace_id}", cookies=mallory.cookies)
    unknown = client.get(
        "/v1/workspaces/00000000-0000-0000-0000-000000000000", cookies=mallory.cookies
    )

    # Same status and body as a workspace that does not exist: no way to probe for tenants.
    assert response.status_code == 404
    assert response.json() == unknown.json()


def test_workspace_name_is_validated(client: TestClient, make_user: MakeUser) -> None:
    alice = make_user("Alice")

    response = client.post("/v1/workspaces", json={"name": "   "}, cookies=alice.cookies)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
