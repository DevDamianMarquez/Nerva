"""`MemoryContextService`: database retrieval and `MemoryContextTarget`
construction for a conversation, against a real database (M6.4, Block 2).

Mirrors `test_memory_formation.py`/`test_memory_service.py`'s style: direct
SQLAlchemy row construction via small helpers, a `WorkspaceContext` built by
hand, and the shared `db_session` fixture (real PostgreSQL, rolled back at
teardown — see `conftest.py`). Every test here is therefore already a
PostgreSQL integration test; tenant isolation is exercised against the real
composite foreign keys, not a mock.

This module never re-implements any eligibility/ranking/truncation rule:
`nerva_core.memory_context.select_context_memories` (Block 1, already tested
exhaustively in `test_memory_context.py`) is used here unmodified, both
inside the service and, in one test below, called a second time directly
against independently-built inputs to prove the service's output matches it
exactly rather than a parallel reimplementation."""

import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import (
    ActorType,
    MemberRole,
    MemoryKind,
    MemoryOrigin,
    MemorySourceType,
)
from nerva.core.errors import NotFoundError
from nerva.models import Company, Contact, Conversation, Memory, Opportunity, User, Workspace
from nerva.nerva_core.memory_context import (
    MemoryCandidate,
    MemoryContextTarget,
    select_context_memories,
)
from nerva.services.memory import MemoryService
from nerva.services.memory_context import MemoryContextService

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Row builders — direct SQLAlchemy, mirroring test_memory_formation.py /
# test_memory_service.py.
# ---------------------------------------------------------------------------


def _workspace(db_session: Session, name: str = "Acme") -> Workspace:
    workspace = Workspace(name=name, slug=f"{name.lower()}-{uuid.uuid4().hex[:8]}")
    db_session.add(workspace)
    db_session.flush()
    return workspace


def _user(db_session: Session, name: str = "Alex") -> User:
    email = f"{name.lower()}-{uuid.uuid4().hex[:8]}@example.com"
    user = User(email=email, name=name, password_hash="x")
    db_session.add(user)
    db_session.flush()
    return user


def _ctx(db_session: Session, workspace: Workspace) -> WorkspaceContext:
    user = _user(db_session)
    return WorkspaceContext(workspace_id=workspace.id, user_id=user.id, role=MemberRole.OWNER)


def _company(db_session: Session, workspace: Workspace, name: str = "Halcyon") -> Company:
    company = Company(workspace_id=workspace.id, name=name)
    db_session.add(company)
    db_session.flush()
    return company


def _contact(
    db_session: Session,
    workspace: Workspace,
    *,
    email: str = "priya@halcyon.example",
    company_id: uuid.UUID | None = None,
) -> Contact:
    contact = Contact(workspace_id=workspace.id, email=email, company_id=company_id)
    db_session.add(contact)
    db_session.flush()
    return contact


def _conversation(
    db_session: Session,
    workspace: Workspace,
    *,
    contact_id: uuid.UUID | None = None,
    subject: str = "Renewal",
) -> Conversation:
    conversation = Conversation(workspace_id=workspace.id, subject=subject, contact_id=contact_id)
    db_session.add(conversation)
    db_session.flush()
    return conversation


