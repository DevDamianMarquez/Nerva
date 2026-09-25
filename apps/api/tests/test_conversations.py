"""Reading conversations (Nerva Inbox): empty state, not-found, tenant isolation."""

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient

from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]


def test_an_empty_workspace_has_no_conversations(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    response = client.get(f"/v1/workspaces/{workspace_id}/conversations", cookies=owner.cookies)

    assert response.status_code == 200
    assert response.json() == []


def test_an_unknown_conversation_is_a_404(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    response = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/{uuid.uuid4()}", cookies=owner.cookies
    )

    assert response.status_code == 404


def test_a_non_member_cannot_read_another_workspaces_conversations(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, mallory = make_user("Alice"), make_user("Mallory")
    workspace_id = make_workspace(alice)

    response = client.get(f"/v1/workspaces/{workspace_id}/conversations", cookies=mallory.cookies)

    assert response.status_code == 404
