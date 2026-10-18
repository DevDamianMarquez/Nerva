"""`MemoryFormationService`: forms/supersedes the automated `estimated_value`
fact memory from a conversation's current completed analysis, against a real
database — including true multi-connection concurrency (M6.3, Block 2).

Most tests here use the shared `db_session` fixture (one connection, rolled
back at teardown) the same way `test_memory_service.py`/`test_signal_service.py`
do. The concurrency tests at the bottom are a deliberate exception: `db_session`
wraps everything in an outer transaction that is never actually committed to
the database (services "commit" onto a savepoint instead — see its own
docstring), so a *second*, independent connection would never see any of that
data. Those tests therefore open their own sessions directly against the
session-scoped `engine` fixture, commit their setup for real, and clean up
afterward (`workspace_id`'s `ON DELETE CASCADE` removes every row that setup
created in one delete)."""

import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
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
from nerva.core.errors import DataIntegrityError, InvalidStateTransitionError, NotFoundError
from nerva.models import (
    Contact,
    Conversation,
    ConversationAnalysis,
    Memory,
    Opportunity,
    User,
    Workspace,
)
from nerva.repositories.workspace_data import ConversationAnalysisRepository, MemoryRepository
from nerva.services.activity import ActivityService
from nerva.services.memory import MemoryService
from nerva.services.memory_formation import MemoryFormationService

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _workspace(session: Session, name: str = "Acme") -> Workspace:
    slug = f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:8]}"
    workspace = Workspace(name=name, slug=slug)
    session.add(workspace)
    session.flush()
    return workspace


def _user(session: Session, name: str = "Alex") -> User:
    email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, name=name, password_hash="x")
    session.add(user)
    session.flush()
    return user


def _contact(
    session: Session, workspace: Workspace, email: str = "priya@halcyon.example"
) -> Contact:
    contact = Contact(workspace_id=workspace.id, email=email)
    session.add(contact)
    session.flush()
    return contact


def _conversation(
    session: Session,
    workspace: Workspace,
    *,
    contact_id: uuid.UUID | None = None,
    subject: str = "Renewal",
) -> Conversation:
    conversation = Conversation(
        workspace_id=workspace.id,
        channel=ConversationChannel.EMAIL,
        subject=subject,
        contact_id=contact_id,
    )
    session.add(conversation)
    session.flush()
    return conversation


def _opportunity(
    session: Session,
    workspace: Workspace,
    *,
    conversation_id: uuid.UUID | None = None,
    title: str = "Deal",
) -> Opportunity:
    opportunity = Opportunity(
        workspace_id=workspace.id,
        title=title,
        conversation_id=conversation_id,
        created_by_type=ActorType.NERVA,
    )
    session.add(opportunity)
    session.flush()
    return opportunity


def _completed_analysis(
    session: Session,
    workspace: Workspace,
    conversation: Conversation,
    *,
    fingerprint: str = "fp-1",
    estimated_value_cents: int | None = 150_000,
    value_currency: str | None = "USD",
    value_is_explicit: bool | None = True,
    confidence: float = 0.9,
) -> ConversationAnalysis:
    repo = ConversationAnalysisRepository(session, workspace.id)
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
            confidence=confidence,
            summary="Discussed renewal timing.",
            estimated_value_cents=estimated_value_cents,
            value_currency=value_currency,
            value_is_explicit=value_is_explicit,
            analyzed_at=NOW,
        )
    )
    session.flush()
    return analysis


MakeFormationService = Callable[[Workspace], MemoryFormationService]


@pytest.fixture
def make_service(db_session: Session) -> MakeFormationService:
    def _make(workspace: Workspace) -> MemoryFormationService:
        user = _user(db_session)
        ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)
        return MemoryFormationService(db_session, ctx)

    return _make


# ---------------------------------------------------------------------------
# A. Creation: an explicit estimate (observed) and an inferred one
# ---------------------------------------------------------------------------


def test_forming_memory_from_an_explicit_estimate_creates_an_observed_memory(
    db_session: Session, make_service: MakeFormationService
) -> None:
    """Uses a conversation with a contact — not about subject resolution
    itself (see the dedicated "G. Subject resolution" section below), but a
    subject must resolve for anything to be created at all (M6.4 Block 3
    correction: see `nerva.services.memory_formation`'s module docstring)."""
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        db_session, workspace, conversation, fingerprint="fp-1", value_is_explicit=True
    )
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is not None
    assert memory.status == MemoryStatus.ACTIVE
    assert memory.kind == MemoryKind.FACT
    assert memory.origin == MemoryOrigin.OBSERVED
    assert memory.confidence is None
    assert memory.source_type == MemorySourceType.ANALYSIS
    assert memory.source_analysis_id == analysis.id
    assert memory.source_fingerprint == "fp-1"
    assert memory.contact_id == contact.id
    assert memory.content["attributes"] == {
        "estimated_value_cents": 150_000,
        "value_currency": "USD",
    }
    assert memory.created_by_type == ActorType.NERVA
    assert memory.created_by_user_id is None


