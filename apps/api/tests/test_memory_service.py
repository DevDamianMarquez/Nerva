"""`MemoryService`: recording, superseding and discarding workspace memory
against a real database, including tenant isolation — mirrors
`test_signal_service.py` for the same reasons. No AI provider is involved:
every memory here is recorded explicitly by the test, the same way a human or
a future reasoning layer would call `remember`."""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import (
    ActivityEventType,
    ActorType,
    AnalysisStatus,
    ConversationCategory,
    ConversationChannel,
    ConversationPriority,
    MemberRole,
    MemoryKind,
    MemoryOrigin,
    MemorySourceType,
    MemoryStatus,
)
from nerva.core.errors import InvalidReferenceError, InvalidStateTransitionError, NotFoundError
from nerva.models import Contact, Conversation, ConversationAnalysis, Memory, User, Workspace
from nerva.nerva_memory.errors import MemoryContentError
from nerva.repositories.workspace_data import ConversationAnalysisRepository, MemoryRepository
from nerva.services.activity import ActivityService
from nerva.services.memory import MemoryService

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

VALID_CONTENT = {"statement": "Acme's renewal is in March."}


def _workspace(db_session: Session, name: str = "Acme") -> Workspace:
    workspace = Workspace(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    db_session.add(workspace)
    db_session.flush()
    return workspace


def _conversation(
    db_session: Session, workspace: Workspace, subject: str = "Renewal"
) -> Conversation:
    conversation = Conversation(
        workspace_id=workspace.id, channel=ConversationChannel.EMAIL, subject=subject
    )
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _completed_analysis(
    db_session: Session,
    workspace: Workspace,
    conversation: Conversation,
    *,
    fingerprint: str = "fp-1",
) -> ConversationAnalysis:
    repo = ConversationAnalysisRepository(db_session, workspace.id)
    analysis = repo.add(
        ConversationAnalysis(
            conversation_id=conversation.id,
            status=AnalysisStatus.COMPLETED,
            schema_version=1,
            prompt_version="v1",
            provider="fake",
            model_id="fake-model-1",
            input_fingerprint=fingerprint,
            category=ConversationCategory.SALES_LEAD,
            priority=ConversationPriority.MEDIUM,
            requires_response=False,
            commercial_signal=True,
            confidence=0.9,
            summary="Discussed renewal timing.",
            analyzed_at=NOW,
        )
    )
    db_session.flush()
    return analysis


def _contact(
    db_session: Session, workspace: Workspace, email: str = "priya@halcyon.example"
) -> Contact:
    contact = Contact(workspace_id=workspace.id, email=email)
    db_session.add(contact)
    db_session.flush()
    return contact


def _user(db_session: Session, name: str = "Alex") -> User:
    email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, name=name, password_hash="x")
    db_session.add(user)
    db_session.flush()
    return user


MakeMemoryService = Callable[[Workspace], MemoryService]


@pytest.fixture
def make_service(db_session: Session) -> MakeMemoryService:
    def _make(workspace: Workspace) -> MemoryService:
        user = _user(db_session)
        ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
        return MemoryService(db_session, ctx)

    return _make


# ---------------------------------------------------------------------------
# A. Recording
# ---------------------------------------------------------------------------


def test_remembering_a_manual_fact_records_an_active_memory(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)

    memory = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
    )

    assert memory.status == MemoryStatus.ACTIVE
    assert memory.source_type == MemorySourceType.MANUAL
    assert memory.content == VALID_CONTENT | {"attributes": {}}
    assert memory.schema_version == 1
    assert memory.confidence is None
    assert memory.resolved_at is None
    assert memory.superseded_by_id is None
    assert memory.created_by_type.value == "user"


