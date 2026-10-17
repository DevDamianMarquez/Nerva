"""Milestone 6, Block 1: the memory domain's pure contracts — the structured
content shape (`nerva_memory.content`) and the status lifecycle
(`nerva_memory.lifecycle`). No database, no agent, no AI provider: this block
only defines what a memory is allowed to look like and how its status may
change, mirroring `test_action_execution_contracts.py` (payloads/lifecycle) and
`test_signal_lifecycle.py` (the lifecycle half) for the same reasons.
"""

import itertools

import pytest
from pydantic import ValidationError

from nerva.core.enums import MemoryStatus
from nerva.core.errors import InvalidStateTransitionError
from nerva.nerva_memory.content import MEMORY_SCHEMA_VERSION, MemoryContent, parse_memory_content
from nerva.nerva_memory.errors import MemoryContentError
from nerva.nerva_memory.lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    can_transition,
    ensure_transition,
)

# ---------------------------------------------------------------------------
# A. MemoryContent: structured, validated content
# ---------------------------------------------------------------------------


def test_a_minimal_valid_content_is_accepted() -> None:
    content = parse_memory_content({"statement": "Acme's renewal is in March."})

    assert content.statement == "Acme's renewal is in March."
    assert content.attributes == {}


def test_attributes_may_carry_structured_json() -> None:
    content = parse_memory_content(
        {
            "statement": "Priya prefers async updates over calls.",
            "attributes": {"preferred_channel": "email", "confidence_hint": 0.8},
        }
    )

    assert content.attributes == {"preferred_channel": "email", "confidence_hint": 0.8}


def test_an_empty_statement_is_rejected() -> None:
    with pytest.raises(MemoryContentError):
        parse_memory_content({"statement": ""})


def test_a_missing_statement_is_rejected() -> None:
    with pytest.raises(MemoryContentError):
        parse_memory_content({"attributes": {}})


def test_an_oversized_statement_is_rejected() -> None:
    with pytest.raises(MemoryContentError):
        parse_memory_content({"statement": "x" * 1001})


def test_an_unknown_field_is_rejected() -> None:
    with pytest.raises(MemoryContentError):
        parse_memory_content({"statement": "Valid.", "raw_email_body": "leak"})


def test_parse_memory_content_never_raises_a_bare_pydantic_validation_error() -> None:
    with pytest.raises(MemoryContentError) as error:
        parse_memory_content({})

    assert error.value.__cause__ is not None  # the pydantic ValidationError, chained


def test_schema_version_is_a_stable_module_constant() -> None:
    assert isinstance(MEMORY_SCHEMA_VERSION, int)
    assert MEMORY_SCHEMA_VERSION >= 1


def test_memory_content_rejects_extra_fields_even_when_constructed_directly() -> None:
    with pytest.raises(ValidationError):
        MemoryContent.model_validate({"statement": "Valid.", "extra": "nope"})


# ---------------------------------------------------------------------------
# B. The memory lifecycle: active -> superseded | discarded
# ---------------------------------------------------------------------------

S = MemoryStatus

LEGAL = {
    (S.ACTIVE, S.SUPERSEDED),
    (S.ACTIVE, S.DISCARDED),
}


@pytest.mark.parametrize(("current", "target"), list(itertools.product(S, S)))
def test_only_the_documented_transitions_are_legal(current: S, target: S) -> None:
    assert can_transition(current, target) is ((current, target) in LEGAL)


@pytest.mark.parametrize(("current", "target"), sorted(LEGAL))
def test_legal_transitions_pass(current: S, target: S) -> None:
    ensure_transition(current, target)  # does not raise


@pytest.mark.parametrize(
    ("current", "target"),
    [pair for pair in itertools.product(S, S) if pair not in LEGAL],
)
def test_illegal_transitions_raise_a_conflict_with_details(current: S, target: S) -> None:
    with pytest.raises(InvalidStateTransitionError) as error:
        ensure_transition(current, target)

    assert error.value.status_code == 409
    assert error.value.details == {
        "current_status": current.value,
        "requested_status": target.value,
    }


def test_superseded_and_discarded_are_both_terminal() -> None:
    assert {S.SUPERSEDED, S.DISCARDED} == TERMINAL_STATUSES
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_every_status_is_covered_by_the_transition_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_a_repeated_supersede_on_an_already_superseded_memory_is_rejected() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.SUPERSEDED, S.SUPERSEDED)


def test_a_repeated_discard_on_an_already_discarded_memory_is_rejected() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.DISCARDED, S.DISCARDED)


def test_a_superseded_memory_cannot_be_discarded_and_vice_versa() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.SUPERSEDED, S.DISCARDED)
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.DISCARDED, S.SUPERSEDED)