def test_forming_memory_from_an_inferred_estimate_creates_an_inferred_memory_with_confidence(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    _completed_analysis(
        db_session, workspace, conversation, value_is_explicit=False, confidence=0.65
    )
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is not None
    assert memory.origin == MemoryOrigin.INFERRED
    assert memory.confidence == 0.65


def test_the_memorys_subject_falls_back_to_contact_when_no_opportunity_resolves(
    db_session: Session, make_service: MakeFormationService
) -> None:
    """Priority order (M6.4 Block 3 correction): opportunity, then contact.
    No opportunity is linked here, so contact is the correct fallback."""
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is not None
    assert memory.contact_id == contact.id
    assert memory.company_id is None
    assert memory.opportunity_id is None


# ---------------------------------------------------------------------------
# A2. Subject resolution policy (M6.4 Block 3 correction): opportunity,
#     then contact, then no memory at all — never a WORKSPACE-level
#     estimated_value memory. See `nerva.services.memory_formation`'s
#     module docstring, "Subject resolution".
# ---------------------------------------------------------------------------


def test_exactly_one_opportunity_takes_priority_over_a_known_contact(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    opportunity = _opportunity(db_session, workspace, conversation_id=conversation.id)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is not None
    assert memory.opportunity_id == opportunity.id
    assert memory.contact_id is None
    assert memory.company_id is None


def test_exactly_one_opportunity_resolves_even_with_no_known_contact(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)  # no contact_id
    opportunity = _opportunity(db_session, workspace, conversation_id=conversation.id)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is not None
    assert memory.opportunity_id == opportunity.id
    assert memory.contact_id is None


def test_multiple_linked_opportunities_never_pick_one_arbitrarily_falls_back_to_contact(
    db_session: Session, make_service: MakeFormationService
) -> None:
    """`Opportunity.conversation_id` carries no unique constraint
    (`nerva.models.crm`) — a conversation may legitimately have more than
    one. This module never picks one arbitrarily; with a known contact, it
    falls back to it instead (same priority order as when no opportunity
    resolves at all)."""
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    _opportunity(db_session, workspace, conversation_id=conversation.id, title="Deal A")
    _opportunity(db_session, workspace, conversation_id=conversation.id, title="Deal B")
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is not None
    assert memory.contact_id == contact.id
    assert memory.opportunity_id is None


def test_multiple_linked_opportunities_and_no_contact_forms_no_memory(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)  # no contact_id
    _opportunity(db_session, workspace, conversation_id=conversation.id, title="Deal A")
    _opportunity(db_session, workspace, conversation_id=conversation.id, title="Deal B")
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is None
    assert MemoryRepository(db_session, workspace.id).list() == []


def test_no_contact_and_no_opportunity_forms_no_memory(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)  # no contact_id, no opportunity
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)

    assert memory is None
    assert MemoryRepository(db_session, workspace.id).list() == []


def test_declining_to_form_for_lack_of_subject_still_returns_none_on_a_later_call(
    db_session: Session, make_service: MakeFormationService
) -> None:
    """Safe to call repeatedly: a conversation that never resolves a subject
    stays a no-op every time, never raises, and never retroactively guesses
    a subject from a later call alone (nothing about the conversation
    changed between calls here)."""
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)

    first = service.form_estimated_value_memory(conversation.id)
    second = service.form_estimated_value_memory(conversation.id)

    assert first is None
    assert second is None


