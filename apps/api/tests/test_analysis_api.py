"""API surface for AI conversation analysis: analyze one conversation, the
bounded owner-only backfill, and the "needs attention" query. Uses
`FakeAIClient` exclusively via `app.dependency_overrides` (see `conftest.py`'s
`ai_client` fixture) — no test here makes a real network call.
"""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nerva.core.enums import (
    ActorType,
    ConversationChannel,
    ConversationPriority,
    MemberRole,
    MemoryKind,
    MemoryOrigin,
    MemorySourceType,
    MessageDirection,
)
from nerva.core.errors import DataIntegrityError, InvalidStateTransitionError
from nerva.models import Contact, Conversation, Memory, Message, WorkspaceMember
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from nerva.nerva_ai.schemas import MonetaryEstimate
from nerva.repositories.workspace_data import MemoryRepository
from nerva.services.memory_formation import MemoryFormationService
from tests.fakes.ai import FakeAIClient, make_analysis_result
from tests.helpers import Account

MakeUser = Callable[[str], Account]
MakeWorkspace = Callable[[Account, str], str]

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _add_member(db_session: Session, workspace_id: str, user: Account) -> None:
    """A minimal way to add a second member without an invitation flow (not
    yet implemented) — mirrors `test_integrations_management.py`."""
    db_session.add(
        WorkspaceMember(
            workspace_id=uuid.UUID(workspace_id), user_id=uuid.UUID(user.id), role=MemberRole.MEMBER
        )
    )
    db_session.commit()


def _conversation(
    db_session: Session,
    workspace_id: str,
    subject: str = "Pricing",
    *,
    contact_id: str | None = None,
) -> str:
    conversation = Conversation(
        workspace_id=uuid.UUID(workspace_id),
        channel=ConversationChannel.EMAIL,
        subject=subject,
        contact_id=uuid.UUID(contact_id) if contact_id is not None else None,
    )
    db_session.add(conversation)
    db_session.commit()
    return str(conversation.id)


def _contact(db_session: Session, workspace_id: str, email: str = "priya@halcyon.example") -> str:
    contact = Contact(workspace_id=uuid.UUID(workspace_id), email=email)
    db_session.add(contact)
    db_session.commit()
    return str(contact.id)


def _memories(client: TestClient, workspace_id: str, owner: Account, **params: object) -> list:
    response = client.get(
        f"/v1/workspaces/{workspace_id}/memories", cookies=owner.cookies, params=params
    )
    assert response.status_code == 200, response.text
    return response.json()


def _insert_duplicate_active_fact_memory(
    db_session: Session, workspace_id: str, analysis_id: str
) -> None:
    """Inserts a *second* active FACT memory for the same analysis, bypassing
    `MemoryFormationService` entirely — the one pre-existing data
    inconsistency `form_estimated_value_memory` refuses to paper over (see its
    module docstring and `test_memory_formation.py`'s own
    `_direct_active_fact_memory`). Used to exercise the route's handling of a
    genuine `DataIntegrityError`, never a mocked one."""
    MemoryRepository(db_session, uuid.UUID(workspace_id)).add(
        Memory(
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content={
                "statement": "Estimated deal value: USD 1.00.",
                "attributes": {"estimated_value_cents": 100, "value_currency": "USD"},
            },
            schema_version=1,
            observed_at=NOW,
            source_type=MemorySourceType.ANALYSIS,
            source_analysis_id=uuid.UUID(analysis_id),
            source_fingerprint="fp-direct-duplicate",
            created_by_type=ActorType.NERVA,
        )
    )
    db_session.commit()


def _message(
    db_session: Session,
    conversation_id: str,
    workspace_id: str,
    *,
    body_text: str = "Can you send pricing for 3 seats?",
    sent_at: datetime = NOW,
) -> None:
    message = Message(
        workspace_id=uuid.UUID(workspace_id),
        conversation_id=uuid.UUID(conversation_id),
        direction=MessageDirection.INBOUND,
        sender_email="priya@halcyon.example",
        sender_name="Priya Nair",
        recipients="alex@gmail.example",
        body_text=body_text,
        sent_at=sent_at,
        external_id=uuid.uuid4().hex,
    )
    db_session.add(message)
    db_session.commit()


