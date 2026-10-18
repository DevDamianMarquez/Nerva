"""`nerva_core.memory_context`: pure, no database, no ORM, no AI client, no I/O.
`select_context_memories` is the only code allowed to decide which already-
active memories are relevant enough, and in what order, to be considered for a
future prompt (M6.4, Block 1 — selection only)."""

import ast
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import nerva.nerva_core.memory_context as memory_context_module
from nerva.core.enums import MemoryKind, MemoryOrigin, MemoryStatus
from nerva.nerva_core.memory_context import (
    MemoryCandidate,
    MemoryContextTarget,
    MemoryScope,
    classify_scope,
    select_context_memories,
)

CONVERSATION_ID = uuid.uuid4()
OPPORTUNITY_ID = uuid.uuid4()
CONTACT_ID = uuid.uuid4()
COMPANY_ID = uuid.uuid4()
OTHER_OPPORTUNITY_ID = uuid.uuid4()
OTHER_CONTACT_ID = uuid.uuid4()
OTHER_COMPANY_ID = uuid.uuid4()

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

TARGET = MemoryContextTarget(
    conversation_id=CONVERSATION_ID,
    contact_id=CONTACT_ID,
    company_id=COMPANY_ID,
    opportunity_id=OPPORTUNITY_ID,
)


def _candidate(
    *,
    id: uuid.UUID | None = None,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    kind: MemoryKind = MemoryKind.FACT,
    origin: MemoryOrigin = MemoryOrigin.OBSERVED,
    confidence: float | None = None,
    statement: str = "statement",
    observed_at: datetime = NOW,
    contact_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
    source_conversation_id: uuid.UUID | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        id=id if id is not None else uuid.uuid4(),
        status=status,
        kind=kind,
        origin=origin,
        confidence=confidence,
        statement=statement,
        observed_at=observed_at,
        contact_id=contact_id,
        company_id=company_id,
        opportunity_id=opportunity_id,
        source_conversation_id=source_conversation_id,
    )


def _select(
    candidates: list[MemoryCandidate],
    *,
    target: MemoryContextTarget = TARGET,
    max_items: int = 50,
    max_chars: int = 10_000,
) -> list[MemoryCandidate]:
    return list(
        select_context_memories(candidates, target=target, max_items=max_items, max_chars=max_chars)
    )


# ---------------------------------------------------------------------------
# A. classify_scope: exact-match eligibility, one scope per subject
# ---------------------------------------------------------------------------


def test_opportunity_subject_matching_target_classifies_as_opportunity_scope() -> None:
    candidate = _candidate(opportunity_id=OPPORTUNITY_ID)
    assert classify_scope(candidate, TARGET) is MemoryScope.OPPORTUNITY


def test_opportunity_subject_not_matching_target_is_excluded() -> None:
    candidate = _candidate(opportunity_id=OTHER_OPPORTUNITY_ID)
    assert classify_scope(candidate, TARGET) is None


def test_contact_subject_matching_target_classifies_as_contact_scope() -> None:
    candidate = _candidate(contact_id=CONTACT_ID)
    assert classify_scope(candidate, TARGET) is MemoryScope.CONTACT


def test_contact_subject_not_matching_target_is_excluded() -> None:
    candidate = _candidate(contact_id=OTHER_CONTACT_ID)
    assert classify_scope(candidate, TARGET) is None


def test_company_subject_matching_target_classifies_as_company_scope() -> None:
    candidate = _candidate(company_id=COMPANY_ID)
    assert classify_scope(candidate, TARGET) is MemoryScope.COMPANY


def test_company_subject_not_matching_target_is_excluded() -> None:
    candidate = _candidate(company_id=OTHER_COMPANY_ID)
    assert classify_scope(candidate, TARGET) is None


def test_no_subject_at_all_classifies_as_workspace_scope() -> None:
    candidate = _candidate()
    assert classify_scope(candidate, TARGET) is MemoryScope.WORKSPACE


def test_a_subject_the_target_never_resolved_is_excluded_even_if_candidate_has_one() -> None:
    bare_target = MemoryContextTarget(conversation_id=CONVERSATION_ID)
    candidate = _candidate(contact_id=CONTACT_ID)
    assert classify_scope(candidate, bare_target) is None