def test_no_estimated_value_memory_ever_has_no_subject_at_all(
    db_session: Session, make_service: MakeFormationService
) -> None:
    """Cross-check over every scenario in this section: whenever a memory
    *is* formed, it always has exactly one of `contact_id`/`opportunity_id`
    set — never all three subject columns `NULL` (which `nerva.models.
    memory`'s own docstring defines as "concerns the workspace as a
    whole") — because an `estimated_value` fact is never that."""
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)

    with_contact = _conversation(db_session, workspace, contact_id=contact.id, subject="A")
    _completed_analysis(db_session, workspace, with_contact, fingerprint="fp-a")

    without_subject = _conversation(db_session, workspace, subject="B")
    _completed_analysis(db_session, workspace, without_subject, fingerprint="fp-b")

    service = make_service(workspace)
    service.form_estimated_value_memory(with_contact.id)
    service.form_estimated_value_memory(without_subject.id)

    for memory in MemoryRepository(db_session, workspace.id).list():
        assert memory.contact_id is not None or memory.opportunity_id is not None, (
            f"memory {memory.id} has no subject at all — it would be treated as "
            "WORKSPACE-level, which an estimated_value fact must never be"
        )


def test_a_changed_estimate_supersedes_under_the_same_opportunity_subject(
    db_session: Session, make_service: MakeFormationService
) -> None:
    """Mirrors `test_a_changed_estimate_supersedes_the_previous_active_memory`
    but for the opportunity-scoped path: supersession pins the subject,
    never re-resolving it (see the module docstring)."""
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    opportunity = _opportunity(db_session, workspace, conversation_id=conversation.id)
    analysis = _completed_analysis(
        db_session, workspace, conversation, fingerprint="fp-1", estimated_value_cents=150_000
    )
    service = make_service(workspace)
    original = service.form_estimated_value_memory(conversation.id)
    assert original is not None
    assert original.opportunity_id == opportunity.id

    analysis.input_fingerprint = "fp-2"
    analysis.estimated_value_cents = 300_000
    db_session.flush()

    replacement = service.form_estimated_value_memory(conversation.id)

    assert replacement is not None
    assert replacement.id != original.id
    assert replacement.opportunity_id == opportunity.id
    assert replacement.contact_id is None


# ---------------------------------------------------------------------------
# B. Idempotency: calling again for the same completed analysis
# ---------------------------------------------------------------------------


