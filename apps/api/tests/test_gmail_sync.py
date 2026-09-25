"""Gmail synchronization: mapping, idempotency, bounds, and failure handling.

`gmail_client` (the `FakeGmailClient` fixture) stands in for the Gmail API — no real
mailbox is ever involved.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from tests.fakes.google import FakeGmailClient, make_gmail_message
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _connect_google(client: TestClient, owner: Account, workspace_id: str) -> str:
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


def _sync(
    client: TestClient, owner: Account, workspace_id: str, integration_id: str
) -> dict[str, object]:
    response = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/sync", cookies=owner.cookies
    )
    assert response.status_code == 200, response.text
    body: dict[str, object] = response.json()
    return body


def test_an_initial_sync_creates_conversations_and_messages(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    gmail_client.messages["m1"] = make_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="Priya Nair <priya@halcyon.example>",
        to="alex@gmail.example",
        subject="Pricing for 3 seats",
        sent_at=NOW,
        body_text="Hi Alex, can you send pricing for 3 seats?",
    )

    result = _sync(client, owner, workspace_id, integration_id)

    assert result == {
        "threads_seen": 1,
        "conversations_created": 1,
        "conversations_updated": 0,
        "messages_created": 1,
    }
    conversations = client.get(
        f"/v1/workspaces/{workspace_id}/conversations", cookies=owner.cookies
    ).json()
    assert len(conversations) == 1
    assert conversations[0]["subject"] == "Pricing for 3 seats"
    assert "pricing for 3 seats" in conversations[0]["preview"].lower()


def test_running_sync_twice_does_not_duplicate_anything(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    gmail_client.messages["m1"] = make_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="priya@halcyon.example",
        to="alex@gmail.example",
        subject="Pricing",
        sent_at=NOW,
        body_text="Hello",
    )

    first = _sync(client, owner, workspace_id, integration_id)
    second = _sync(client, owner, workspace_id, integration_id)

    assert first["messages_created"] == 1
    assert second["messages_created"] == 0
    assert second["conversations_created"] == 0
    conversations = client.get(
        f"/v1/workspaces/{workspace_id}/conversations", cookies=owner.cookies
    ).json()
    assert len(conversations) == 1


def test_a_second_message_in_the_same_thread_updates_the_conversation(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    gmail_client.messages["m1"] = make_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="priya@halcyon.example",
        to="alex@gmail.example",
        subject="Pricing",
        sent_at=NOW,
        body_text="Question one",
    )

    _sync(client, owner, workspace_id, integration_id)
    gmail_client.messages["m2"] = make_gmail_message(
        message_id="m2",
        thread_id="t1",
        sender="alex@gmail.example",
        to="priya@halcyon.example",
        subject="Re: Pricing",
        sent_at=NOW + timedelta(hours=1),
        body_text="Answer",
    )
    result = _sync(client, owner, workspace_id, integration_id)

    assert result == {
        "threads_seen": 1,
        "conversations_created": 0,
        "conversations_updated": 1,
        "messages_created": 1,
    }
    conversations = client.get(
        f"/v1/workspaces/{workspace_id}/conversations", cookies=owner.cookies
    ).json()
    assert len(conversations) == 1
    conversation_id = conversations[0]["id"]
    detail = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}", cookies=owner.cookies
    ).json()
    assert [m["direction"] for m in detail["messages"]] == ["inbound", "outbound"]
    assert [m["body_text"] for m in detail["messages"]] == ["Question one", "Answer"]


def test_multipart_messages_prefer_the_plain_text_part(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    gmail_client.messages["m1"] = make_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="priya@halcyon.example",
        to="alex@gmail.example",
        subject="Multipart",
        sent_at=NOW,
        body_text="Plain text version",
        body_html="<p>HTML <b>version</b></p>",
    )

    _sync(client, owner, workspace_id, integration_id)

    conversation_id = client.get(
        f"/v1/workspaces/{workspace_id}/conversations", cookies=owner.cookies
    ).json()[0]["id"]
    detail = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}", cookies=owner.cookies
    ).json()
    assert detail["messages"][0]["body_text"] == "Plain text version"


def test_html_only_messages_are_converted_to_safe_plain_text(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    gmail_client.messages["m1"] = make_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="priya@halcyon.example",
        to="alex@gmail.example",
        subject="HTML only",
        sent_at=NOW,
        body_html="<div>Hello <script>alert('x')</script><b>Priya</b></div><p>Second line</p>",
    )

    _sync(client, owner, workspace_id, integration_id)

    conversation_id = client.get(
        f"/v1/workspaces/{workspace_id}/conversations", cookies=owner.cookies
    ).json()[0]["id"]
    detail = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}", cookies=owner.cookies
    ).json()
    body = detail["messages"][0]["body_text"]
    assert "Hello" in body
    assert "Priya" in body
    assert "Second line" in body
    assert "<" not in body  # no raw HTML reaches storage/display
    assert "alert" not in body  # script contents are dropped, not just untagged


def test_sync_respects_the_message_cap(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    """`FakeGmailClient.list_recent_message_ids` truncates to `max_results` exactly
    like the real bound in `GmailSyncService.MAX_MESSAGES_PER_SYNC`; this proves the
    sync call actually threads that cap through."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    for i in range(250):
        gmail_client.messages[f"m{i}"] = make_gmail_message(
            message_id=f"m{i}",
            thread_id=f"t{i}",
            sender="priya@halcyon.example",
            to="alex@gmail.example",
            subject=f"Message {i}",
            sent_at=NOW,
            body_text="x",
        )

    result = _sync(client, owner, workspace_id, integration_id)

    assert result["messages_created"] == 200  # MAX_MESSAGES_PER_SYNC, not 250