# ---------------------------------------------------------------------------
# B. Priority order: opportunity > contact > company > workspace
# ---------------------------------------------------------------------------


def test_priority_order_is_opportunity_then_contact_then_company_then_workspace() -> None:
    opportunity = _candidate(opportunity_id=OPPORTUNITY_ID, statement="opportunity")
    contact = _candidate(contact_id=CONTACT_ID, statement="contact")
    company = _candidate(company_id=COMPANY_ID, statement="company")
    workspace = _candidate(statement="workspace")

    # Shuffled input order must not affect the result.
    selected = _select([workspace, company, opportunity, contact])

    assert [c.statement for c in selected] == ["opportunity", "contact", "company", "workspace"]


def test_an_unrelated_subject_never_appears_regardless_of_order() -> None:
    relevant = _candidate(contact_id=CONTACT_ID, statement="relevant")
    unrelated = _candidate(contact_id=OTHER_CONTACT_ID, statement="unrelated")

    selected = _select([unrelated, relevant])

    assert [c.statement for c in selected] == ["relevant"]


# ---------------------------------------------------------------------------
# C. Exclusion: inactive status, self-reference
# ---------------------------------------------------------------------------


def test_superseded_memory_is_never_included() -> None:
    candidate = _candidate(status=MemoryStatus.SUPERSEDED)
    assert _select([candidate]) == []


def test_discarded_memory_is_never_included() -> None:
    candidate = _candidate(status=MemoryStatus.DISCARDED)
    assert _select([candidate]) == []


def test_active_memory_is_included_when_otherwise_eligible() -> None:
    candidate = _candidate(status=MemoryStatus.ACTIVE)
    assert _select([candidate]) == [candidate]


def test_memory_sourced_from_the_conversation_being_analyzed_is_excluded() -> None:
    candidate = _candidate(source_conversation_id=CONVERSATION_ID)
    assert _select([candidate]) == []


def test_memory_sourced_from_a_different_conversation_is_not_excluded_by_self_reference() -> None:
    candidate = _candidate(source_conversation_id=uuid.uuid4())
    assert _select([candidate]) == [candidate]


def test_manual_memory_with_no_source_conversation_is_not_excluded() -> None:
    candidate = _candidate(source_conversation_id=None)
    assert _select([candidate]) == [candidate]


# ---------------------------------------------------------------------------
# D. Secondary ordering: origin, then recency, within the same scope
# ---------------------------------------------------------------------------


def test_observed_origin_is_prioritized_over_inferred_within_the_same_scope() -> None:
    inferred = _candidate(origin=MemoryOrigin.INFERRED, confidence=0.6, statement="inferred")
    observed = _candidate(origin=MemoryOrigin.OBSERVED, statement="observed")

    selected = _select([inferred, observed])

    assert [c.statement for c in selected] == ["observed", "inferred"]


def test_more_recently_observed_memory_is_prioritized_within_the_same_origin() -> None:
    older = _candidate(observed_at=NOW - timedelta(days=10), statement="older")
    newer = _candidate(observed_at=NOW, statement="newer")

    selected = _select([older, newer])

    assert [c.statement for c in selected] == ["newer", "older"]


def test_scope_dominates_origin_and_recency() -> None:
    """A workspace-level memory that is observed and very recent must still
    rank behind an opportunity-scoped memory that is inferred and old — scope
    is the most significant key."""
    opportunity_old_inferred = _candidate(
        opportunity_id=OPPORTUNITY_ID,
        origin=MemoryOrigin.INFERRED,
        confidence=0.3,
        observed_at=NOW - timedelta(days=365),
        statement="opportunity",
    )
    workspace_new_observed = _candidate(
        origin=MemoryOrigin.OBSERVED,
        observed_at=NOW,
        statement="workspace",
    )

    selected = _select([workspace_new_observed, opportunity_old_inferred])

    assert [c.statement for c in selected] == ["opportunity", "workspace"]