def test_calling_again_for_the_same_completed_analysis_does_not_duplicate(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    first = service.form_estimated_value_memory(conversation.id)
    assert first is not None

    second = service.form_estimated_value_memory(conversation.id)

    assert second is not None
    assert second.id == first.id
    assert len(MemoryRepository(db_session, workspace.id).list()) == 1


# ---------------------------------------------------------------------------
# C. A changed estimate supersedes the previous active memory
# ---------------------------------------------------------------------------


def test_a_changed_estimate_supersedes_the_previous_active_memory(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        db_session, workspace, conversation, fingerprint="fp-1", estimated_value_cents=150_000
    )
    service = make_service(workspace)
    original = service.form_estimated_value_memory(conversation.id)
    assert original is not None

    analysis.input_fingerprint = "fp-2"
    analysis.estimated_value_cents = 300_000
    db_session.flush()

    replacement = service.form_estimated_value_memory(conversation.id)

    assert replacement is not None
    assert replacement.id != original.id
    assert replacement.source_fingerprint == "fp-2"
    assert replacement.content["attributes"]["estimated_value_cents"] == 300_000
    assert original.status == MemoryStatus.SUPERSEDED
    assert original.superseded_by_id == replacement.id
    # Subject is pinned across the supersession — never re-resolved (M6.4
    # Block 3 correction: see the module docstring's "Subject resolution").
    assert original.contact_id == contact.id
    assert replacement.contact_id == contact.id

    active = MemoryRepository(db_session, workspace.id).get_active_for_analysis(
        analysis.id, kind=MemoryKind.FACT
    )
    assert [m.id for m in active] == [replacement.id]


def test_a_changed_currency_alone_also_supersedes(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        db_session,
        workspace,
        conversation,
        fingerprint="fp-1",
        estimated_value_cents=150_000,
        value_currency="USD",
    )
    service = make_service(workspace)
    original = service.form_estimated_value_memory(conversation.id)
    assert original is not None

    analysis.input_fingerprint = "fp-2"
    analysis.value_currency = "EUR"  # same cents, different currency
    db_session.flush()

    replacement = service.form_estimated_value_memory(conversation.id)

    assert replacement is not None
    assert replacement.id != original.id
    assert replacement.content["attributes"]["value_currency"] == "EUR"


def test_a_chain_of_value_changes_leaves_exactly_one_active_memory(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        db_session, workspace, conversation, fingerprint="fp-1", estimated_value_cents=100_000
    )
    service = make_service(workspace)
    m1 = service.form_estimated_value_memory(conversation.id)
    assert m1 is not None

    analysis.input_fingerprint = "fp-2"
    analysis.estimated_value_cents = 200_000
    db_session.flush()
    m2 = service.form_estimated_value_memory(conversation.id)
    assert m2 is not None

    analysis.input_fingerprint = "fp-3"
    analysis.estimated_value_cents = 300_000
    db_session.flush()
    m3 = service.form_estimated_value_memory(conversation.id)
    assert m3 is not None

    assert m1.status == MemoryStatus.SUPERSEDED
    assert m2.status == MemoryStatus.SUPERSEDED
    assert m3.status == MemoryStatus.ACTIVE
    assert m1.superseded_by_id == m2.id
    assert m2.superseded_by_id == m3.id
    active = MemoryRepository(db_session, workspace.id).get_active_for_analysis(
        analysis.id, kind=MemoryKind.FACT
    )
    assert [m.id for m in active] == [m3.id]


# ---------------------------------------------------------------------------
# D. A later analysis with no valid estimate never auto-discards
# ---------------------------------------------------------------------------


def test_a_later_analysis_with_no_valid_estimate_does_not_discard_the_existing_memory(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    original = service.form_estimated_value_memory(conversation.id)
    assert original is not None

    analysis.input_fingerprint = "fp-2"
    analysis.estimated_value_cents = None
    analysis.value_currency = None
    analysis.value_is_explicit = None
    db_session.flush()

    result = service.form_estimated_value_memory(conversation.id)

    assert result is not None
    assert result.id == original.id
    assert result.status == MemoryStatus.ACTIVE


# ---------------------------------------------------------------------------
# D2. Data integrity: an inconsistent pre-existing state must raise a
#     controlled error, not pick a row arbitrarily, not modify data, and not
#     rely on assertions a `python -O` run would silently skip (M6.3, Block 2).
# ---------------------------------------------------------------------------


def _direct_active_fact_memory(
    session: Session,
    workspace: Workspace,
    analysis: ConversationAnalysis,
    *,
    content: Mapping[str, object],
    fingerprint: str | None = "fp-direct",
) -> Memory:
    """Inserts a `Memory` row directly via `MemoryRepository`, bypassing
    `MemoryService`/`MemoryFormationService` entirely — standing in for
    "something else wrote a conflicting FACT memory for this analysis" (e.g. a
    manual `POST /memories` write with the same `source_analysis_id`), which
    nothing currently prevents (see M6.3 Block 2's review of point 3) and which
    `form_estimated_value_memory` must therefore detect rather than assume
    away."""
    memory = MemoryRepository(session, workspace.id).add(
        Memory(
            kind=MemoryKind.FACT,
            origin=MemoryOrigin.OBSERVED,
            content=dict(content),
            schema_version=1,
            observed_at=NOW,
            source_type=MemorySourceType.ANALYSIS,
            source_analysis_id=analysis.id,
            source_fingerprint=fingerprint,
            created_by_type=ActorType.NERVA,
        )
    )
    session.flush()
    return memory


def test_raises_data_integrity_error_when_two_active_fact_memories_exist_for_the_analysis(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    valid_content = {
        "statement": "Estimated deal value: USD 1,500.00.",
        "attributes": {"estimated_value_cents": 150_000, "value_currency": "USD"},
    }
    first = _direct_active_fact_memory(
        db_session, workspace, analysis, content=valid_content, fingerprint="fp-old-a"
    )
    second = _direct_active_fact_memory(
        db_session, workspace, analysis, content=valid_content, fingerprint="fp-old-b"
    )
    service = make_service(workspace)

    with pytest.raises(DataIntegrityError):
        service.form_estimated_value_memory(conversation.id)

    # Never picked one arbitrarily, and never touched either row.
    for memory_id in (first.id, second.id):
        memory = db_session.get(Memory, memory_id)
        assert memory is not None
        assert memory.status == MemoryStatus.ACTIVE
        assert memory.superseded_by_id is None


def test_raises_data_integrity_error_when_the_active_facts_content_does_not_match_the_shape(
    db_session: Session, make_service: MakeFormationService
) -> None:
    """A `FACT` memory with the right `source_analysis_id` but attributes that
    were never written by this module (e.g. a human's manual note, not an
    estimated_value) must not crash on a bare assertion or be silently
    coerced — it must raise a clear, typed error."""
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    _direct_active_fact_memory(
        db_session,
        workspace,
        analysis,
        content={"statement": "Acme prefers async updates.", "attributes": {}},
    )
    service = make_service(workspace)

    with pytest.raises(DataIntegrityError):
        service.form_estimated_value_memory(conversation.id)


def test_raises_data_integrity_error_when_a_completed_analysis_is_missing_analyzed_at(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    analysis.analyzed_at = None  # not DB-enforced — see ConversationAnalysis's columns
    db_session.flush()
    service = make_service(workspace)

    with pytest.raises(DataIntegrityError):
        service.form_estimated_value_memory(conversation.id)


def test_raises_data_integrity_error_when_a_completed_analysis_is_missing_confidence(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    analysis.confidence = None
    db_session.flush()
    service = make_service(workspace)

    with pytest.raises(DataIntegrityError):
        service.form_estimated_value_memory(conversation.id)


# ---------------------------------------------------------------------------
# D2b. Known limitation (M6.3, Block 2, point 3 / Block 3 audit): a *genuine*
#      external race on the active memory itself — not a pre-existing data
#      inconsistency — surfaces as `InvalidStateTransitionError`, not
#      `DataIntegrityError`. `get_active_for_analysis` (the read the SUPERSEDE
#      decision is made from) takes no row lock on the `Memory` it returns —
#      only `ConversationAnalysis`'s row is locked (see the module docstring).
#      If another caller resolves that exact memory (e.g. a human calling
#      `POST /memories/{id}/replace` or `/supersede` directly) between this
#      read and `MemoryService.replace`'s own locking re-read, that re-read
#      correctly observes the now-stale status and raises
#      `InvalidStateTransitionError` via `ensure_transition` — never silently
#      overwriting a resolution that already happened, and never corrupting
#      data. `routes/analysis.py`'s backfill must isolate this exception the
#      same way it isolates `DataIntegrityError` (see `test_analysis_api.py`).
# ---------------------------------------------------------------------------


def test_a_concurrent_external_resolution_of_the_active_memory_raises_invalid_state_transition(
    db_session: Session, make_service: MakeFormationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates the race deterministically rather than with true threading
    (unlike the multi-connection tests below): nothing but this one read is
    unlocked at that point in `form_estimated_value_memory`, so splicing a
    real, synchronous resolution into the exact gap between the read and the
    write — on the same session — reproduces the identical observable
    interleaving a genuinely concurrent second connection would, without the
    cost of a real thread/Barrier for a result that does not depend on actual
    parallel execution to be true."""
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        db_session, workspace, conversation, fingerprint="fp-1", estimated_value_cents=100_000
    )
    service = make_service(workspace)
    original = service.form_estimated_value_memory(conversation.id)
    assert original is not None

    analysis.input_fingerprint = "fp-2"
    analysis.estimated_value_cents = 200_000
    db_session.flush()

    original_get_active = MemoryRepository.get_active_for_analysis

    def _read_then_race(self: MemoryRepository, *args: object, **kwargs: object) -> object:
        rows = original_get_active(self, *args, **kwargs)
        # Another request resolving this exact memory in the gap between this
        # (unlocked) read and `replace()`'s own locking re-read.
        service.memory_service.discard(original.id)
        return rows

    monkeypatch.setattr(MemoryRepository, "get_active_for_analysis", _read_then_race)

    with pytest.raises(InvalidStateTransitionError):
        service.form_estimated_value_memory(conversation.id)

    # Nothing corrupted: the externally-discarded memory stays discarded, and
    # no orphaned replacement was ever created for it.
    all_memories = MemoryRepository(db_session, workspace.id).list()
    assert len(all_memories) == 1
    assert all_memories[0].id == original.id
    assert all_memories[0].status == MemoryStatus.DISCARDED


# ---------------------------------------------------------------------------
# D3. Transaction boundary: a SUPERSEDE decision is exactly one commit — the
#     new memory, its MEMORY_RECORDED event, the old memory's resolution, and
#     its MEMORY_SUPERSEDED event all land together or not at all (M6.3,
#     Block 2).
# ---------------------------------------------------------------------------


def test_supersede_decision_passes_commit_false_to_both_remember_and_supersede(
    db_session: Session, make_service: MakeFormationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spies on the exact `commit=` kwargs `form_estimated_value_memory` passes
    down for a SUPERSEDE decision. `MemoryService.replace` (M6.3, Block 2) is
    what orchestrates `remember` and `supersede` here, not this method
    directly: both run with `commit=False`, inside the SAVEPOINT `replace`
    opens for the pair (see `MemoryService.replace`'s docstring for why), and
    `replace` performs the one real commit itself — outside that SAVEPOINT —
    only when its own `commit=True` (passed here) was requested. This pins
    that call shape so a future edit that makes either inner call commit on
    its own, defeating the SAVEPOINT's atomicity, is caught immediately."""
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        db_session, workspace, conversation, fingerprint="fp-1", estimated_value_cents=100_000
    )
    service = make_service(workspace)
    service.form_estimated_value_memory(conversation.id)
    analysis.input_fingerprint = "fp-2"
    analysis.estimated_value_cents = 200_000
    db_session.flush()

    remember_commits: list[object] = []
    supersede_commits: list[object] = []
    original_remember = MemoryService.remember
    original_supersede = MemoryService.supersede

    def _spy_remember(self: MemoryService, *args: object, **kwargs: object) -> Memory:
        remember_commits.append(kwargs.get("commit"))
        return original_remember(self, *args, **kwargs)  # type: ignore[arg-type]

    def _spy_supersede(self: MemoryService, *args: object, **kwargs: object) -> Memory:
        supersede_commits.append(kwargs.get("commit"))
        return original_supersede(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(MemoryService, "remember", _spy_remember)
    monkeypatch.setattr(MemoryService, "supersede", _spy_supersede)

    service.form_estimated_value_memory(conversation.id)

    assert remember_commits == [False]
    assert supersede_commits == [False]


def test_a_failure_between_remember_and_supersede_leaves_nothing_committed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If anything raises between `remember(commit=False)` creating the
    replacement and `supersede(commit=False)` resolving the original, `replace`'s
    own SAVEPOINT must roll back — never a replacement that exists without the
    original ever being resolved, and never the original marked superseded
    without a replacement to point to. Forces that failure directly
    (`MemoryService.supersede` raises before doing anything) and
    verifies, from a second, independent connection (a real commit/rollback,
    not `db_session`'s savepoint — see the module docstring), that nothing
    survived: the original memory is still the only one, still active."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Atomicity")
    user = _user(setup)
    contact = _contact(setup, workspace)
    conversation = _conversation(setup, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        setup, workspace, conversation, fingerprint="fp-1", estimated_value_cents=100_000
    )
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        session = _real_session(engine)
        try:
            service = MemoryFormationService(session, ctx)
            original = service.form_estimated_value_memory(conversation.id)
            assert original is not None
            original_id = original.id  # read before the rollback below expires it

            analysis_row = session.get(ConversationAnalysis, analysis.id)
            assert analysis_row is not None
            analysis_row.input_fingerprint = "fp-2"
            analysis_row.estimated_value_cents = 200_000
            session.commit()

            def _boom(*args: object, **kwargs: object) -> Memory:
                raise RuntimeError("simulated crash before the SUPERSEDE commit")

            monkeypatch.setattr(MemoryService, "supersede", _boom)

            with pytest.raises(RuntimeError, match="simulated crash"):
                service.form_estimated_value_memory(conversation.id)
            # No explicit rollback needed here: `MemoryService.replace` (which
            # `form_estimated_value_memory`'s SUPERSEDE path calls into) already
            # rolled back its own SAVEPOINT internally before re-raising, so the
            # session is left exactly as it was before this call — see
            # `MemoryService.replace`'s docstring.
        finally:
            session.close()

        verify = _real_session(engine)
        active = MemoryRepository(verify, workspace.id).get_active_for_analysis(
            analysis.id, kind=MemoryKind.FACT
        )
        assert [m.id for m in active] == [original_id], (
            "a crash between remember and supersede must leave the original memory "
            "as the sole active one — no orphaned replacement, no partial resolution"
        )
        all_memories = MemoryRepository(verify, workspace.id).list()
        assert [m.id for m in all_memories] == [original_id], (
            "the uncommitted replacement must never have reached the database"
        )
        events = ActivityService(verify, workspace.id).list(entity_type="memory")
        recorded = [e for e in events if e.event_type == ActivityEventType.MEMORY_RECORDED.value]
        assert len(recorded) == 1, "the replacement's MEMORY_RECORDED event must not survive either"
        verify.close()
    finally:
        _cleanup(setup, workspace, user)


def test_returns_none_when_there_is_no_completed_analysis(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    conversation = _conversation(db_session, workspace)
    service = make_service(workspace)

    assert service.form_estimated_value_memory(conversation.id) is None


def test_raises_not_found_for_a_conversation_outside_the_workspace(
    db_session: Session, make_service: MakeFormationService
) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    bob_conversation = _conversation(db_session, bob_workspace)
    service = make_service(alice_workspace)

    with pytest.raises(NotFoundError):
        service.form_estimated_value_memory(bob_conversation.id)


# ---------------------------------------------------------------------------
# E. Isolation and activity attribution
# ---------------------------------------------------------------------------


def test_is_isolated_by_workspace(db_session: Session, make_service: MakeFormationService) -> None:
    alice_workspace = _workspace(db_session, "Alice Co")
    bob_workspace = _workspace(db_session, "Bob Co")
    alice_contact = _contact(db_session, alice_workspace)
    bob_contact = _contact(db_session, bob_workspace)
    alice_conversation = _conversation(db_session, alice_workspace, contact_id=alice_contact.id)
    bob_conversation = _conversation(db_session, bob_workspace, contact_id=bob_contact.id)
    _completed_analysis(db_session, alice_workspace, alice_conversation, fingerprint="fp-alice")
    _completed_analysis(db_session, bob_workspace, bob_conversation, fingerprint="fp-bob")

    make_service(alice_workspace).form_estimated_value_memory(alice_conversation.id)
    make_service(bob_workspace).form_estimated_value_memory(bob_conversation.id)

    alice_memories = MemoryRepository(db_session, alice_workspace.id).list()
    bob_memories = MemoryRepository(db_session, bob_workspace.id).list()
    assert len(alice_memories) == 1
    assert len(bob_memories) == 1
    assert alice_memories[0].workspace_id == alice_workspace.id
    assert bob_memories[0].workspace_id == bob_workspace.id


def test_records_an_activity_event_attributed_to_nerva_on_create(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    _completed_analysis(db_session, workspace, conversation)
    service = make_service(workspace)

    memory = service.form_estimated_value_memory(conversation.id)
    assert memory is not None

    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    recorded = [e for e in events if e.event_type == ActivityEventType.MEMORY_RECORDED.value]
    assert len(recorded) == 1
    assert recorded[0].entity_id == memory.id
    assert recorded[0].actor_type == ActorType.NERVA
    assert recorded[0].actor_user_id is None


def test_records_activity_events_attributed_to_nerva_on_supersede(
    db_session: Session, make_service: MakeFormationService
) -> None:
    workspace = _workspace(db_session)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    analysis = _completed_analysis(db_session, workspace, conversation, fingerprint="fp-1")
    service = make_service(workspace)
    original = service.form_estimated_value_memory(conversation.id)
    assert original is not None

    analysis.input_fingerprint = "fp-2"
    analysis.estimated_value_cents = 999_900
    db_session.flush()
    replacement = service.form_estimated_value_memory(conversation.id)
    assert replacement is not None

    events = ActivityService(db_session, workspace.id).list(entity_type="memory")
    superseded = [e for e in events if e.event_type == ActivityEventType.MEMORY_SUPERSEDED.value]
    assert len(superseded) == 1
    assert superseded[0].entity_id == original.id
    assert superseded[0].actor_type == ActorType.NERVA
    assert superseded[0].actor_user_id is None
    recorded = [e for e in events if e.event_type == ActivityEventType.MEMORY_RECORDED.value]
    assert len(recorded) == 2  # the original creation, and the replacement


# ---------------------------------------------------------------------------
# F. Real, multi-connection concurrency.
#
# `db_session` cannot be used here: it wraps the whole test in an outer
# transaction that is rolled back at teardown, and a service's "commit" inside
# it is only a savepoint release (see tests/conftest.py) — never visible to a
# second, independent connection. These tests open their own sessions directly
# against the session-scoped `engine` fixture, commit their own setup for
# real, and clean up afterward via `Workspace`'s `ON DELETE CASCADE`.
# ---------------------------------------------------------------------------


def _real_session(engine: Engine) -> Session:
    return Session(bind=engine, expire_on_commit=False)


def _cleanup(session: Session, workspace: Workspace, user: User) -> None:
    session.delete(session.get(Workspace, workspace.id))  # cascades to every owned row
    session.delete(session.get(User, user.id))
    session.commit()
    session.close()


def _run_and_join(threads: list[threading.Thread], *, timeout: float = 30) -> None:
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
    for thread in threads:
        assert not thread.is_alive(), "a thread did not finish in time — likely stuck on a lock"


def test_two_concurrent_calls_for_the_same_fingerprint_never_duplicate(engine: Engine) -> None:
    setup = _real_session(engine)
    workspace = _workspace(setup, "Concurrency Same FP")
    user = _user(setup)
    contact = _contact(setup, workspace)
    conversation = _conversation(setup, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        setup, workspace, conversation, fingerprint="fp-1", estimated_value_cents=150_000
    )
    setup.commit()
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def _call(key: str) -> None:
            session = _real_session(engine)
            try:
                barrier.wait(timeout=10)
                service = MemoryFormationService(session, ctx)
                memory = service.form_estimated_value_memory(conversation.id)
                results[key] = memory.id if memory is not None else None
            except BaseException as exc:  # surfaced on the main thread below
                results[key] = exc
            finally:
                session.close()

        threads = [threading.Thread(target=_call, args=(key,)) for key in ("a", "b")]
        _run_and_join(threads)

        for key in ("a", "b"):
            if isinstance(results[key], BaseException):
                raise results[key]  # type: ignore[misc]

        verify = _real_session(engine)
        active = MemoryRepository(verify, workspace.id).get_active_for_analysis(
            analysis.id, kind=MemoryKind.FACT
        )
        assert len(active) == 1, "two concurrent calls for the same fingerprint created duplicates"
        assert results["a"] == active[0].id
        assert results["b"] == active[0].id
        all_memories = MemoryRepository(verify, workspace.id).list()
        assert len(all_memories) == 1
        events = ActivityService(verify, workspace.id).list(entity_type="memory")
        recorded = [e for e in events if e.event_type == ActivityEventType.MEMORY_RECORDED.value]
        assert len(recorded) == 1
        verify.close()
    finally:
        _cleanup(setup, workspace, user)


def test_two_different_fingerprints_racing_never_leave_two_active_or_a_stale_winner(
    engine: Engine,
) -> None:
    """Simulates two analyses with different fingerprints completing in an
    order that is not known in advance (both `UPDATE` the same, single
    `ConversationAnalysis` row — Postgres's own row lock serializes that
    write, exactly as `AnalysisService._persist_success` already relies on —
    then both race to form a memory from it). Whichever update actually wins
    is not asserted (that would make the test flaky by construction); what is
    asserted is the invariant that must hold regardless: exactly one active
    memory exists afterward, and it matches whatever the analysis row
    *actually* ends up holding — never a value that lost the race, and never
    two contradictory active memories."""
    setup = _real_session(engine)
    workspace = _workspace(setup, "Concurrency Diff FP")
    user = _user(setup)
    contact = _contact(setup, workspace)
    conversation = _conversation(setup, workspace, contact_id=contact.id)
    analysis = _completed_analysis(
        setup, workspace, conversation, fingerprint="fp-start", estimated_value_cents=1_000
    )
    setup.commit()
    analysis_id = analysis.id
    ctx = WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)

    try:
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def _update_then_form(key: str, fingerprint: str, cents: int) -> None:
            session = _real_session(engine)
            try:
                row = session.get(ConversationAnalysis, analysis_id)
                assert row is not None
                row.input_fingerprint = fingerprint
                row.estimated_value_cents = cents
                session.commit()  # last writer between "a" and "b" wins, deterministically

                barrier.wait(timeout=10)

                service = MemoryFormationService(session, ctx)
                memory = service.form_estimated_value_memory(conversation.id)
                results[key] = memory.id if memory is not None else None
            except BaseException as exc:
                results[key] = exc
            finally:
                session.close()

        threads = [
            threading.Thread(target=_update_then_form, args=("a", "fp-a", 111_00)),
            threading.Thread(target=_update_then_form, args=("b", "fp-b", 222_00)),
        ]
        _run_and_join(threads)

        for key in ("a", "b"):
            if isinstance(results[key], BaseException):
                raise results[key]  # type: ignore[misc]

        verify = _real_session(engine)
        final_analysis = verify.get(ConversationAnalysis, analysis_id)
        assert final_analysis is not None
        active = MemoryRepository(verify, workspace.id).get_active_for_analysis(
            analysis_id, kind=MemoryKind.FACT
        )
        assert len(active) == 1, "the race left two (or zero) active memories"
        winner = active[0]
        assert winner.source_fingerprint == final_analysis.input_fingerprint
        assert (
            winner.content["attributes"]["estimated_value_cents"]
            == final_analysis.estimated_value_cents
        )
        # Both calls observed *some* outcome consistent with the single winner —
        # neither silently created a second, now-superseded-or-orphaned memory
        # carrying the losing fingerprint's value.
        all_memories = MemoryRepository(verify, workspace.id).list()
        for m in all_memories:
            if m.id != winner.id:
                assert m.status == MemoryStatus.SUPERSEDED
                assert m.superseded_by_id == winner.id
        verify.close()
    finally:
        _cleanup(setup, workspace, user)
