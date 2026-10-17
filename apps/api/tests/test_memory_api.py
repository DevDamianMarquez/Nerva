"""M6.2: the HTTP surface over `MemoryService` — record, list (with its
active-by-default policy), read, supersede, replace (M6.3 Block 2) and
discard. Mirrors
`test_signals_api.py` for the same reasons: real authentication, real tenant
isolation, no AI provider involved — every memory here is recorded explicitly
by the test, the same way a human or a future reasoning layer would call
`POST /memories`.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.enums import ConversationChannel, MemberRole
from nerva.models import Company, Contact, Conversation, WorkspaceMember
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

VALID_BODY = {
    "origin": "observed",
    "statement": "Acme's renewal is in March.",
    "observed_at": "2026-01-15T12:00:00Z",
}


@dataclass
class Setup:
    owner: Account
    workspace_id: str

    @property
    def memories(self) -> str:
        return f"/v1/workspaces/{self.workspace_id}/memories"


@pytest.fixture
def setup(make_user: MakeUser, make_workspace: MakeWorkspace) -> Setup:
    owner = make_user("Alice")
    return Setup(owner, make_workspace(owner, "Northwind"))


def _add_member(
    db_session: Session, workspace_id: str, user: Account, *, role: MemberRole = MemberRole.MEMBER
) -> None:
    db_session.add(
        WorkspaceMember(workspace_id=uuid.UUID(workspace_id), user_id=uuid.UUID(user.id), role=role)
    )
    db_session.commit()


def _contact(db_session: Session, workspace_id: str, email: str = "priya@halcyon.example") -> str:
    contact = Contact(workspace_id=uuid.UUID(workspace_id), email=email)
    db_session.add(contact)
    db_session.commit()
    return str(contact.id)


def _conversation(db_session: Session, workspace_id: str, subject: str = "Renewal") -> str:
    conversation = Conversation(
        workspace_id=uuid.UUID(workspace_id), channel=ConversationChannel.EMAIL, subject=subject
    )
    db_session.add(conversation)
    db_session.commit()
    return str(conversation.id)


def _remember(client: TestClient, setup: Setup, **overrides: object) -> dict[str, object]:
    body = VALID_BODY | overrides
    response = client.post(setup.memories, json=body, cookies=setup.owner.cookies)
    assert response.status_code == 201, response.text
    created: dict[str, object] = response.json()
    return created


# --- Record ------------------------------------------------------------------------


def test_a_member_can_record_a_manual_memory(client: TestClient, setup: Setup) -> None:
    created = _remember(client, setup)

    assert created["status"] == "active"
    assert created["kind"] == "fact"
    assert created["source_type"] == "manual"
    assert created["statement"] == VALID_BODY["statement"]
    assert created["workspace_id"] == setup.workspace_id


def test_recording_rejects_empty_content(client: TestClient, setup: Setup) -> None:
    """`MemoryCreate.statement` already mirrors `MemoryContent`'s own bounds (see
    `schemas.memory`'s module docstring), so this is caught by request validation
    before `MemoryService.remember` is ever called — still a 422, just the generic
    `validation_error` rather than `invalid_content`. `invalid_content` itself is
    exercised directly at the service layer
    (`test_memory_service.test_remembering_rejects_invalid_content`), since the
    schema leaves no input that reaches `remember` with a shape `MemoryContent`
    would reject."""
    response = client.post(
        setup.memories, json=VALID_BODY | {"statement": ""}, cookies=setup.owner.cookies
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_recording_an_observed_memory_with_confidence_is_a_clean_422(
    client: TestClient, setup: Setup
) -> None:
    """Would otherwise reach `MemoryService.remember` and fail only at `flush()`
    against `Memory`'s `confidence_only_when_inferred` CHECK — a raw, unhandled
    `IntegrityError` (500), not a clean validation error."""
    response = client.post(
        setup.memories, json=VALID_BODY | {"origin": "observed", "confidence": 0.9},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_recording_an_inferred_memory_with_no_confidence_is_a_clean_422(
    client: TestClient, setup: Setup
) -> None:
    response = client.post(
        setup.memories, json=VALID_BODY | {"origin": "inferred"}, cookies=setup.owner.cookies
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_recording_rejects_more_than_one_subject(
    client: TestClient, setup: Setup, db_session: Session
) -> None:
    """Security review (M6.2), fix: `MemoryCreate._at_most_one_subject` catches this
    before the request ever reaches `MemoryService.remember` — previously it flushed
    and hit `Memory`'s `at_most_one_subject` CHECK as a raw, unhandled 500.

    Both ids must genuinely exist in this workspace — an unknown id would otherwise
    raise the (already-handled) 422 `invalid_reference` from `_require_contact`/
    `_require_company` first and mask what this test targets: the *combination* is
    now rejected by request validation, before any reference lookup runs at all."""
    contact_id = _contact(db_session, setup.workspace_id)
    company = Company(workspace_id=uuid.UUID(setup.workspace_id), name="Halcyon")
    db_session.add(company)
    db_session.commit()

    body = VALID_BODY | {"contact_id": contact_id, "company_id": str(company.id)}
    response = client.post(setup.memories, json=body, cookies=setup.owner.cookies)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_recording_with_exactly_one_subject_still_succeeds(
    client: TestClient, setup: Setup, db_session: Session
) -> None:
    """The fix for the previous test must not reject the legitimate, single-subject
    case — each of the three subject fields on its own still works."""
    contact_id = _contact(db_session, setup.workspace_id)

    response = client.post(
        setup.memories, json=VALID_BODY | {"contact_id": contact_id}, cookies=setup.owner.cookies
    )

    assert response.status_code == 201, response.text
    assert response.json()["contact_id"] == contact_id


def test_superseding_a_memory_with_itself_is_rejected_cleanly(
    client: TestClient, setup: Setup
) -> None:
    """Security review (M6.2), fix: `MemoryService.supersede` now rejects
    `replacement_id == memory_id` with `InvalidStateTransitionError` before reading
    or mutating any row — previously it reached `Memory`'s `not_superseded_by_self`
    CHECK as a raw, unhandled 500."""
    memory = _remember(client, setup)

    response = client.post(
        f"{setup.memories}/{memory['id']}/supersede",
        json={"replacement_id": memory["id"]},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "invalid_state_transition"

    # The fix must not have mutated the memory it refused to resolve.
    unchanged = client.get(f"{setup.memories}/{memory['id']}", cookies=setup.owner.cookies)
    assert unchanged.json()["status"] == "active"
    assert unchanged.json()["superseded_by_id"] is None


def test_recording_rejects_a_contact_id_from_another_workspace(
    client: TestClient, db_session: Session, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    bob_contact_id = _contact(db_session, bob_workspace)

    response = client.post(
        f"/v1/workspaces/{alice_workspace}/memories",
        json=VALID_BODY | {"contact_id": bob_contact_id},
        cookies=alice.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"


def test_recording_rejects_a_source_conversation_from_another_workspace(
    client: TestClient, db_session: Session, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    bob_conversation_id = _conversation(db_session, bob_workspace)

    response = client.post(
        f"/v1/workspaces/{alice_workspace}/memories",
        json=VALID_BODY | {"source_conversation_id": bob_conversation_id},
        cookies=alice.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"


def test_an_unknown_field_is_rejected(client: TestClient, setup: Setup) -> None:
    body = VALID_BODY | {"workspace_id": str(uuid.uuid4())}
    response = client.post(setup.memories, json=body, cookies=setup.owner.cookies)

    assert response.status_code == 422


def test_a_client_cannot_attribute_a_memory_to_nerva_or_another_user(
    client: TestClient, setup: Setup
) -> None:
    """`created_by_type`/`created_by_user_id` are not fields on `MemoryCreate` at
    all — `RequestModel`'s `extra="forbid"` rejects any attempt to set them, and
    even if it did not, `MemoryService.remember` always attributes to
    `self.ctx.actor` (the authenticated human), never to a client-supplied actor."""
    body = VALID_BODY | {"created_by_type": "nerva", "created_by_user_id": None}
    response = client.post(setup.memories, json=body, cookies=setup.owner.cookies)

    assert response.status_code == 422
    created = _remember(client, setup)
    assert "created_by_type" not in created
    assert "created_by_user_id" not in created


# --- List --------------------------------------------------------------------------


def test_listing_defaults_to_active_memories_only(
    client: TestClient, setup: Setup, db_session: Session
) -> None:
    active = _remember(client, setup)
    discarded = _remember(client, setup)
    client.post(
        f"{setup.memories}/{discarded['id']}/discard", json={}, cookies=setup.owner.cookies
    )

    response = client.get(setup.memories, cookies=setup.owner.cookies)

    assert response.status_code == 200
    ids = [memory["id"] for memory in response.json()]
    assert ids == [active["id"]]


def test_an_explicit_status_reaches_a_historical_memory(
    client: TestClient, setup: Setup
) -> None:
    discarded = _remember(client, setup)
    client.post(
        f"{setup.memories}/{discarded['id']}/discard",
        json={"reason": "Stale."},
        cookies=setup.owner.cookies,
    )

    response = client.get(
        setup.memories, params={"status": "discarded"}, cookies=setup.owner.cookies
    )

    assert response.status_code == 200
    body = response.json()
    assert [memory["id"] for memory in body] == [discarded["id"]]
    assert body[0]["resolution_reason"] == "Stale."


def test_listing_filters_by_kind_and_origin(client: TestClient, setup: Setup) -> None:
    fact = _remember(client, setup, kind="fact", origin="observed")
    _remember(client, setup, kind="preference", origin="observed")
    _remember(client, setup, kind="fact", origin="inferred", confidence=0.5)

    response = client.get(
        setup.memories, params={"kind": "fact", "origin": "observed"}, cookies=setup.owner.cookies
    )

    assert [memory["id"] for memory in response.json()] == [fact["id"]]


def test_listing_filters_by_subject(
    client: TestClient, setup: Setup, db_session: Session
) -> None:
    contact_id = _contact(db_session, setup.workspace_id)
    about_contact = _remember(client, setup, contact_id=contact_id)
    _remember(client, setup)

    response = client.get(
        setup.memories, params={"contact_id": contact_id}, cookies=setup.owner.cookies
    )

    assert [memory["id"] for memory in response.json()] == [about_contact["id"]]


def test_listing_respects_pagination_limits(client: TestClient, setup: Setup) -> None:
    for _ in range(3):
        _remember(client, setup)

    response = client.get(setup.memories, params={"limit": 2}, cookies=setup.owner.cookies)

    assert len(response.json()) == 2


def test_an_invalid_limit_is_rejected(client: TestClient, setup: Setup) -> None:
    response = client.get(setup.memories, params={"limit": 0}, cookies=setup.owner.cookies)

    assert response.status_code == 422


def test_memories_in_another_workspace_are_hidden(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_setup = Setup(alice, make_workspace(alice, "Alice Co"))
    bob_workspace = make_workspace(bob, "Bob Co")
    _remember(client, alice_setup)

    bob_list = client.get(f"/v1/workspaces/{bob_workspace}/memories", cookies=bob.cookies)
    bob_reads_alice_workspace = client.get(
        f"/v1/workspaces/{alice_setup.workspace_id}/memories", cookies=bob.cookies
    )

    assert bob_list.json() == []
    assert bob_reads_alice_workspace.status_code == 404


def test_reading_a_memory_from_another_workspace_by_id_is_rejected(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_setup = Setup(alice, make_workspace(alice, "Alice Co"))
    bob_workspace = make_workspace(bob, "Bob Co")
    alice_memory = _remember(client, alice_setup)

    response = client.get(
        f"/v1/workspaces/{bob_workspace}/memories/{alice_memory['id']}", cookies=bob.cookies
    )

    assert response.status_code == 404


# --- Resolve: supersede and discard -------------------------------------------------


def test_a_member_can_supersede_a_memory_with_an_already_created_replacement(
    client: TestClient, setup: Setup
) -> None:
    original = _remember(client, setup)
    replacement = _remember(client, setup, statement="Acme's renewal moved to April.")

    response = client.post(
        f"{setup.memories}/{original['id']}/supersede",
        json={"replacement_id": replacement["id"]},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "superseded"
    assert body["superseded_by_id"] == replacement["id"]


def test_superseding_with_a_replacement_from_another_workspace_is_rejected(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_setup = Setup(alice, make_workspace(alice, "Alice Co"))
    bob_setup = Setup(bob, make_workspace(bob, "Bob Co"))
    original = _remember(client, alice_setup)
    bob_memory = _remember(client, bob_setup)

    response = client.post(
        f"{alice_setup.memories}/{original['id']}/supersede",
        json={"replacement_id": bob_memory["id"]},
        cookies=alice.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"


def test_a_repeated_supersede_is_rejected_with_409(client: TestClient, setup: Setup) -> None:
    original = _remember(client, setup)
    replacement = _remember(client, setup)
    supersede_url = f"{setup.memories}/{original['id']}/supersede"
    replace_body = {"replacement_id": replacement["id"]}
    client.post(supersede_url, json=replace_body, cookies=setup.owner.cookies)

    second = client.post(supersede_url, json=replace_body, cookies=setup.owner.cookies)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"


# --- Resolve: replace (atomic create + supersede, M6.3 Block 2) --------------------


def test_a_member_can_replace_a_memory_in_one_request(client: TestClient, setup: Setup) -> None:
    original = _remember(client, setup)

    response = client.post(
        f"{setup.memories}/{original['id']}/replace",
        json=VALID_BODY | {"statement": "Acme's renewal moved to April."},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    # Returns the new, active memory — not the one just superseded.
    assert body["id"] != original["id"]
    assert body["status"] == "active"
    assert body["statement"] == "Acme's renewal moved to April."
    assert body["superseded_by_id"] is None

    resolved_original = client.get(
        f"{setup.memories}/{original['id']}", cookies=setup.owner.cookies
    )
    assert resolved_original.json()["status"] == "superseded"
    assert resolved_original.json()["superseded_by_id"] == body["id"]


def test_replacing_an_unknown_memory_is_404(client: TestClient, setup: Setup) -> None:
    response = client.post(
        f"{setup.memories}/{uuid.uuid4()}/replace", json=VALID_BODY, cookies=setup.owner.cookies
    )

    assert response.status_code == 404


def test_replacing_a_memory_in_another_workspace_is_404(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_setup = Setup(alice, make_workspace(alice, "Alice Co"))
    bob_workspace = make_workspace(bob, "Bob Co")
    alice_memory = _remember(client, alice_setup)

    response = client.post(
        f"/v1/workspaces/{bob_workspace}/memories/{alice_memory['id']}/replace",
        json=VALID_BODY,
        cookies=bob.cookies,
    )

    assert response.status_code == 404
    # Untouched by the rejected cross-workspace attempt.
    unchanged = client.get(
        f"{alice_setup.memories}/{alice_memory['id']}", cookies=alice.cookies
    )
    assert unchanged.json()["status"] == "active"


def test_replacing_an_already_superseded_memory_is_rejected_with_409(
    client: TestClient, setup: Setup
) -> None:
    original = _remember(client, setup)
    client.post(
        f"{setup.memories}/{original['id']}/replace",
        json=VALID_BODY | {"statement": "First replacement."},
        cookies=setup.owner.cookies,
    )

    second = client.post(
        f"{setup.memories}/{original['id']}/replace",
        json=VALID_BODY | {"statement": "Second replacement."},
        cookies=setup.owner.cookies,
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"


def test_replacing_with_invalid_content_is_a_clean_422_and_leaves_the_original_untouched(
    client: TestClient, setup: Setup
) -> None:
    original = _remember(client, setup)

    response = client.post(
        f"{setup.memories}/{original['id']}/replace",
        json=VALID_BODY | {"statement": ""},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    unchanged = client.get(f"{setup.memories}/{original['id']}", cookies=setup.owner.cookies)
    assert unchanged.json()["status"] == "active"


def test_replacing_with_a_source_reference_from_another_workspace_is_422(
    client: TestClient, db_session: Session, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_setup = Setup(alice, make_workspace(alice, "Alice Co"))
    bob_workspace = make_workspace(bob, "Bob Co")
    bob_conversation_id = _conversation(db_session, bob_workspace)
    original = _remember(client, alice_setup)

    response = client.post(
        f"{alice_setup.memories}/{original['id']}/replace",
        json=VALID_BODY | {"source_conversation_id": bob_conversation_id},
        cookies=alice.cookies,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_reference"
    unchanged = client.get(
        f"{alice_setup.memories}/{original['id']}", cookies=alice.cookies
    )
    assert unchanged.json()["status"] == "active"


def test_an_admin_member_can_replace_another_members_memory(
    client: TestClient, setup: Setup, db_session: Session, make_user: MakeUser
) -> None:
    admin = make_user("Jordan")
    _add_member(db_session, setup.workspace_id, admin, role=MemberRole.ADMIN)
    original = _remember(client, setup)

    response = client.post(
        f"{setup.memories}/{original['id']}/replace",
        json=VALID_BODY | {"statement": "Replaced by an admin."},
        cookies=admin.cookies,
    )

    assert response.status_code == 201, response.text
    assert response.json()["status"] == "active"


def test_unauthenticated_replace_is_rejected(client: TestClient, setup: Setup) -> None:
    original = _remember(client, setup)

    response = client.post(f"{setup.memories}/{original['id']}/replace", json=VALID_BODY)

    assert response.status_code == 401


def test_a_member_can_discard_a_memory_with_a_reason(client: TestClient, setup: Setup) -> None:
    memory = _remember(client, setup)

    response = client.post(
        f"{setup.memories}/{memory['id']}/discard",
        json={"reason": "Turned out to be wrong."},
        cookies=setup.owner.cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "discarded"
    assert body["resolution_reason"] == "Turned out to be wrong."


def test_discarding_with_no_body_is_allowed(client: TestClient, setup: Setup) -> None:
    memory = _remember(client, setup)

    response = client.post(f"{setup.memories}/{memory['id']}/discard", cookies=setup.owner.cookies)

    assert response.status_code == 200, response.text
    assert response.json()["resolution_reason"] is None


def test_a_repeated_discard_is_rejected_with_409(client: TestClient, setup: Setup) -> None:
    memory = _remember(client, setup)
    discard_url = f"{setup.memories}/{memory['id']}/discard"
    client.post(discard_url, json={}, cookies=setup.owner.cookies)

    second = client.post(discard_url, json={}, cookies=setup.owner.cookies)

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "invalid_state_transition"


def test_resolving_a_memory_in_another_workspace_is_rejected(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_setup = Setup(alice, make_workspace(alice, "Alice Co"))
    bob_workspace = make_workspace(bob, "Bob Co")
    alice_memory = _remember(client, alice_setup)

    response = client.post(
        f"/v1/workspaces/{bob_workspace}/memories/{alice_memory['id']}/discard",
        json={},
        cookies=bob.cookies,
    )

    assert response.status_code == 404


# --- Authorization and isolation -----------------------------------------------------


def test_a_non_member_cannot_list_or_record_or_resolve(
    client: TestClient, setup: Setup, make_user: MakeUser
) -> None:
    mallory = make_user("Mallory")
    memory = _remember(client, setup)

    assert client.get(setup.memories, cookies=mallory.cookies).status_code == 404
    assert (
        client.post(setup.memories, json=VALID_BODY, cookies=mallory.cookies).status_code == 404
    )
    assert (
        client.post(
            f"{setup.memories}/{memory['id']}/discard", json={}, cookies=mallory.cookies
        ).status_code
        == 404
    )


def test_a_regular_member_can_record_and_resolve_memory(
    client: TestClient, setup: Setup, db_session: Session, make_user: MakeUser
) -> None:
    """Unlike `actions.execute_action`, nothing here spends an external resource —
    any workspace member may record or resolve memory, not just the owner."""
    member = make_user("Priya")
    _add_member(db_session, setup.workspace_id, member)

    created = client.post(setup.memories, json=VALID_BODY, cookies=member.cookies)
    assert created.status_code == 201, created.text

    discarded = client.post(
        f"{setup.memories}/{created.json()['id']}/discard", json={}, cookies=member.cookies
    )
    assert discarded.status_code == 200, discarded.text


def test_an_admin_member_can_supersede_another_members_memory(
    client: TestClient, setup: Setup, db_session: Session, make_user: MakeUser
) -> None:
    """Security review (M6.2): confirms the member-level access policy holds for the
    `admin` role too, not only `owner`/`member` — there is no `ctx.role` branch
    anywhere in `routes.memory`/`MemoryService`, so this is expected to pass for
    every role; this pins that down for `admin` specifically."""
    admin = make_user("Jordan")
    _add_member(db_session, setup.workspace_id, admin, role=MemberRole.ADMIN)
    original = _remember(client, setup)
    replacement = _remember(client, setup)

    response = client.post(
        f"{setup.memories}/{original['id']}/supersede",
        json={"replacement_id": replacement["id"]},
        cookies=admin.cookies,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "superseded"


def test_unauthenticated_requests_are_rejected(client: TestClient, setup: Setup) -> None:
    memory = _remember(client, setup)

    assert client.get(setup.memories).status_code == 401
    assert client.post(setup.memories, json=VALID_BODY).status_code == 401
    assert client.post(f"{setup.memories}/{memory['id']}/discard", json={}).status_code == 401


def test_no_internal_schema_version_leaks_unexpected_fields(
    client: TestClient, setup: Setup
) -> None:
    created = _remember(client, setup)

    forbidden_keys = {"prompt", "raw_output", "raw_response", "provider_response"}
    assert forbidden_keys.isdisjoint(created.keys())
