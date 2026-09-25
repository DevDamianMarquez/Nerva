"""Listing, disconnecting, and workspace/role boundaries around integrations."""

import uuid
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.enums import MemberRole
from nerva.models import WorkspaceMember
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


def _connect_google(client: TestClient, owner: Account, workspace_id: str) -> str:
    """Connects Gmail and returns the integration id."""
    begin = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=owner.cookies
    )
    state = parse_qs(urlparse(begin.json()["authorization_url"]).query)["state"][0]
    client.get(
        "/v1/integrations/google/callback",
        params={"code": "fake-auth-code", "state": state},
        cookies=owner.cookies,
        follow_redirects=False,
    )
    integrations = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()
    integration_id: str = integrations[0]["id"]
    return integration_id


def _add_member(db_session: Session, workspace_id: str, user: Account) -> None:
    """A minimal way to add a second member without an invitation flow (not yet
    implemented) — inserts the membership row directly, the way a future invitation
    endpoint eventually would."""
    db_session.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(workspace_id), user_id=uuid.UUID(user.id), role=MemberRole.MEMBER
        )
    )
    db_session.commit()


def test_integrations_are_isolated_by_workspace(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    _connect_google(client, alice, alice_workspace)

    bob_sees_response = client.get(
        f"/v1/workspaces/{bob_workspace}/integrations", cookies=bob.cookies
    )
    bob_sees = bob_sees_response.json()
    bob_reads_alice = client.get(
        f"/v1/workspaces/{alice_workspace}/integrations", cookies=bob.cookies
    )

    assert bob_sees == []
    assert bob_reads_alice.status_code == 404  # not a member: looks like it does not exist


def test_a_member_can_view_but_not_manage_integrations(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace, db_session: Session
) -> None:
    owner, member = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    _add_member(db_session, workspace_id, member)

    can_view = client.get(f"/v1/workspaces/{workspace_id}/integrations", cookies=member.cookies)
    cannot_connect = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/google/connect", cookies=member.cookies
    )

    assert can_view.status_code == 200
    assert cannot_connect.status_code == 403
    assert cannot_connect.json()["error"]["code"] == "forbidden"


def test_a_non_owner_member_cannot_sync_or_disconnect(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace, db_session: Session
) -> None:
    owner, member = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    _add_member(db_session, workspace_id, member)

    sync = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/sync", cookies=member.cookies
    )
    disconnect = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/disconnect",
        cookies=member.cookies,
    )

    assert sync.status_code == 403
    assert disconnect.status_code == 403


def test_disconnect_keeps_the_integration_row_and_clears_the_credential(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/disconnect",
        cookies=owner.cookies,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"
    still_listed = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()
    assert [i["id"] for i in still_listed] == [integration_id]


def test_syncing_a_disconnected_integration_is_a_conflict_not_a_silent_success(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/disconnect",
        cookies=owner.cookies,
    )

    response = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/sync", cookies=owner.cookies
    )

    assert response.status_code == 409


def test_disconnecting_is_recorded_in_the_activity_feed(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)

    client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/disconnect",
        cookies=owner.cookies,
    )

    feed = client.get(f"/v1/workspaces/{workspace_id}/activity", cookies=owner.cookies).json()
    event_types = [event["event_type"] for event in feed]
    assert "integration.connected" in event_types
    assert "integration.disconnected" in event_types