# ---------------------------------------------------------------------------
# E. Stable tiebreak by id when scope, origin and date all tie
# ---------------------------------------------------------------------------


def test_identical_scope_origin_and_date_breaks_ties_by_ascending_id() -> None:
    low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    high_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    first = _candidate(id=low_id, statement="low")
    second = _candidate(id=high_id, statement="high")

    selected = _select([second, first])

    assert [c.statement for c in selected] == ["low", "high"]


def test_tiebreak_order_is_deterministic_regardless_of_input_order() -> None:
    low_id = uuid.UUID("00000000-0000-0000-0000-000000000001")
    high_id = uuid.UUID("00000000-0000-0000-0000-000000000002")
    first = _candidate(id=low_id, statement="low")
    second = _candidate(id=high_id, statement="high")

    forward = _select([first, second])
    backward = _select([second, first])

    assert forward == backward


# ---------------------------------------------------------------------------
# F. max_items
# ---------------------------------------------------------------------------


def test_max_items_caps_the_count_even_when_more_are_eligible() -> None:
    candidates = [
        _candidate(statement=f"m{i}", observed_at=NOW - timedelta(days=i)) for i in range(5)
    ]

    selected = _select(candidates, max_items=2)

    assert len(selected) == 2
    assert [c.statement for c in selected] == ["m0", "m1"]  # most recent first


def test_max_items_zero_selects_nothing() -> None:
    candidate = _candidate()
    assert _select([candidate], max_items=0) == []


def test_fewer_eligible_candidates_than_max_items_returns_all_of_them() -> None:
    candidate = _candidate()
    assert _select([candidate], max_items=50) == [candidate]


def test_negative_max_items_raises_value_error() -> None:
    with pytest.raises(ValueError, match="max_items"):
        select_context_memories([], target=TARGET, max_items=-1, max_chars=100)


# ---------------------------------------------------------------------------
# G. max_chars: exact boundaries, oversized-single-memory, best-fit packing
# ---------------------------------------------------------------------------


def test_a_statement_exactly_at_the_char_budget_is_included() -> None:
    candidate = _candidate(statement="x" * 10)
    assert _select([candidate], max_chars=10) == [candidate]


def test_a_statement_one_character_over_the_budget_is_excluded() -> None:
    candidate = _candidate(statement="x" * 11)
    assert _select([candidate], max_chars=10) == []


def test_a_single_oversized_memory_is_dropped_whole_never_truncated() -> None:
    """A memory longer than the entire budget can never be included — not even
    a truncated prefix of it."""
    oversized = _candidate(statement="x" * 1000, observed_at=NOW)
    selected = _select([oversized], max_chars=10)

    assert selected == []


def test_skipping_an_oversized_candidate_does_not_stop_the_scan() -> None:
    """A higher-priority candidate that does not fit must not block a later,
    shorter, lower-priority candidate from still being considered."""
    too_long = _candidate(
        contact_id=CONTACT_ID, statement="x" * 100, observed_at=NOW, confidence=None
    )
    fits = _candidate(statement="short", observed_at=NOW - timedelta(days=1))

    selected = _select([too_long, fits], max_chars=10)

    assert selected == [fits]


def test_running_budget_is_the_sum_of_included_statement_lengths_only() -> None:
    first = _candidate(statement="12345", observed_at=NOW)
    second = _candidate(statement="67890", observed_at=NOW - timedelta(days=1))
    third = _candidate(statement="x", observed_at=NOW - timedelta(days=2))

    # Budget fits exactly the first two (5 + 5 = 10); the third never fits.
    selected = _select([first, second, third], max_chars=10)

    assert selected == [first, second]


def test_max_chars_zero_selects_nothing() -> None:
    candidate = _candidate(statement="a")
    assert _select([candidate], max_chars=0) == []


def test_negative_max_chars_raises_value_error() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        select_context_memories([], target=TARGET, max_items=10, max_chars=-1)


# ---------------------------------------------------------------------------
# H. Confidence and origin are preserved, never reinterpreted
# ---------------------------------------------------------------------------