# --- Analyze -----------------------------------------------------------------------


def test_analyzing_a_conversation_returns_the_completed_result(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(commercial_signal=True, confidence=0.9)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["commercial_signal"] is True
    assert body["confidence"] == 0.9
    assert ai_client.call_count == 1


def test_a_non_owner_member_can_also_trigger_analysis(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    """Unlike Gmail connect/sync, analysis touches no external credentials, so
    it is not owner-restricted."""
    owner, member = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    _add_member(db_session, workspace_id, member)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=member.cookies,
    )

    assert response.status_code == 200, response.text


def test_reanalyzing_unchanged_content_costs_no_extra_provider_call(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    url = f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze"

    first = client.post(url, cookies=owner.cookies)
    second = client.post(url, cookies=owner.cookies)

    assert first.status_code == second.status_code == 200
    assert ai_client.call_count == 1
    assert first.json()["id"] == second.json()["id"]


def test_the_conversation_detail_embeds_the_current_analysis(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    detail_url = f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}"

    before = client.get(detail_url, cookies=owner.cookies).json()
    assert before["analysis"] is None

    analyze_response = client.post(f"{detail_url}/analyze", cookies=owner.cookies)
    assert analyze_response.status_code == 200, analyze_response.text
    after_response = client.get(detail_url, cookies=owner.cookies)
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()

    assert after["analysis"] is not None
    assert after["analysis"]["status"] == "completed"


def test_analyzing_a_conversation_in_another_workspace_is_not_found(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    alice_conversation = _conversation(db_session, alice_workspace)
    _message(db_session, alice_conversation, alice_workspace)

    response = client.post(
        f"/v1/workspaces/{bob_workspace}/conversations/{alice_conversation}/analyze",
        cookies=bob.cookies,
    )

    assert response.status_code == 404


def test_a_non_member_cannot_analyze_in_a_workspace_they_do_not_belong_to(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
) -> None:
    alice, mallory = make_user("Alice"), make_user("Mallory")
    workspace_id = make_workspace(alice)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=mallory.cookies,
    )

    assert response.status_code == 404


def test_an_unauthenticated_request_is_rejected(
    client: TestClient, db_session: Session, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    response = client.post(f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze")

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (AIAuthError("bad key"), 503, "ai_not_configured"),
        (AIRateLimitError("slow down", retry_after=30), 429, "rate_limited"),
        (AIOutputValidationError("bad json"), 503, "invalid_ai_output"),
        (AIProviderError("connection reset"), 503, "ai_unavailable"),
    ],
)
def test_provider_failures_map_to_safe_responses(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
    error: AIError,
    expected_status: int,
    expected_code: str,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(
        db_session, conversation_id, workspace_id, body_text="A message with SECRET-XYZ inside."
    )
    ai_client.fail_with = error

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == expected_status
    body = response.json()
    assert body["error"]["details"] == {"code": expected_code}
    assert "SECRET-XYZ" not in response.text
    assert "connection reset" not in response.text
    assert "bad json" not in response.text
    assert "bad key" not in response.text
    assert "slow down" not in response.text


# --- Backfill ------------------------------------------------------------------------


def test_owner_can_run_a_bounded_backfill(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    for i in range(3):
        conversation_id = _conversation(db_session, workspace_id, subject=f"Conversation {i}")
        _message(db_session, conversation_id, workspace_id, body_text=f"message {i}")

    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"
    response = client.post(backfill_url, cookies=owner.cookies)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body == {"considered": 3, "analyzed": 3, "cached": 0, "failed": 0, "signals_created": 0}
    assert ai_client.call_count == 3


def test_a_non_owner_member_cannot_run_backfill(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner, member = make_user("Alice"), make_user("Bob")
    workspace_id = make_workspace(owner)
    _add_member(db_session, workspace_id, member)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)

    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"
    response = client.post(backfill_url, cookies=member.cookies)

    assert response.status_code == 403
    assert ai_client.call_count == 0


def test_the_batch_size_maximum_is_enforced(
    client: TestClient, make_user: MakeUser, make_workspace: MakeWorkspace
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)

    response = client.post(
        f"/v1/workspaces/{workspace_id}/analysis/backfill?batch_size=21", cookies=owner.cookies
    )

    assert response.status_code == 422


def test_one_failed_conversation_does_not_abort_the_batch(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    for i in range(3):
        conversation_id = _conversation(db_session, workspace_id, subject=f"Conversation {i}")
        _message(db_session, conversation_id, workspace_id, body_text=f"message {i}")
    ai_client.fail_for_subjects = {"Conversation 1"}

    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"
    response = client.post(backfill_url, cookies=owner.cookies)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["considered"] == 3
    assert body["analyzed"] == 2
    assert body["failed"] == 1
    assert ai_client.call_count == 3  # the failure did not stop the remaining conversations


def test_repeated_backfill_does_not_unnecessarily_call_the_provider_again(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    url = f"/v1/workspaces/{workspace_id}/analysis/backfill"

    first = client.post(url, cookies=owner.cookies)
    second = client.post(url, cookies=owner.cookies)

    assert first.json()["analyzed"] == 1
    assert ai_client.call_count == 1  # the pre-filter already excludes the up-to-date conversation
    assert second.json() == {
        "considered": 0,
        "analyzed": 0,
        "cached": 0,
        "failed": 0,
        "signals_created": 0,
    }


def test_a_stale_model_id_makes_a_completed_conversation_eligible_for_backfill_again(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    """A conversation whose content has not changed at all must still be
    re-offered to `AnalysisService` once the configured model changes — the
    pre-filter's job is to not silently skip that, even though this particular
    scenario also happens to trigger a real provider call (the fingerprint,
    which folds in `model_id`, is genuinely different)."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    url = f"/v1/workspaces/{workspace_id}/analysis/backfill"

    first = client.post(url, cookies=owner.cookies)
    ai_client.model_id = "fake-model-2"
    second = client.post(url, cookies=owner.cookies)

    assert first.json()["analyzed"] == 1
    assert second.json()["considered"] == 1
    assert second.json()["analyzed"] == 1
    assert ai_client.call_count == 2


# --- Memory formation (M6.3, Block 3: wired into this route) -----------------------------


def test_analyzing_with_an_explicit_estimate_forms_a_fact_memory(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(
        commercial_signal=True,
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == 200, response.text
    analysis_id = response.json()["id"]
    memories = _memories(client, workspace_id, owner)
    assert len(memories) == 1
    memory = memories[0]
    assert memory["kind"] == "fact"
    assert memory["origin"] == "observed"
    assert memory["status"] == "active"
    assert memory["confidence"] is None
    assert memory["source_type"] == "analysis"
    assert memory["source_analysis_id"] == analysis_id
    assert memory["source_fingerprint"] is not None
    assert memory["attributes"] == {"estimated_value_cents": 150_000, "value_currency": "USD"}


def test_forcing_reanalysis_with_a_changed_estimate_supersedes_without_two_active_memories(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    url = f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze"
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )
    first = client.post(url, cookies=owner.cookies)
    assert first.status_code == 200, first.text
    original_memory_id = _memories(client, workspace_id, owner)[0]["id"]

    # New message content: a genuinely different analysis input, so the next
    # analysis gets a new fingerprint rather than hitting the cache.
    _message(
        db_session,
        conversation_id,
        workspace_id,
        body_text="Actually we'd need 10 seats now.",
        sent_at=NOW + timedelta(minutes=5),
    )
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=300_000, currency="USD", is_explicit=True),
    )

    second = client.post(f"{url}?force=true", cookies=owner.cookies)

    assert second.status_code == 200, second.text
    active = _memories(client, workspace_id, owner)
    assert len(active) == 1
    assert active[0]["id"] != original_memory_id
    assert active[0]["attributes"]["estimated_value_cents"] == 300_000
    superseded = _memories(client, workspace_id, owner, status="superseded")
    assert [m["id"] for m in superseded] == [original_memory_id]
    assert superseded[0]["superseded_by_id"] == active[0]["id"]


def test_analyzing_without_an_estimate_does_not_create_a_memory(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(confidence=0.5)  # no estimated_value

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "completed"
    assert _memories(client, workspace_id, owner) == []


def test_a_provider_failure_does_not_form_a_memory(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.fail_with = AIProviderError("connection reset")

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    # Same contract `test_provider_failures_map_to_safe_responses` already
    # pins for this error: AIProviderError -> 503 ai_unavailable, via
    # `_raise_for_ai_failure`. Analysis never reached COMPLETED, so
    # MemoryFormationService.form_estimated_value_memory is never even
    # reached (routes/analysis.py: SignalService/MemoryFormationService run
    # only after `analysis_service.analyze()` returns successfully).
    assert response.status_code == 503
    assert response.json()["error"]["details"] == {"code": "ai_unavailable"}
    assert _memories(client, workspace_id, owner) == []


def test_a_cache_hit_reanalysis_does_not_duplicate_the_memory(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )
    url = f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze"

    first = client.post(url, cookies=owner.cookies)
    first_memory_id = _memories(client, workspace_id, owner)[0]["id"]
    second = client.post(url, cookies=owner.cookies)

    assert first.status_code == second.status_code == 200
    assert ai_client.call_count == 1  # the cache hit never calls the provider again
    memories = _memories(client, workspace_id, owner)
    assert len(memories) == 1
    assert memories[0]["id"] == first_memory_id


def test_a_conversation_without_a_contact_forms_a_workspace_level_memory(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)  # no contact_id
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == 200, response.text
    memories = _memories(client, workspace_id, owner)
    assert len(memories) == 1
    assert memories[0]["contact_id"] is None
    assert memories[0]["company_id"] is None
    assert memories[0]["opportunity_id"] is None


def test_a_conversation_with_a_known_contact_attaches_the_memory_to_it(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    contact_id = _contact(db_session, workspace_id)
    conversation_id = _conversation(db_session, workspace_id, contact_id=contact_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )

    response = client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze",
        cookies=owner.cookies,
    )

    assert response.status_code == 200, response.text
    memories = _memories(client, workspace_id, owner)
    assert len(memories) == 1
    assert memories[0]["contact_id"] == contact_id


def test_memory_formation_is_isolated_between_workspaces(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    alice, bob = make_user("Alice"), make_user("Bob")
    alice_workspace = make_workspace(alice, "Alice Co")
    bob_workspace = make_workspace(bob, "Bob Co")
    alice_conversation = _conversation(db_session, alice_workspace)
    _message(db_session, alice_conversation, alice_workspace)
    bob_conversation = _conversation(db_session, bob_workspace)
    _message(db_session, bob_conversation, bob_workspace)
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )

    client.post(
        f"/v1/workspaces/{alice_workspace}/conversations/{alice_conversation}/analyze",
        cookies=alice.cookies,
    )
    client.post(
        f"/v1/workspaces/{bob_workspace}/conversations/{bob_conversation}/analyze",
        cookies=bob.cookies,
    )

    alice_memories = _memories(client, alice_workspace, alice)
    bob_memories = _memories(client, bob_workspace, bob)
    assert len(alice_memories) == 1
    assert len(bob_memories) == 1
    assert alice_memories[0]["id"] != bob_memories[0]["id"]


def test_a_memory_formation_failure_during_individual_analysis_is_not_a_misleading_success(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    """When `form_estimated_value_memory` hits pre-existing inconsistent data
    (a real `DataIntegrityError`, not mocked — see
    `_insert_duplicate_active_fact_memory`), the request must not swallow it
    into a 200: the whole point of M6.3 Block 3's contract is that a memory
    failure is never silently reported as success."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_id = _conversation(db_session, workspace_id)
    _message(db_session, conversation_id, workspace_id)
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )
    url = f"/v1/workspaces/{workspace_id}/conversations/{conversation_id}/analyze"
    first = client.post(url, cookies=owner.cookies)
    assert first.status_code == 200, first.text
    analysis_id = first.json()["id"]
    _insert_duplicate_active_fact_memory(db_session, workspace_id, analysis_id)

    with pytest.raises(DataIntegrityError):
        client.post(url, cookies=owner.cookies)

    # Neither pre-existing active memory was touched by the failed attempt.
    memories = _memories(client, workspace_id, owner)
    assert len(memories) == 2
    assert all(m["status"] == "active" for m in memories)


def test_backfill_forms_memories_for_conversations_with_valid_estimates(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    with_estimate = _conversation(db_session, workspace_id, subject="Has an estimate")
    _message(db_session, with_estimate, workspace_id, body_text="estimate conversation")
    without_estimate = _conversation(db_session, workspace_id, subject="No estimate")
    _message(
        db_session,
        without_estimate,
        workspace_id,
        body_text="no estimate conversation",
        sent_at=NOW + timedelta(minutes=5),
    )
    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"

    # FakeAIClient scripts one result for every call in a batch, so the
    # no-estimate conversation is analyzed individually first (with a
    # no-estimate result) to take it out of `list_needing_analysis`'s
    # selection before the backfill call below runs with an estimate scripted.
    ai_client.next_result = make_analysis_result(confidence=0.5)
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{without_estimate}/analyze",
        cookies=owner.cookies,
    )
    assert _memories(client, workspace_id, owner) == []

    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )
    response = client.post(backfill_url, cookies=owner.cookies)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["considered"] == 1  # the no-estimate conversation is already analyzed/cached
    assert body["analyzed"] == 1
    assert body["failed"] == 0
    memories = _memories(client, workspace_id, owner)
    assert len(memories) == 1
    assert memories[0]["attributes"]["estimated_value_cents"] == 150_000


def test_a_memory_formation_failure_in_the_backfill_does_not_abort_the_batch(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    """A real `DataIntegrityError` (not mocked) for exactly one conversation's
    memory formation must be isolated: the batch still processes every other
    conversation, and the counters stay internally consistent — `considered
    == analyzed + cached + failed`, with no double-counting of the one
    conversation whose *memory* (not analysis) failed."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_ids = []
    for i in range(3):
        conversation_id = _conversation(db_session, workspace_id, subject=f"Conversation {i}")
        _message(
            db_session,
            conversation_id,
            workspace_id,
            body_text=f"message {i}",
            sent_at=NOW + timedelta(minutes=i),
        )
        conversation_ids.append(conversation_id)
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )
    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"

    first = client.post(backfill_url, cookies=owner.cookies)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "considered": 3,
        "analyzed": 3,
        "cached": 0,
        "failed": 0,
        "signals_created": 0,
    }
    assert len(_memories(client, workspace_id, owner)) == 3

    detail = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/{conversation_ids[1]}",
        cookies=owner.cookies,
    ).json()
    analysis_id = detail["analysis"]["id"]
    _insert_duplicate_active_fact_memory(db_session, workspace_id, analysis_id)
    ai_client.model_id = "fake-model-2"  # makes every conversation eligible again

    second = client.post(backfill_url, cookies=owner.cookies)

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["considered"] == 3
    assert body["failed"] == 1
    assert body["analyzed"] + body["cached"] == 2
    assert ai_client.call_count == 6  # all 3 re-analyzed in round one, and again in round two
    # The conversation whose memory formation failed keeps both of its active
    # memories untouched — no partial supersede, nothing silently resolved.
    all_memories = _memories(client, workspace_id, owner)
    assert len(all_memories) == 4  # 3 from round one + the directly-inserted duplicate
    conflicting = [m for m in all_memories if m["source_analysis_id"] == analysis_id]
    assert len(conflicting) == 2
    assert all(m["status"] == "active" for m in conflicting)


def test_an_invalid_state_transition_in_the_backfill_does_not_abort_the_batch(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`InvalidStateTransitionError` is the *other* documented, non-bug
    failure `MemoryFormationService.form_estimated_value_memory` can raise —
    a live race with someone else resolving the exact same memory directly
    (e.g. `POST /memories/{id}/replace` or `/discard`) in the window between
    that service's own unlocked read and `MemoryService.replace`'s locking
    re-read. That race itself is proven real and deterministic, with no
    mocking, in `test_memory_formation.py`'s own
    `test_a_concurrent_external_resolution_of_the_active_memory_raises_invalid_state_transition`.
    This test's job is narrower and route-specific: given that exception type
    is genuinely reachable, does `backfill_analysis` isolate it exactly like
    it isolates `DataIntegrityError`? Scripted via a direct monkeypatch (the
    cheapest faithful stand-in for an already-proven-real condition — the
    same role `ai_client.fail_for_subjects` already plays for
    `AnalysisService` failures) rather than re-synchronizing a real race
    through the HTTP layer a second time."""
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    conversation_ids = []
    for i in range(3):
        conversation_id = _conversation(db_session, workspace_id, subject=f"Conversation {i}")
        _message(
            db_session,
            conversation_id,
            workspace_id,
            body_text=f"message {i}",
            sent_at=NOW + timedelta(minutes=i),
        )
        conversation_ids.append(conversation_id)
    ai_client.next_result = make_analysis_result(
        confidence=0.9,
        estimated_value=MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True),
    )
    backfill_url = f"/v1/workspaces/{workspace_id}/analysis/backfill"
    first = client.post(backfill_url, cookies=owner.cookies)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "considered": 3,
        "analyzed": 3,
        "cached": 0,
        "failed": 0,
        "signals_created": 0,
    }

    target_id = uuid.UUID(conversation_ids[1])
    original_form = MemoryFormationService.form_estimated_value_memory

    def _raise_for_target(
        self: MemoryFormationService, conversation_id: uuid.UUID, *args: object, **kwargs: object
    ) -> object:
        if conversation_id == target_id:
            raise InvalidStateTransitionError("simulated concurrent resolution")
        return original_form(self, conversation_id, *args, **kwargs)

    monkeypatch.setattr(MemoryFormationService, "form_estimated_value_memory", _raise_for_target)
    ai_client.model_id = "fake-model-2"  # makes every conversation eligible again

    second = client.post(backfill_url, cookies=owner.cookies)

    assert second.status_code == 200, second.text
    body = second.json()
    assert body["considered"] == 3
    assert body["failed"] == 1
    assert body["analyzed"] + body["cached"] == 2
    assert ai_client.call_count == 6  # all 3 re-analyzed in round one, and again in round two


# --- Needs attention -------------------------------------------------------------------


def test_attention_lists_high_priority_and_requires_response_conversations(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    urgent_conversation = _conversation(db_session, workspace_id, subject="Server is down")
    _message(db_session, urgent_conversation, workspace_id, body_text="Urgent issue")
    quiet_conversation = _conversation(db_session, workspace_id, subject="Newsletter")
    _message(
        db_session,
        quiet_conversation,
        workspace_id,
        body_text="Just a newsletter",
        sent_at=NOW + timedelta(minutes=5),
    )

    ai_client.next_result = make_analysis_result(
        priority=ConversationPriority.URGENT, requires_response=True
    )
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{urgent_conversation}/analyze",
        cookies=owner.cookies,
    )
    ai_client.next_result = make_analysis_result(
        priority=ConversationPriority.LOW, requires_response=False
    )
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{quiet_conversation}/analyze",
        cookies=owner.cookies,
    )

    response = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/attention", cookies=owner.cookies
    )

    assert response.status_code == 200
    body = response.json()
    assert [row["conversation_id"] for row in body] == [urgent_conversation]
    assert body[0]["priority"] == "urgent"
    assert body[0]["requires_response"] is True


def test_conversations_without_a_qualifying_analysis_are_not_surfaced(
    client: TestClient,
    db_session: Session,
    make_user: MakeUser,
    make_workspace: MakeWorkspace,
    ai_client: FakeAIClient,
) -> None:
    owner = make_user("Alice")
    workspace_id = make_workspace(owner)
    unanalyzed = _conversation(db_session, workspace_id, subject="Never analyzed")
    _message(db_session, unanalyzed, workspace_id)
    low_priority = _conversation(db_session, workspace_id, subject="Low priority")
    _message(db_session, low_priority, workspace_id, sent_at=NOW + timedelta(minutes=5))
    ai_client.next_result = make_analysis_result(
        priority=ConversationPriority.LOW, requires_response=False
    )
    client.post(
        f"/v1/workspaces/{workspace_id}/conversations/{low_priority}/analyze", cookies=owner.cookies
    )

    response = client.get(
        f"/v1/workspaces/{workspace_id}/conversations/attention", cookies=owner.cookies
    )

    assert response.json() == []