def test_a_provider_failure_does_not_claim_success(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    gmail_client.fail_list = True

    response = client.post(
        f"/v1/workspaces/{workspace_id}/integrations/{integration_id}/sync", cookies=owner.cookies
    )

    assert response.status_code == 409
    assert response.json()["error"]["details"] == {"code": "sync_failed"}
    integration = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()[0]
    assert integration["status"] == "error"
    assert integration["last_synced_at"] is None  # never claimed a sync happened


def test_last_synced_at_is_set_only_after_a_successful_sync(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    before = client.get(
        f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies
    ).json()[0]
    assert before["last_synced_at"] is None

    _sync(client, owner, workspace_id, integration_id)

    response = client.get(f"/v1/workspaces/{workspace_id}/integrations", cookies=owner.cookies)
    after = response.json()[0]
    assert after["last_synced_at"] is not None


def test_syncing_finds_or_creates_a_contact_for_the_counterpart(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    gmail_client.messages["m1"] = make_gmail_message(
        message_id="m1",
        thread_id="t1",
        sender="Priya Nair <priya@halcyon.example>",
        to="alex@gmail.example",
        subject="Pricing",
        sent_at=NOW,
        body_text="Hi",
    )

    _sync(client, owner, workspace_id, integration_id)

    contacts = client.get(f"/v1/workspaces/{workspace_id}/contacts", cookies=owner.cookies).json()
    assert [c["email"] for c in contacts] == ["priya@halcyon.example"]
    conversation = client.get(
        f"/v1/workspaces/{workspace_id}/conversations", cookies=owner.cookies
    ).json()[0]
    assert conversation["contact_id"] == contacts[0]["id"]


def test_syncing_is_recorded_once_in_the_activity_feed_not_per_message(
    client: TestClient,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    gmail_client: FakeGmailClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    integration_id = _connect_google(client, owner, workspace_id)
    for i in range(5):
        gmail_client.messages[f"m{i}"] = make_gmail_message(
            message_id=f"m{i}",
            thread_id=f"t{i}",
            sender="priya@halcyon.example",
            to="alex@gmail.example",
            subject="Pricing",
            sent_at=NOW,
            body_text="Hi",
        )

    _sync(client, owner, workspace_id, integration_id)

    feed = client.get(f"/v1/workspaces/{workspace_id}/activity", cookies=owner.cookies).json()
    sync_events = [event for event in feed if event["event_type"] == "integration.synced"]
    assert len(sync_events) == 1
    assert sync_events[0]["data"]["messages_created"] == 5