def test_confidence_and_origin_are_returned_unchanged_on_an_inferred_memory() -> None:
    candidate = _candidate(origin=MemoryOrigin.INFERRED, confidence=0.42, statement="guess")

    (selected,) = _select([candidate])

    assert selected is candidate  # the exact same object, never reconstructed
    assert selected.origin is MemoryOrigin.INFERRED
    assert selected.confidence == 0.42


def test_confidence_is_none_on_an_observed_memory_and_stays_none() -> None:
    candidate = _candidate(origin=MemoryOrigin.OBSERVED, confidence=None)

    (selected,) = _select([candidate])

    assert selected.confidence is None


def test_kind_is_preserved_unchanged() -> None:
    candidate = _candidate(kind=MemoryKind.PREFERENCE)

    (selected,) = _select([candidate])

    assert selected.kind is MemoryKind.PREFERENCE


# ---------------------------------------------------------------------------
# I. Contradictory memories are preserved, never resolved
# ---------------------------------------------------------------------------


def test_two_contradictory_active_memories_for_the_same_subject_are_both_kept() -> None:
    """Selection never arbitrates between two active, contradictory beliefs —
    that is a lifecycle decision (supersede/discard) made elsewhere."""
    says_ten = _candidate(
        opportunity_id=OPPORTUNITY_ID, statement="The deal is worth $10,000.", observed_at=NOW
    )
    says_twenty = _candidate(
        opportunity_id=OPPORTUNITY_ID,
        statement="The deal is worth $20,000.",
        observed_at=NOW - timedelta(days=1),
    )

    selected = _select([says_ten, says_twenty])

    assert set(selected) == {says_ten, says_twenty}


# ---------------------------------------------------------------------------
# J. Empty input and other edge cases
# ---------------------------------------------------------------------------


def test_no_candidates_returns_an_empty_sequence() -> None:
    assert _select([]) == []


def test_candidates_all_ineligible_returns_an_empty_sequence() -> None:
    candidates = [
        _candidate(status=MemoryStatus.DISCARDED),
        _candidate(contact_id=OTHER_CONTACT_ID),
        _candidate(source_conversation_id=CONVERSATION_ID),
    ]
    assert _select(candidates) == []


def test_a_target_with_no_resolved_subject_still_admits_workspace_memories() -> None:
    bare_target = MemoryContextTarget(conversation_id=CONVERSATION_ID)
    candidate = _candidate()

    assert _select([candidate], target=bare_target) == [candidate]


def test_the_result_is_a_tuple_not_a_mutable_list() -> None:
    candidate = _candidate()
    result = select_context_memories([candidate], target=TARGET, max_items=50, max_chars=10_000)

    assert isinstance(result, tuple)


# ---------------------------------------------------------------------------
# K. No infrastructure imports — pure module, verified by static analysis
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORT_PREFIXES = (
    "sqlalchemy",
    "nerva.models",
    "nerva.db",
    "nerva.repositories",
    "nerva.nerva_ai",
    "nerva.nerva_connect",
    "nerva.services",
    "openai",
    "httpx",
    "google",
    "googleapiclient",
)


def _imported_module_names(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_memory_context_module_imports_no_database_orm_or_ai_infrastructure() -> None:
    source_path = Path(memory_context_module.__file__)
    imported = _imported_module_names(source_path)

    for forbidden in FORBIDDEN_IMPORT_PREFIXES:
        offenders = {
            name for name in imported if name == forbidden or name.startswith(forbidden + ".")
        }
        assert not offenders, f"{source_path.name} imports {offenders}"


def test_memory_context_module_only_imports_stdlib_and_plain_enums() -> None:
    """A tighter, allowlist-based check than the forbidden-prefix scan above:
    every import in this module must be stdlib, or `nerva.core.enums` (a
    plain-`StrEnum` module with no SQLAlchemy/ORM — the same one
    `nerva_core.rules` already imports from)."""
    source_path = Path(memory_context_module.__file__)
    imported = _imported_module_names(source_path)

    allowed_exact = {
        "uuid",
        "collections.abc",
        "dataclasses",
        "datetime",
        "enum",
        "nerva.core.enums",
    }
    for name in imported:
        assert name in allowed_exact, f"unexpected import: {name}"