def test_remembering_from_a_conversation_sets_source_type_and_id(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    service = make_service(workspace)

    memory = service.remember(
        kind=MemoryKind.NOTE,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
        source_conversation_id=conversation.id,
    )

    assert memory.source_type == MemorySourceType.CONVERSATION
    assert memory.source_conversation_id == conversation.id
    assert memory.source_fingerprint is None


def test_remembering_from_an_analysis_captures_its_fingerprint(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-xyz")
    service = make_service(workspace)

    memory = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.7,
        source_analysis_id=analysis.id,
    )

    assert memory.source_type == MemorySourceType.ANALYSIS
    assert memory.source_analysis_id == analysis.id
    assert memory.source_fingerprint == "fp-xyz"
    assert memory.confidence == 0.7


def test_remembering_rejects_both_a_conversation_and_an_analysis_source(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    with pytest.raises(InvalidReferenceError):
        service.remember(
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            observed_at=NOW,
            source_conversation_id=conversation.id,
            source_analysis_id=analysis.id,
        )


def test_remembering_rejects_invalid_content(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)

    with pytest.raises(MemoryContentError):
        service.remember(
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content={"statement": ""},
            observed_at=NOW,
        )

    assert MemoryRepository(db_session, workspace.id).list() == []


def test_remembering_rejects_a_source_conversation_from_another_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    alice_service = make_service(alice_workspace)

    with pytest.raises(InvalidReferenceError):
        alice_service.remember(
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            observed_at=NOW,
            source_conversation_id=bob_conversation.id,
        )
    assert MemoryRepository(db_session, alice_workspace.id).list() == []


def test_remembering_rejects_a_source_analysis_from_another_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    bob_analysis = _completed_analysis(db_session, bob_workspace, bob_conversation)
    alice_service = make_service(alice_workspace)

    with pytest.raises(InvalidReferenceError):
        alice_service.remember(
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.INFERRED,
            content=VALID_CONTENT,
            observed_at=NOW,
            confidence=0.5,
            source_analysis_id=bob_analysis.id,
        )


def test_remembering_rejects_a_subject_contact_from_another_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_contact = _contact(db_session, bob_workspace)
    alice_service = make_service(alice_workspace)

    with pytest.raises(InvalidReferenceError):
        alice_service.remember(
            kind=MemoryKind.PREFERENCE,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            observed_at=NOW,
            contact_id=bob_contact.id,
        )


def test_an_activity_event_is_recorded_when_a_memory_is_remembered(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)

    memory = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
    )

    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    assert len(events) == 1
    assert events[0].event_type == ActivityEventType.MEMORY_RECORDED.value
    assert events[0].entity_id == memory.id


# ---------------------------------------------------------------------------
# B. Reading, and isolation
# ---------------------------------------------------------------------------


def test_get_raises_not_found_for_an_unknown_memory(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)

    with pytest.raises(NotFoundError):
        service.get(uuid.uuid4())


def test_get_is_isolated_by_workspace(db_session: Session, make_service: MakeMemoryService) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_memory = make_service(bob_workspace).remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    alice_service = make_service(alice_workspace)

    with pytest.raises(NotFoundError):
        alice_service.get(bob_memory.id)


# ---------------------------------------------------------------------------
# C. Resolving: supersede and discard
# ---------------------------------------------------------------------------


def test_superseding_links_the_old_memory_to_its_replacement(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    replacement = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.OBSERVED,
        content={"statement": "Acme's renewal moved to April."},
        observed_at=NOW,
    )

    resolved = service.supersede(original.id, replacement_id=replacement.id)

    assert resolved.status == MemoryStatus.SUPERSEDED
    assert resolved.superseded_by_id == replacement.id
    assert resolved.resolved_at is not None


def test_superseding_rejects_a_replacement_from_another_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    alice_service = make_service(alice_workspace)
    original = alice_service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    bob_memory = make_service(bob_workspace).remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    with pytest.raises(InvalidReferenceError):
        alice_service.supersede(original.id, replacement_id=bob_memory.id)


def test_a_repeated_supersede_is_rejected(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    replacement = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    service.supersede(original.id, replacement_id=replacement.id)

    with pytest.raises(InvalidStateTransitionError):
        service.supersede(original.id, replacement_id=replacement.id)


def test_discarding_records_a_reason(db_session: Session, make_service: MakeMemoryService) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    resolved = service.discard(memory.id, reason="Turned out to be wrong.")

    assert resolved.status == MemoryStatus.DISCARDED
    assert resolved.resolution_reason == "Turned out to be wrong."
    assert resolved.resolved_at is not None


def test_a_repeated_discard_is_rejected(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    service.discard(memory.id)

    with pytest.raises(InvalidStateTransitionError):
        service.discard(memory.id)


def test_resolving_an_unknown_memory_is_not_found(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)

    with pytest.raises(NotFoundError):
        service.discard(uuid.uuid4())


def test_superseding_records_an_activity_event_for_the_authenticated_actor(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    """Security review (M6.2): `_resolve` always records via `self.ctx.actor` —
    this was already true in the code, just never asserted directly for
    `supersede`/`discard` (only for `remember`)."""
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    replacement = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    service.supersede(original.id, replacement_id=replacement.id)

    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    superseded = ActivityEventType.MEMORY_SUPERSEDED.value
    supersede_events = [e for e in events if e.event_type == superseded]
    assert len(supersede_events) == 1
    event = supersede_events[0]
    assert event.workspace_id == workspace.id
    assert event.entity_id == original.id
    assert event.actor_type == ActorType.USER
    assert event.actor_user_id == service.ctx.user_id
    assert event.data == {
        "from_status": "active",
        "to_status": "superseded",
        "superseded_by_id": str(replacement.id),
    }


def test_discarding_records_an_activity_event_for_the_authenticated_actor(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    service.discard(memory.id, reason="Turned out to be wrong.")

    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    discard_events = [e for e in events if e.event_type == ActivityEventType.MEMORY_DISCARDED.value]
    assert len(discard_events) == 1
    event = discard_events[0]
    assert event.workspace_id == workspace.id
    assert event.entity_id == memory.id
    assert event.actor_type == ActorType.USER
    assert event.actor_user_id == service.ctx.user_id
    assert event.data == {
        "from_status": "active",
        "to_status": "discarded",
        "reason": "Turned out to be wrong.",
    }


# ---------------------------------------------------------------------------
# D. Listing active memories
# ---------------------------------------------------------------------------


def test_list_active_excludes_resolved_memories_and_is_isolated_by_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    alice_service = make_service(alice_workspace)
    active = alice_service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    discarded = alice_service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    alice_service.discard(discarded.id)
    make_service(bob_workspace).remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    results = MemoryRepository(db_session, alice_workspace.id).list_active(
        kind=None, contact_id=None, company_id=None, opportunity_id=None, limit=50, offset=0
    )

    assert [memory.id for memory in results] == [active.id]


# ---------------------------------------------------------------------------
# D2. MemoryService.list — explicit, typed filters and the active-by-default policy
# ---------------------------------------------------------------------------


def test_list_with_no_status_defaults_to_active_only(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    active = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    discarded = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    service.discard(discarded.id)

    results = service.list()

    assert [memory.id for memory in results] == [active.id]


def test_list_with_an_explicit_status_returns_exactly_that_historical_state(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    active = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    discarded = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    service.discard(discarded.id, reason="No longer true.")
    replacement = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    superseded = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    service.supersede(superseded.id, replacement_id=replacement.id)

    discarded_results = service.list(status=MemoryStatus.DISCARDED)
    superseded_results = service.list(status=MemoryStatus.SUPERSEDED)
    explicit_active_results = service.list(status=MemoryStatus.ACTIVE)

    assert [memory.id for memory in discarded_results] == [discarded.id]
    assert [memory.id for memory in superseded_results] == [superseded.id]
    assert {memory.id for memory in explicit_active_results} == {active.id, replacement.id}


def test_list_filters_by_kind_and_origin(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    observed_fact = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.6,
    )
    service.remember(
        kind=MemoryKind.PREFERENCE,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
    )

    results = service.list(kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED)

    assert [memory.id for memory in results] == [observed_fact.id]


def test_list_filters_by_subject(db_session: Session, make_service: MakeMemoryService) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    service = make_service(workspace)
    about_contact = service.remember(
        kind=MemoryKind.PREFERENCE,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
        contact_id=contact.id,
    )
    service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    results = service.list(contact_id=contact.id)

    assert [memory.id for memory in results] == [about_contact.id]


def test_list_filtering_by_a_subject_id_from_another_workspace_returns_nothing(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    """Never a cross-tenant leak, never an error either: `Memory.contact_id` can
    only ever point at a contact in the same workspace (the `tenant_fk` the model
    declares), so a foreign id simply matches no row."""
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_contact = _contact(db_session, bob_workspace)
    alice_service = make_service(alice_workspace)
    alice_service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    results = alice_service.list(contact_id=bob_contact.id)

    assert list(results) == []


def test_list_is_isolated_by_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    alice_memory = make_service(alice_workspace).remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    make_service(bob_workspace).remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    results = make_service(alice_workspace).list()

    assert [memory.id for memory in results] == [alice_memory.id]


def test_list_orders_newest_first_and_respects_the_limit(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    created = [
        service.remember(
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            observed_at=NOW,
        )
        for _ in range(3)
    ]

    results = service.list(limit=2)

    assert len(results) == 2
    assert [memory.id for memory in results] == [created[2].id, created[1].id]


# ---------------------------------------------------------------------------
# E. Defense in depth: the database itself enforces the contract
# ---------------------------------------------------------------------------


def test_database_rejects_a_memory_referencing_a_conversation_in_another_workspace(
    db_session: Session,
) -> None:
    first, second = _workspace(db_session, "One"), _workspace(db_session, "Two")
    conversation_in_first = _conversation(db_session, first)

    db_session.add(
        Memory(
            workspace_id=second.id,  # mismatched on purpose
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            schema_version=1,
            observed_at=NOW,
            source_type=MemorySourceType.CONVERSATION,
            source_conversation_id=conversation_in_first.id,
            created_by_type=ActorType.NERVA,
        )
    )

    with pytest.raises(IntegrityError, match="fk_memories"):
        db_session.flush()


def test_database_rejects_confidence_on_an_observed_memory(db_session: Session) -> None:
    workspace = _workspace(db_session)

    db_session.add(
        Memory(
            workspace_id=workspace.id,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            schema_version=1,
            observed_at=NOW,
            confidence=0.9,  # not allowed for an observed memory
            source_type=MemorySourceType.MANUAL,
            created_by_type=ActorType.NERVA,
        )
    )

    with pytest.raises(IntegrityError, match="ck_memories_confidence_only_when_inferred"):
        db_session.flush()


def test_database_rejects_an_inferred_memory_with_no_confidence(db_session: Session) -> None:
    workspace = _workspace(db_session)

    db_session.add(
        Memory(
            workspace_id=workspace.id,
            origin=MemoryOrigin.INFERRED,
            content=VALID_CONTENT,
            schema_version=1,
            observed_at=NOW,
            confidence=None,
            source_type=MemorySourceType.MANUAL,
            created_by_type=ActorType.NERVA,
        )
    )

    with pytest.raises(IntegrityError, match="ck_memories_confidence_only_when_inferred"):
        db_session.flush()


def test_database_rejects_an_active_memory_with_a_superseded_by_id_set(db_session: Session) -> None:
    workspace = _workspace(db_session)

    db_session.add(
        Memory(
            workspace_id=workspace.id,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            schema_version=1,
            observed_at=NOW,
            source_type=MemorySourceType.MANUAL,
            created_by_type=ActorType.NERVA,
            superseded_by_id=uuid.uuid4(),
        )
    )

    with pytest.raises(IntegrityError, match="ck_memories_superseded_by_only_when_superseded"):
        db_session.flush()


def test_database_rejects_a_memory_that_supersedes_itself(db_session: Session) -> None:
    workspace = _workspace(db_session)
    memory_id = uuid.uuid4()

    db_session.add(
        Memory(
            id=memory_id,
            workspace_id=workspace.id,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            schema_version=1,
            observed_at=NOW,
            status=MemoryStatus.SUPERSEDED,
            resolved_at=NOW,
            source_type=MemorySourceType.MANUAL,
            created_by_type=ActorType.NERVA,
            superseded_by_id=memory_id,
        )
    )

    with pytest.raises(IntegrityError, match="ck_memories_not_superseded_by_self"):
        db_session.flush()


# ---------------------------------------------------------------------------
# F. A memory outlives its source: ON DELETE SET NULL is column-restricted
# ---------------------------------------------------------------------------
#
# A plain composite-FK `ON DELETE SET NULL` nulls *every* column in the FK,
# including `workspace_id` — which is NOT NULL on `memories` and would make the
# delete fail instead of the memory surviving. `tenant_fk`'s callers here use
# PostgreSQL 15+'s `SET NULL (column)` syntax to restrict the null action to
# the dangling reference alone. These tests exercise the real deletes, not
# just the DDL, since this behavior cannot be trusted from inspection alone.


def test_a_memory_survives_deletion_of_its_source_conversation(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    memory = MemoryService(
        db_session,
        WorkspaceContext(
            workspace_id=workspace.id, user_id=_user(db_session).id, role=MemberRole.OWNER
        ),
    ).remember(
        kind=MemoryKind.NOTE,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
        source_conversation_id=conversation.id,
    )

    db_session.delete(db_session.get(Conversation, conversation.id))
    db_session.commit()  # must not raise — see the module note above
    db_session.expire_all()  # the FK action is server-side; the fixture disables auto-expiry

    survivor = db_session.get(Memory, memory.id)
    assert survivor is not None
    assert survivor.workspace_id == workspace.id  # untouched by the FK action
    assert survivor.source_conversation_id is None  # the dangling reference, nulled
    assert survivor.source_type == MemorySourceType.CONVERSATION  # provenance kept
    assert survivor.content == VALID_CONTENT | {"attributes": {}}  # content untouched


def test_a_memory_survives_deletion_of_its_source_analysis(db_session: Session) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-outlive")
    memory = MemoryService(
        db_session,
        WorkspaceContext(
            workspace_id=workspace.id, user_id=_user(db_session).id, role=MemberRole.OWNER
        ),
    ).remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.6,
        source_analysis_id=analysis.id,
    )

    # Deleting the conversation cascades to the analysis (see `ConversationAnalysis`'s
    # own `ondelete="CASCADE"` on `conversation_id`) — the harder case, since two FK
    # actions fire for the same memory in one statement.
    db_session.delete(db_session.get(Conversation, conversation.id))
    db_session.commit()
    db_session.expire_all()  # the FK action is server-side; the fixture disables auto-expiry

    survivor = db_session.get(Memory, memory.id)
    assert survivor is not None
    assert survivor.workspace_id == workspace.id
    assert survivor.source_analysis_id is None
    assert survivor.source_type == MemorySourceType.ANALYSIS
    assert survivor.source_fingerprint == "fp-outlive"  # the durable provenance copy


def test_deleting_a_source_conversation_does_not_touch_an_unrelated_memory(
    db_session: Session,
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    user = _user(db_session)
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
    service = MemoryService(db_session, ctx)
    sourced = service.remember(
        kind=MemoryKind.NOTE,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
        source_conversation_id=conversation.id,
    )
    manual = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    db_session.delete(db_session.get(Conversation, conversation.id))
    db_session.commit()
    db_session.expire_all()  # the FK action is server-side; the fixture disables auto-expiry

    assert db_session.get(Memory, sourced.id).source_conversation_id is None
    untouched = db_session.get(Memory, manual.id)
    assert untouched.source_type == MemorySourceType.MANUAL
    assert untouched.source_conversation_id is None  # was always None, still is


# `test_database_rejects_a_memory_referencing_a_conversation_in_another_workspace`
# (above) already proves the composite FK still rejects a cross-tenant insert —
# the column-restricted ondelete action only changes what happens on *delete*,
# not what a new row may reference, so it needs no separate test here.


# ---------------------------------------------------------------------------
# G. MemoryRepository.get_active_for_analysis — the read `nerva_core.
#    memory_rules.decide_memory_formation` is built from (M6.3, Block 1)
# ---------------------------------------------------------------------------


def test_get_active_for_analysis_finds_the_active_fact_memory_sourced_from_it(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.7,
        source_analysis_id=analysis.id,
    )

    results = MemoryRepository(db_session, workspace.id).get_active_for_analysis(
        analysis.id, kind=MemoryKind.FACT
    )

    assert [m.id for m in results] == [memory.id]


def test_get_active_for_analysis_excludes_a_superseded_memory(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.7,
        source_analysis_id=analysis.id,
    )
    replacement = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content={"statement": "Acme's deal is now $3,000."},
        observed_at=NOW,
        confidence=0.7,
        source_analysis_id=analysis.id,
    )
    service.supersede(original.id, replacement_id=replacement.id)

    results = MemoryRepository(db_session, workspace.id).get_active_for_analysis(
        analysis.id, kind=MemoryKind.FACT
    )

    assert [m.id for m in results] == [replacement.id]


def test_get_active_for_analysis_excludes_a_discarded_memory(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.7,
        source_analysis_id=analysis.id,
    )
    service.discard(memory.id)

    results = MemoryRepository(db_session, workspace.id).get_active_for_analysis(
        analysis.id, kind=MemoryKind.FACT
    )

    assert list(results) == []


def test_get_active_for_analysis_filters_by_kind(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    service.remember(
        kind=MemoryKind.NOTE,
        origin=MemoryOrigin.OBSERVED,
        content=VALID_CONTENT,
        observed_at=NOW,
        source_analysis_id=None,
        source_conversation_id=conversation.id,
    )
    fact = service.remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.7,
        source_analysis_id=analysis.id,
    )

    results = MemoryRepository(db_session, workspace.id).get_active_for_analysis(
        analysis.id, kind=MemoryKind.FACT
    )

    assert [m.id for m in results] == [fact.id]


def test_get_active_for_analysis_is_isolated_by_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    bob_analysis = _completed_analysis(
        db_session, bob_workspace, bob_conversation, fingerprint="fp-1"
    )
    make_service(bob_workspace).remember(
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.INFERRED,
        content=VALID_CONTENT,
        observed_at=NOW,
        confidence=0.7,
        source_analysis_id=bob_analysis.id,
    )

    results = MemoryRepository(db_session, alice_workspace.id).get_active_for_analysis(
        bob_analysis.id, kind=MemoryKind.FACT
    )

    assert list(results) == []


def test_get_active_for_analysis_returns_empty_when_nothing_was_ever_recorded(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    make_service(workspace)  # no remember() call at all

    results = MemoryRepository(db_session, workspace.id).get_active_for_analysis(
        analysis.id, kind=MemoryKind.FACT
    )

    assert list(results) == []


# ---------------------------------------------------------------------------
# H. actor attribution on supersede/discard (M6.3, Block 1): an optional
#    `actor` parameter, defaulting to the authenticated caller — existing
#    calls (which never pass it) must keep behaving exactly as before.
# ---------------------------------------------------------------------------


def test_supersede_without_an_explicit_actor_still_attributes_to_the_authenticated_user(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    """Compatibility: this is the exact call shape `routes.memory.supersede_memory`
    and every pre-M6.3 test use — it must keep attributing to the human caller."""
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    replacement = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    resolved = service.supersede(original.id, replacement_id=replacement.id)

    assert resolved.status == MemoryStatus.SUPERSEDED
    event = ActivityService(db_session, workspace.id).list(entity_type="memory")[-1]
    assert event.actor_type == ActorType.USER
    assert event.actor_user_id == service.ctx.user_id


def test_supersede_with_an_explicit_nerva_actor_attributes_the_event_to_nerva(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    replacement = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    resolved = service.supersede(original.id, replacement_id=replacement.id, actor=Actor.nerva())

    assert resolved.status == MemoryStatus.SUPERSEDED
    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    supersede_event = next(
        e for e in events if e.event_type == ActivityEventType.MEMORY_SUPERSEDED.value
    )
    assert supersede_event.actor_type == ActorType.NERVA
    assert supersede_event.actor_user_id is None


def test_discard_without_an_explicit_actor_still_attributes_to_the_authenticated_user(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    resolved = service.discard(memory.id)

    assert resolved.status == MemoryStatus.DISCARDED
    event = ActivityService(db_session, workspace.id).list(entity_type="memory")[-1]
    assert event.actor_type == ActorType.USER
    assert event.actor_user_id == service.ctx.user_id


def test_discard_with_an_explicit_nerva_actor_attributes_the_event_to_nerva(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    resolved = service.discard(memory.id, reason="Superseded by newer data.", actor=Actor.nerva())

    assert resolved.status == MemoryStatus.DISCARDED
    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    discard_event = next(
        e for e in events if e.event_type == ActivityEventType.MEMORY_DISCARDED.value
    )
    assert discard_event.actor_type == ActorType.NERVA
    assert discard_event.actor_user_id is None


def test_remember_still_defaults_to_the_authenticated_actor_unaffected_by_the_new_parameter(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    """`remember` already had `actor: Actor | None = None` before M6.3 — this
    documents that `supersede`/`discard` gaining the same parameter did not
    change `remember`'s own default behavior."""
    workspace = _workspace(db_session)
    service = make_service(workspace)

    memory = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    assert memory.created_by_type == ActorType.USER
    assert memory.created_by_user_id == service.ctx.user_id


# ---------------------------------------------------------------------------
# J. MemoryService.replace() — atomic create + supersede (M6.3, Block 2).
# ---------------------------------------------------------------------------


def test_replace_creates_the_new_memory_and_supersedes_the_old_one(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    replacement = service.replace(
        original.id,
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.OBSERVED,
        content={"statement": "Acme's renewal moved to April."},
        observed_at=NOW,
    )

    assert replacement.id != original.id
    assert replacement.status == MemoryStatus.ACTIVE
    assert replacement.content == {"statement": "Acme's renewal moved to April.", "attributes": {}}
    resolved_original = service.get(original.id)
    assert resolved_original.status == MemoryStatus.SUPERSEDED
    assert resolved_original.superseded_by_id == replacement.id
    assert resolved_original.resolved_at is not None


def test_replace_records_both_activity_events_for_the_authenticated_actor(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    replacement = service.replace(
        original.id,
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.OBSERVED,
        content={"statement": "Acme's renewal moved to April."},
        observed_at=NOW,
    )

    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    recorded = [e for e in events if e.event_type == ActivityEventType.MEMORY_RECORDED.value]
    superseded = [e for e in events if e.event_type == ActivityEventType.MEMORY_SUPERSEDED.value]
    # One MEMORY_RECORDED from the original `remember` above, one more from `replace`.
    assert len(recorded) == 2
    replacement_recorded = next(e for e in recorded if e.entity_id == replacement.id)
    assert replacement_recorded.actor_type == ActorType.USER
    assert replacement_recorded.actor_user_id == service.ctx.user_id
    assert len(superseded) == 1
    assert superseded[0].entity_id == original.id
    assert superseded[0].actor_type == ActorType.USER
    assert superseded[0].actor_user_id == service.ctx.user_id
    assert superseded[0].data == {
        "from_status": "active",
        "to_status": "superseded",
        "superseded_by_id": str(replacement.id),
    }


def test_replace_with_an_explicit_nerva_actor_attributes_both_events_to_nerva(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    replacement = service.replace(
        original.id,
        kind=MemoryKind.FACT,
        origin=MemoryOrigin.OBSERVED,
        content={"statement": "Acme's renewal moved to April."},
        observed_at=NOW,
        actor=Actor.nerva(),
    )

    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    recorded = next(
        e
        for e in events
        if e.event_type == ActivityEventType.MEMORY_RECORDED.value and e.entity_id == replacement.id
    )
    superseded = next(
        e for e in events if e.event_type == ActivityEventType.MEMORY_SUPERSEDED.value
    )
    assert recorded.actor_type == ActorType.NERVA
    assert recorded.actor_user_id is None
    assert superseded.actor_type == ActorType.NERVA
    assert superseded.actor_user_id is None


def test_replace_raises_not_found_for_an_unknown_memory(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)

    with pytest.raises(NotFoundError):
        service.replace(
            uuid.uuid4(),
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            observed_at=NOW,
        )
    assert MemoryRepository(db_session, workspace.id).list() == []


def test_replace_raises_not_found_for_a_memory_in_another_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_memory = make_service(bob_workspace).remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    alice_service = make_service(alice_workspace)

    with pytest.raises(NotFoundError):
        alice_service.replace(
            bob_memory.id,
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content=VALID_CONTENT,
            observed_at=NOW,
        )
    # Bob's memory must be untouched by a replace attempt from another workspace.
    untouched = make_service(bob_workspace).get(bob_memory.id)
    assert untouched.status == MemoryStatus.ACTIVE
    assert untouched.superseded_by_id is None


def test_replace_rejects_a_memory_that_is_not_active(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    memory = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    service.discard(memory.id, reason="Already resolved.")

    with pytest.raises(InvalidStateTransitionError):
        service.replace(
            memory.id,
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content={"statement": "A different fact."},
            observed_at=NOW,
        )

    # Rejected before any new memory was created.
    assert [m.id for m in MemoryRepository(db_session, workspace.id).list()] == [memory.id]
    unchanged = service.get(memory.id)
    assert unchanged.status == MemoryStatus.DISCARDED
    assert unchanged.resolution_reason == "Already resolved."


def test_replace_fails_cleanly_on_invalid_content_leaving_the_original_untouched(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    with pytest.raises(MemoryContentError):
        service.replace(
            original.id,
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content={"statement": ""},  # violates MemoryContent's min_length
            observed_at=NOW,
        )

    unchanged = service.get(original.id)
    assert unchanged.status == MemoryStatus.ACTIVE
    assert unchanged.superseded_by_id is None
    assert [m.id for m in MemoryRepository(db_session, workspace.id).list()] == [original.id]


def test_replace_fails_cleanly_on_a_source_reference_from_another_workspace(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    alice_service = make_service(alice_workspace)
    original = alice_service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    with pytest.raises(InvalidReferenceError):
        alice_service.replace(
            original.id,
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content={"statement": "A different fact."},
            observed_at=NOW,
            source_conversation_id=bob_conversation.id,
        )

    unchanged = alice_service.get(original.id)
    assert unchanged.status == MemoryStatus.ACTIVE
    assert [m.id for m in MemoryRepository(db_session, alice_workspace.id).list()] == [original.id]


def test_replace_with_commit_false_persists_nothing_until_an_explicit_commit(
    engine: Engine,
) -> None:
    """`commit=False` must not persist any part of the operation — verified
    from a second, independent connection (not `db_session`'s savepoint, which
    a second connection would never see regardless — see the module docstring
    of `test_memory_formation.py` for why that distinction matters here)."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Replace Commit False")
    user = _user(setup)
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        session = _real_session(engine)
        try:
            service = MemoryService(session, ctx)
            original = service.remember(
                kind=MemoryKind.FACT,
                origin=MemoryOrigin.OBSERVED,
                content=VALID_CONTENT,
                observed_at=NOW,
            )
            original_id = original.id

            replacement = service.replace(
                original_id,
                kind=MemoryKind.FACT,
                origin=MemoryOrigin.OBSERVED,
                content={"statement": "Acme's renewal moved to April."},
                observed_at=NOW,
                commit=False,
            )
            replacement_id = replacement.id

            # Not yet visible from a second connection — nothing was committed.
            verify_before = _real_session(engine)
            assert verify_before.get(Memory, replacement_id) is None
            still_active_elsewhere = verify_before.get(Memory, original_id)
            assert still_active_elsewhere is not None
            assert still_active_elsewhere.status == MemoryStatus.ACTIVE
            verify_before.close()

            session.commit()  # the caller's explicit commit, deferred until now
        finally:
            session.close()

        verify_after = _real_session(engine)
        committed_replacement = verify_after.get(Memory, replacement_id)
        committed_original = verify_after.get(Memory, original_id)
        assert committed_replacement is not None
        assert committed_original is not None
        assert committed_replacement.status == MemoryStatus.ACTIVE
        assert committed_original.status == MemoryStatus.SUPERSEDED
        verify_after.close()
    finally:
        cleanup = _real_session(engine)
        cleanup.delete(cleanup.get(Workspace, workspace.id))
        cleanup.delete(cleanup.get(User, user.id))
        cleanup.commit()
        cleanup.close()


def test_replace_rolls_back_and_leaves_the_session_usable_when_remember_fails(
    db_session: Session, make_service: MakeMemoryService
) -> None:
    """`replace` must not leave the caller's session in a half-flushed,
    unusable state after a failure — it rolls back internally rather than
    relying on the caller (or the request's session dependency) to discard a
    partial flush. Proven by continuing to use the *same* session normally
    right after the failure."""
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    with pytest.raises(MemoryContentError):
        service.replace(
            original.id,
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content={"statement": ""},
            observed_at=NOW,
        )

    # The session is still perfectly usable — a real `IntegrityError`/flush
    # failure would otherwise leave it needing a rollback before any further
    # ORM operation, raising PendingRollbackError instead of this succeeding.
    another = service.remember(
        kind=MemoryKind.NOTE, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    assert another.status == MemoryStatus.ACTIVE
    assert service.get(original.id).status == MemoryStatus.ACTIVE


def test_replace_rolls_back_and_leaves_the_session_usable_when_the_savepoint_release_fails(
    db_session: Session, make_service: MakeMemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A different failure point than the test above: here `remember` and
    `supersede` both succeed, and it is releasing the SAVEPOINT itself
    (`nested.commit()`, i.e. the `RELEASE SAVEPOINT` round trip) that raises.
    Before M6.3 Block 2's fix, `nested.commit()` ran *after* the `try`/
    `except`, so this failure skipped `nested.rollback()` entirely and left
    the session pointing at a dead SAVEPOINT transaction — the next commit
    anywhere in the session (even an unrelated one) re-raised the same
    error. `nested.commit()` must be inside the `try` so this failure is
    recovered exactly like a failure in `remember`/`supersede` themselves."""
    workspace = _workspace(db_session)
    service = make_service(workspace)
    original = service.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )

    real_begin_nested = Session.begin_nested

    def _begin_nested_with_failing_release(self: Session) -> Any:
        nested = real_begin_nested(self)

        def _failing_release(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated SAVEPOINT release failure")

        nested.commit = _failing_release  # type: ignore[method-assign]
        return nested

    monkeypatch.setattr(Session, "begin_nested", _begin_nested_with_failing_release)

    with pytest.raises(RuntimeError, match="simulated SAVEPOINT release failure"):
        service.replace(
            original.id,
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content={"statement": "Acme's renewal moved to April."},
            observed_at=NOW,
        )

    # Same proof as the test above, for this different failure point: the
    # session keeps working, and the failed replace left no partial state.
    another = service.remember(
        kind=MemoryKind.NOTE, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    assert another.status == MemoryStatus.ACTIVE
    assert service.get(original.id).status == MemoryStatus.ACTIVE
    memories = MemoryRepository(db_session, workspace.id).list()
    assert {m.id for m in memories} == {original.id, another.id}


def test_an_unrelated_pending_write_survives_a_failed_replace_and_can_be_committed_after(
    engine: Engine,
) -> None:
    """A caller's own unrelated write, already pending in this session
    *before* `replace()` is even called, must survive a `replace()` failure
    and still be committable afterward. This is the exact scope-of-rollback
    bug `replace`'s SAVEPOINT strategy exists to fix: the previous
    implementation called `self.session.rollback()` on any exception, which
    discards the *entire* outer transaction — every pending write in the
    session, not just the create-and-supersede pair `replace` itself is
    responsible for. Proven with a real, independent connection so the
    caller's later commit is actually verifiable from a second connection,
    the same way `test_replace_with_commit_false_persists_nothing_until_an_
    explicit_commit` and `test_a_failure_between_creating_and_resolving_
    leaves_nothing_committed` prove the other ends of this contract."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Unrelated Write Survives")
    user = _user(setup)
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        session = _real_session(engine)
        try:
            service = MemoryService(session, ctx)
            original = service.remember(
                kind=MemoryKind.FACT,
                origin=MemoryOrigin.OBSERVED,
                content=VALID_CONTENT,
                observed_at=NOW,
            )
            original_id = original.id

            # An unrelated write, flushed and pending in this same session,
            # made *before* the failing `replace()` call below — the caller
            # never commits it until the very end.
            unrelated_contact = _contact(session, workspace, email="unrelated@example.com")
            unrelated_id = unrelated_contact.id

            with pytest.raises(MemoryContentError):
                service.replace(
                    original_id,
                    kind=MemoryKind.FACT,
                    origin=MemoryOrigin.OBSERVED,
                    content={"statement": ""},  # violates MemoryContent's min_length
                    observed_at=NOW,
                )

            # The failed replace() must not have left the session needing a
            # rollback before this, its caller's own, later commit succeeds.
            session.commit()
        finally:
            session.close()

        verify = _real_session(engine)
        assert verify.get(Contact, unrelated_id) is not None, (
            "the caller's own unrelated write must have survived replace()'s failure"
        )
        all_memories = MemoryRepository(verify, workspace.id).list()
        assert [m.id for m in all_memories] == [original_id], (
            "the failed replace must not have left a partial replacement behind"
        )
        assert all_memories[0].status == MemoryStatus.ACTIVE
        verify.close()
    finally:
        cleanup = _real_session(engine)
        cleanup.delete(cleanup.get(Workspace, workspace.id))
        cleanup.delete(cleanup.get(User, user.id))
        cleanup.commit()
        cleanup.close()


def test_a_failure_between_creating_and_resolving_leaves_nothing_committed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Forces a failure *inside* `replace`, after the new memory would have
    been created but before `supersede` resolves the old one (`MemoryService.
    supersede` raises immediately), and verifies — from a second, independent
    connection — that nothing survived: the original is still the only memory,
    still active. Mirrors `test_memory_formation.py`'s equivalent test for
    `MemoryFormationService`, generalized here to the primitive `replace`
    itself now backs."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Replace Atomicity")
    user = _user(setup)
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        session = _real_session(engine)
        try:
            service = MemoryService(session, ctx)
            original = service.remember(
                kind=MemoryKind.FACT,
                origin=MemoryOrigin.OBSERVED,
                content=VALID_CONTENT,
                observed_at=NOW,
            )
            original_id = original.id

            def _boom(*args: object, **kwargs: object) -> Memory:
                raise RuntimeError("simulated crash before the SUPERSEDE commit")

            monkeypatch.setattr(MemoryService, "supersede", _boom)

            with pytest.raises(RuntimeError, match="simulated crash"):
                service.replace(
                    original_id,
                    kind=MemoryKind.FACT,
                    origin=MemoryOrigin.OBSERVED,
                    content={"statement": "Acme's renewal moved to April."},
                    observed_at=NOW,
                )
            # `replace` rolled back internally — no caller-side rollback needed,
            # unlike the equivalent MemoryFormationService test.
        finally:
            session.close()

        verify = _real_session(engine)
        all_memories = MemoryRepository(verify, workspace.id).list()
        assert [m.id for m in all_memories] == [original_id], (
            "the uncommitted replacement must never have reached the database"
        )
        assert all_memories[0].status == MemoryStatus.ACTIVE
        events = ActivityService(verify, workspace.id).list(entity_type="memory")
        recorded = [e for e in events if e.event_type == ActivityEventType.MEMORY_RECORDED.value]
        assert len(recorded) == 1, "the replacement's MEMORY_RECORDED event must not survive either"
        verify.close()
    finally:
        cleanup = _real_session(engine)
        cleanup.delete(cleanup.get(Workspace, workspace.id))
        cleanup.delete(cleanup.get(User, user.id))
        cleanup.commit()
        cleanup.close()


def test_two_concurrent_replaces_of_the_same_memory_leave_exactly_one_winner(
    engine: Engine,
) -> None:
    """Two real connections race to `replace` the *same* already-active
    memory with different new content. `replace`'s own row lock (`get_for_
    update` on `memory_id`, same as `supersede`) serializes them: whichever
    commits first wins; the second re-reads under the lock, finds the memory
    already `superseded`, and fails with `InvalidStateTransitionError` — never
    two replacements, never a lost update. A real `threading.Barrier` proves
    both calls actually overlap rather than running one after the other by
    accident."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Concurrent Replace")
    user = _user(setup)
    service_setup = MemoryService(
        setup, WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
    )
    original = service_setup.remember(
        kind=MemoryKind.FACT, origin=MemoryOrigin.OBSERVED, content=VALID_CONTENT, observed_at=NOW
    )
    original_id = original.id
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def _call(key: str, statement: str) -> None:
            session = _real_session(engine)
            try:
                service = MemoryService(session, ctx)
                barrier.wait(timeout=10)  # both calls provably overlap here
                replacement = service.replace(
                    original_id,
                    kind=MemoryKind.FACT,
                    origin=MemoryOrigin.OBSERVED,
                    content={"statement": statement},
                    observed_at=NOW,
                )
                results[key] = replacement.id
            except BaseException as exc:  # surfaced on the main thread below
                results[key] = exc
            finally:
                session.close()

        threads = [
            threading.Thread(target=_call, args=("a", "Replaced by A.")),
            threading.Thread(target=_call, args=("b", "Replaced by B.")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        for thread in threads:
            assert not thread.is_alive(), "a thread did not finish in time"

        winners = [key for key, value in results.items() if not isinstance(value, BaseException)]
        losers = [key for key, value in results.items() if isinstance(value, BaseException)]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        assert len(losers) == 1
        assert isinstance(results[losers[0]], InvalidStateTransitionError)

        verify = _real_session(engine)
        all_memories = MemoryRepository(verify, workspace.id).list()
        assert len(all_memories) == 2  # the original (now superseded) + the one winner
        active_memories = [m for m in all_memories if m.status == MemoryStatus.ACTIVE]
        assert len(active_memories) == 1
        assert active_memories[0].id == results[winners[0]]
        original_row = verify.get(Memory, original_id)
        assert original_row is not None
        assert original_row.status == MemoryStatus.SUPERSEDED
        assert original_row.superseded_by_id == active_memories[0].id
        verify.close()

        # A third, independent session confirms the activity trail matches
        # exactly: the original's MEMORY_RECORDED (from `setup`, above) plus
        # the one winner's MEMORY_RECORDED for its replacement, and exactly one
        # MEMORY_SUPERSEDED. The loser's `replace()` call fails `ensure_
        # transition` as soon as it re-reads the row under the lock — *before*
        # opening its own SAVEPOINT or calling `remember` at all — so it must
        # leave no event and no reference to a memory that was never created.
        events_session = _real_session(engine)
        events = ActivityService(events_session, workspace.id).list(entity_type="memory")
        recorded = [e for e in events if e.event_type == ActivityEventType.MEMORY_RECORDED.value]
        superseded_events = [
            e for e in events if e.event_type == ActivityEventType.MEMORY_SUPERSEDED.value
        ]
        assert {e.entity_id for e in recorded} == {original_id, active_memories[0].id}
        assert len(superseded_events) == 1
        assert superseded_events[0].entity_id == original_id
        assert superseded_events[0].data["superseded_by_id"] == str(active_memories[0].id)
        events_session.close()
    finally:
        cleanup = _real_session(engine)
        cleanup.delete(cleanup.get(Workspace, workspace.id))
        cleanup.delete(cleanup.get(User, user.id))
        cleanup.commit()
        cleanup.close()


# ---------------------------------------------------------------------------
# I. Known limitation (M6.3, Block 2, point 3): `remember` takes no lock and
#    runs no "is one already active" check before inserting a FACT sourced
#    from an analysis, so two concurrent manual creates for the same
#    `source_analysis_id` can both become active. A write-time guard here
#    was considered and rejected for this block: it would also block the
#    documented, already-tested create-then-supersede sequence any memory
#    (FACT-from-analysis included — see section G above) can legitimately go
#    through via two separate `POST /memories` calls, since `remember` cannot
#    tell "about to be superseded" apart from "permanently orphaned" at
#    creation time. Closing this safely needs a product/schema decision
#    (e.g. an atomic create-and-supersede call) — see the Block 2 review.
#    This test documents the gap with a real, synchronized race rather than
#    asserting it away; `MemoryFormationService`'s own CREATE/SUPERSEDE path
#    is unaffected, since it always holds the `ConversationAnalysis` row lock
#    across its whole read-decide-write sequence (`nerva.services.
#    memory_formation`), and now raises a controlled `DataIntegrityError`
#    instead of crashing or guessing if it ever observes this state (see
#    `test_memory_formation.py`'s D2 section).
# ---------------------------------------------------------------------------


def _real_session(engine: Engine) -> Session:
    return Session(bind=engine, expire_on_commit=False)


def test_two_concurrent_manual_fact_creates_for_the_same_analysis_can_both_become_active(
    engine: Engine,
) -> None:
    """A real `threading.Barrier` makes both `remember` calls provably
    overlap (both reach it before either proceeds) rather than relying on a
    sleep-based guess — the same pattern `test_memory_formation.py`'s own
    multi-connection concurrency tests use. Each call runs on its own session
    against a real, separately committed row, the same way those tests open
    independent connections instead of `db_session`'s savepoint."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Known Gap")
    conversation = _conversation(setup, workspace)
    analysis = _completed_analysis(setup, workspace, conversation, fingerprint="fp-1")
    user = _user(setup)
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def _call(key: str) -> None:
            session = _real_session(engine)
            try:
                service = MemoryService(session, ctx)
                barrier.wait(timeout=10)  # both calls provably overlap here
                memory = service.remember(
                    kind=MemoryKind.FACT,
                    origin=MemoryOrigin.OBSERVED,
                    content={"statement": f"Manual fact {key}."},
                    observed_at=NOW,
                    source_analysis_id=analysis.id,
                    actor=Actor.nerva(),
                )
                results[key] = memory.id
            except BaseException as exc:  # surfaced on the main thread below
                results[key] = exc
            finally:
                session.close()

        threads = [threading.Thread(target=_call, args=(key,)) for key in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        for thread in threads:
            assert not thread.is_alive(), "a thread did not finish in time"

        for key in ("a", "b"):
            if isinstance(results[key], BaseException):
                raise results[key]  # type: ignore[misc]

        verify = _real_session(engine)
        active = MemoryRepository(verify, workspace.id).get_active_for_analysis(
            analysis.id, kind=MemoryKind.FACT
        )
        # The gap, not a guarantee: both concurrent creates succeeded, so both
        # ended up active for the same source_analysis_id. If this ever starts
        # asserting 1 instead of 2, a fix for point 3 has landed — update or
        # remove this test to match.
        assert len(active) == 2
        assert {m.id for m in active} == {results["a"], results["b"]}
        verify.close()
    finally:
        cleanup = _real_session(engine)
        cleanup.delete(cleanup.get(Workspace, workspace.id))  # cascades to every owned row
        cleanup.delete(cleanup.get(User, user.id))
        cleanup.commit()
        cleanup.close()