def _opportunity(
    db_session: Session,
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
    db_session.add(opportunity)
    db_session.flush()
    return opportunity


def _remember(
    db_session: Session,
    ctx: WorkspaceContext,
    *,
    statement: str = "statement",
    origin: MemoryOrigin = MemoryOrigin.OBSERVED,
    confidence: float | None = None,
    kind: MemoryKind = MemoryKind.FACT,
    observed_at: datetime = NOW,
    contact_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    source_conversation_id: uuid.UUID | None = None,
) -> Memory:
    return MemoryService(db_session, ctx).remember(
        kind=kind,
        origin=origin,
        content={"statement": statement},
        observed_at=observed_at,
        confidence=confidence,
        contact_id=contact_id,
        company_id=company_id,
        opportunity_id=opportunity_id,
        source_conversation_id=source_conversation_id,
    )


MakeService = Callable[[WorkspaceContext], MemoryContextService]


@pytest.fixture
def make_service(db_session: Session) -> MakeService:
    def _make(ctx: WorkspaceContext) -> MemoryContextService:
        return MemoryContextService(db_session, ctx)

    return _make


# ---------------------------------------------------------------------------
# A. Each subject scope resolves and is included (1, 3, 4) / resolves
#    conditionally (2, with its documented limitation)
# ---------------------------------------------------------------------------


def test_a_contact_scoped_memory_is_included_for_its_own_conversation(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    memory = _remember(
        db_session, ctx, statement="Prefers email over calls.", contact_id=contact.id
    )

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert [c.id for c in result] == [memory.id]


def test_an_opportunity_scoped_memory_is_included_when_exactly_one_opportunity_links_it(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    opportunity = _opportunity(db_session, workspace, conversation_id=conversation.id)
    memory = _remember(db_session, ctx, statement="Deal worth $50k.", opportunity_id=opportunity.id)

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert [c.id for c in result] == [memory.id]


def test_opportunity_id_is_unresolved_when_two_opportunities_link_the_same_conversation(
    db_session: Session, make_service: MakeService
) -> None:
    """Documents the limitation in `services.memory_context`'s module
    docstring: `Opportunity.conversation_id` carries no unique constraint
    (`nerva.models.crm`), so a conversation linked to more than one
    opportunity has no single, unambiguous "the opportunity". This service
    deliberately leaves `opportunity_id` unresolved rather than guessing one
    of the two, so the opportunity-scoped memory is excluded here — not
    included under an arbitrarily-picked id."""
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    first = _opportunity(db_session, workspace, conversation_id=conversation.id, title="Deal A")
    _opportunity(db_session, workspace, conversation_id=conversation.id, title="Deal B")
    _remember(db_session, ctx, statement="Deal worth $50k.", opportunity_id=first.id)

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


def test_opportunity_id_is_unresolved_when_no_opportunity_links_the_conversation(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    other_conversation = _conversation(db_session, workspace, subject="Other")
    opportunity = _opportunity(db_session, workspace, conversation_id=other_conversation.id)
    _remember(db_session, ctx, statement="Deal worth $50k.", opportunity_id=opportunity.id)

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


def test_a_company_scoped_memory_is_included_via_the_conversations_contact(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    company = _company(db_session, workspace)
    contact = _contact(db_session, workspace, company_id=company.id)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    memory = _remember(
        db_session, ctx, statement="Annual budget cycle is Q1.", company_id=company.id
    )

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert [c.id for c in result] == [memory.id]


def test_a_conversation_with_no_contact_cannot_resolve_a_company(
    db_session: Session, make_service: MakeService
) -> None:
    """Without a resolved contact there is no chain to a company at all —
    `Conversation.contact_id` is the only link this service uses."""
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    company = _company(db_session, workspace)
    conversation = _conversation(db_session, workspace)  # no contact_id
    _remember(
        db_session,
        ctx,
        statement="Belongs to a company, not this conversation.",
        company_id=company.id,
    )

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


def test_a_workspace_level_memory_is_included_for_any_conversation(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    memory = _remember(db_session, ctx, statement="We only sell annual contracts.")

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert [c.id for c in result] == [memory.id]


# ---------------------------------------------------------------------------
# B. Unrelated subjects are excluded (5, 6, 7)
# ---------------------------------------------------------------------------


def test_a_memory_for_a_different_contact_is_excluded(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    contact = _contact(db_session, workspace, email="a@x.example")
    other_contact = _contact(db_session, workspace, email="b@x.example")
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    _remember(
        db_session, ctx, statement="About someone else entirely.", contact_id=other_contact.id
    )

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


def test_a_memory_for_a_different_company_is_excluded(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    company = _company(db_session, workspace, name="Acme")
    other_company = _company(db_session, workspace, name="Globex")
    contact = _contact(db_session, workspace, company_id=company.id)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    _remember(db_session, ctx, statement="About a different company.", company_id=other_company.id)

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


def test_a_memory_for_a_different_opportunity_is_excluded(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    other_conversation = _conversation(db_session, workspace, subject="Other")
    other_opportunity = _opportunity(db_session, workspace, conversation_id=other_conversation.id)
    _remember(
        db_session,
        ctx,
        statement="A different deal entirely.",
        opportunity_id=other_opportunity.id,
    )

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


# ---------------------------------------------------------------------------
# C. Lifecycle and self-reference exclusion (8, 9, 10) — enforced by Block 1,
#    exercised end to end here through real rows.
# ---------------------------------------------------------------------------


def test_a_superseded_memory_is_excluded(db_session: Session, make_service: MakeService) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    memory = _remember(db_session, ctx, statement="Old fact.")
    replacement = _remember(
        db_session, ctx, statement="Unrelated row used only as a replacement target."
    )
    MemoryService(db_session, ctx).supersede(memory.id, replacement_id=replacement.id)

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert memory.id not in [c.id for c in result]


def test_a_discarded_memory_is_excluded(db_session: Session, make_service: MakeService) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    memory = _remember(db_session, ctx, statement="Turned out to be wrong.")
    MemoryService(db_session, ctx).discard(memory.id, reason="Incorrect.")

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


def test_a_memory_sourced_from_the_same_conversation_is_excluded(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    _remember(
        db_session,
        ctx,
        statement="From this very thread.",
        source_conversation_id=conversation.id,
    )

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


def test_a_memory_sourced_from_a_different_conversation_is_not_excluded(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    other_conversation = _conversation(db_session, workspace, subject="Other")
    memory = _remember(
        db_session,
        ctx,
        statement="From a different thread.",
        source_conversation_id=other_conversation.id,
    )

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert [c.id for c in result] == [memory.id]


# ---------------------------------------------------------------------------
# D. Workspace isolation (11, 12)
# ---------------------------------------------------------------------------


def test_a_memory_in_another_workspace_is_never_retrieved(
    db_session: Session, make_service: MakeService
) -> None:
    workspace_a = _workspace(db_session, "Acme")
    ctx_a = _ctx(db_session, workspace_a)
    conversation_a = _conversation(db_session, workspace_a)
    memory_a = _remember(db_session, ctx_a, statement="Belongs to Acme.")

    workspace_b = _workspace(db_session, "Globex")
    ctx_b = _ctx(db_session, workspace_b)
    _remember(db_session, ctx_b, statement="Belongs to a completely different workspace.")

    result = make_service(ctx_a).get_context_for_conversation(
        conversation_id=conversation_a.id, max_items=50, max_chars=10_000
    )

    assert [c.id for c in result] == [memory_a.id]


class TestDatabaseEnforcesTenantBoundariesForMemoryContext:
    """Mirrors `test_workspace_isolation.py::TestDatabaseEnforcesTenantBoundaries`:
    even a `Memory` row that tried to carry another workspace's subject id
    could never be stored, because the composite foreign keys on `Memory`
    (`nerva.models.memory`) reject it in PostgreSQL itself. This service never
    has to defend against a "similar/crossed id" at the application layer —
    the database makes such a row impossible to create in the first place, so
    there is nothing for `MemoryContextService` to ever read."""

    def test_a_memory_cannot_reference_a_contact_from_another_workspace(
        self, db_session: Session
    ) -> None:
        workspace_a = _workspace(db_session, "Acme")
        workspace_b = _workspace(db_session, "Globex")
        contact_in_a = _contact(db_session, workspace_a)

        db_session.add(
            Memory(
                workspace_id=workspace_b.id,
                kind=MemoryKind.FACT,
                origin=MemoryOrigin.OBSERVED,
                content={"statement": "x"},
                schema_version=1,
                observed_at=NOW,
                contact_id=contact_in_a.id,
                source_type=MemorySourceType.MANUAL,
                created_by_type=ActorType.NERVA,
            )
        )

        with pytest.raises(IntegrityError, match="fk_memories"):
            db_session.flush()

    def test_a_memory_cannot_reference_an_opportunity_from_another_workspace(
        self, db_session: Session
    ) -> None:
        workspace_a = _workspace(db_session, "Acme")
        workspace_b = _workspace(db_session, "Globex")
        opportunity_in_a = _opportunity(db_session, workspace_a)

        db_session.add(
            Memory(
                workspace_id=workspace_b.id,
                kind=MemoryKind.FACT,
                origin=MemoryOrigin.OBSERVED,
                content={"statement": "x"},
                schema_version=1,
                observed_at=NOW,
                opportunity_id=opportunity_in_a.id,
                source_type=MemorySourceType.MANUAL,
                created_by_type=ActorType.NERVA,
            )
        )

        with pytest.raises(IntegrityError, match="fk_memories"):
            db_session.flush()


# ---------------------------------------------------------------------------
# E. Conversation resolution failures fail closed (13)
# ---------------------------------------------------------------------------


def test_an_unknown_conversation_id_raises_not_found(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)

    with pytest.raises(NotFoundError):
        make_service(ctx).get_context_for_conversation(
            conversation_id=uuid.uuid4(), max_items=50, max_chars=10_000
        )


def test_a_conversation_in_another_workspace_raises_not_found(
    db_session: Session, make_service: MakeService
) -> None:
    workspace_a = _workspace(db_session, "Acme")
    workspace_b = _workspace(db_session, "Globex")
    conversation_in_b = _conversation(db_session, workspace_b)
    ctx_a = _ctx(db_session, workspace_a)

    with pytest.raises(NotFoundError):
        make_service(ctx_a).get_context_for_conversation(
            conversation_id=conversation_in_b.id, max_items=50, max_chars=10_000
        )


# ---------------------------------------------------------------------------
# F. Legitimately empty context (14)
# ---------------------------------------------------------------------------


def test_a_conversation_with_no_relevant_memories_returns_an_empty_sequence(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert result == ()


# ---------------------------------------------------------------------------
# G. max_items / max_chars are forwarded and respected (15, 16)
# ---------------------------------------------------------------------------


def test_max_items_is_forwarded_and_respected(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    for i in range(5):
        _remember(db_session, ctx, statement=f"fact {i}", observed_at=NOW - timedelta(days=i))

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=2, max_chars=10_000
    )

    assert len(result) == 2
    assert [c.statement for c in result] == ["fact 0", "fact 1"]  # most recently-true first


def test_max_chars_is_forwarded_and_respected(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    _remember(db_session, ctx, statement="x" * 5, observed_at=NOW)
    _remember(db_session, ctx, statement="y" * 5, observed_at=NOW - timedelta(days=1))

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=5
    )

    assert len(result) == 1
    assert result[0].statement == "x" * 5


# ---------------------------------------------------------------------------
# H. Final order matches Block 1 exactly; no parallel ranking (17, 18)
# ---------------------------------------------------------------------------


def test_the_final_order_matches_block_1_priority_exactly(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    company = _company(db_session, workspace)
    contact = _contact(db_session, workspace, company_id=company.id)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    opportunity = _opportunity(db_session, workspace, conversation_id=conversation.id)

    _remember(db_session, ctx, statement="workspace")
    _remember(db_session, ctx, statement="company", company_id=company.id)
    _remember(db_session, ctx, statement="contact", contact_id=contact.id)
    _remember(db_session, ctx, statement="opportunity", opportunity_id=opportunity.id)

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert [c.statement for c in result] == ["opportunity", "contact", "company", "workspace"]


def test_service_output_matches_a_direct_call_to_select_context_memories(
    db_session: Session, make_service: MakeService
) -> None:
    """Independently rebuilds the same candidates/target the service would
    have built, and calls the Block 1 selector directly with them. Asserting
    the two results are equal proves the service delegates to
    `select_context_memories` rather than reimplementing any part of its
    ranking — a divergence here would mean the service and the selector had
    drifted apart."""
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)
    opportunity = _opportunity(db_session, workspace, conversation_id=conversation.id)

    m_opportunity = _remember(
        db_session, ctx, statement="opportunity fact", opportunity_id=opportunity.id
    )
    m_contact = _remember(db_session, ctx, statement="contact fact", contact_id=contact.id)
    m_workspace = _remember(db_session, ctx, statement="workspace fact")

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    target = MemoryContextTarget(
        conversation_id=conversation.id,
        contact_id=contact.id,
        company_id=None,
        opportunity_id=opportunity.id,
    )
    independent_candidates = [
        _candidate_from_memory(m) for m in (m_opportunity, m_contact, m_workspace)
    ]
    expected = select_context_memories(
        independent_candidates, target=target, max_items=50, max_chars=10_000
    )

    assert list(result) == list(expected)


def _candidate_from_memory(memory: Memory) -> MemoryCandidate:
    """Deliberately independent of `nerva.services.memory_context._to_candidate`
    — rebuilt here from scratch so the comparison above is a genuine
    cross-check, not the same code calling itself twice."""
    return MemoryCandidate(
        id=memory.id,
        status=memory.status,
        kind=memory.kind,
        origin=memory.origin,
        confidence=memory.confidence,
        statement=memory.content["statement"],
        observed_at=memory.observed_at,
        contact_id=memory.contact_id,
        company_id=memory.company_id,
        opportunity_id=memory.opportunity_id,
        source_conversation_id=memory.source_conversation_id,
    )


# ---------------------------------------------------------------------------
# J. Correctness fix: no candidate cap upstream of the selector
# ---------------------------------------------------------------------------


def test_a_higher_priority_memory_survives_past_more_than_200_lower_priority_candidates(
    db_session: Session, make_service: MakeService
) -> None:
    """Regression test for a correctness bug fixed in this revision:
    `MemoryRepository.list_candidates_for_context` used to cap results at an
    arbitrary 200, ordered by `created_at` descending — a `LIMIT` applied
    *before* `select_context_memories` (Block 1) ever ran. A CONTACT-scoped
    memory (Block 1 priority 1) created *before* more than 200
    WORKSPACE-scoped ones (priority 3) would therefore have been the single
    oldest row by `created_at` and fallen outside that cap, even though the
    selector ranks it first — the exact failure mode the fix's docstring
    (`nerva.repositories.workspace_data.MemoryRepository.
    list_candidates_for_context`) describes. With the cap removed, the
    service must see the complete eligible set and the higher-priority
    memory must lead the result.

    This also satisfies the second required check: the service's result must
    equal `select_context_memories` applied directly to the full, independently
    -rebuilt candidate set — not a parallel, possibly-diverging computation.
    """
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    contact = _contact(db_session, workspace)
    conversation = _conversation(db_session, workspace, contact_id=contact.id)

    # Created first, so it carries the *oldest* `created_at` of everything
    # below — exactly what a `created_at DESC LIMIT 200` cap would drop.
    contact_memory = _remember(
        db_session,
        ctx,
        statement="Always wants a phone call, never an email.",
        contact_id=contact.id,
    )
    # More than the old cap (200), all WORKSPACE-scoped (lower priority than
    # CONTACT) and all created strictly after the memory above.
    workspace_memories = [
        _remember(db_session, ctx, statement=f"workspace note {i}") for i in range(205)
    ]
    candidate_count = 1 + len(workspace_memories)
    assert candidate_count > 200  # the condition this test exists to exercise

    result = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=10, max_chars=10_000
    )

    # The higher-priority memory must not have been silently excluded.
    assert result[0].id == contact_memory.id

    # And the result must match the Block 1 selector applied to the full,
    # independently-rebuilt candidate set — not a parallel computation.
    target = MemoryContextTarget(conversation_id=conversation.id, contact_id=contact.id)
    full_candidates = [
        _candidate_from_memory(m) for m in (contact_memory, *workspace_memories)
    ]
    expected = select_context_memories(
        full_candidates, target=target, max_items=10, max_chars=10_000
    )
    assert list(result) == list(expected)


# ---------------------------------------------------------------------------
# I. Mapping preserves provenance unchanged (19)
# ---------------------------------------------------------------------------


def test_mapping_preserves_origin_confidence_kind_and_statement(
    db_session: Session, make_service: MakeService
) -> None:
    workspace = _workspace(db_session)
    ctx = _ctx(db_session, workspace)
    conversation = _conversation(db_session, workspace)
    memory = _remember(
        db_session,
        ctx,
        statement="Prefers async updates over calls.",
        origin=MemoryOrigin.INFERRED,
        confidence=0.73,
        kind=MemoryKind.PREFERENCE,
    )

    (candidate,) = make_service(ctx).get_context_for_conversation(
        conversation_id=conversation.id, max_items=50, max_chars=10_000
    )

    assert candidate.id == memory.id
    assert candidate.origin is MemoryOrigin.INFERRED
    assert candidate.confidence == 0.73
    assert candidate.kind is MemoryKind.PREFERENCE
    assert candidate.statement == "Prefers async updates over calls."
    assert candidate.observed_at == memory.observed_at
