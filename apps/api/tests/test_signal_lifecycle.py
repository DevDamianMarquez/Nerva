"""The business-signal state machine, tested exhaustively and without a
database — mirrors `test_action_lifecycle.py` for the same reasons."""

import itertools

import pytest

from nerva.core.enums import BusinessSignalStatus
from nerva.core.errors import InvalidStateTransitionError
from nerva.nerva_core.signal_lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    can_transition,
    ensure_transition,
)

S = BusinessSignalStatus

LEGAL = {
    (S.PENDING, S.CONFIRMED),
    (S.PENDING, S.DISMISSED),
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


def test_confirmed_and_dismissed_are_both_terminal() -> None:
    assert {S.CONFIRMED, S.DISMISSED} == TERMINAL_STATUSES
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_every_status_is_covered_by_the_transition_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_a_repeated_confirm_on_an_already_confirmed_signal_is_rejected() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.CONFIRMED, S.CONFIRMED)


def test_a_repeated_dismiss_on_an_already_dismissed_signal_is_rejected() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.DISMISSED, S.DISMISSED)


def test_a_confirmed_signal_cannot_be_dismissed_and_vice_versa() -> None:
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.CONFIRMED, S.DISMISSED)
    with pytest.raises(InvalidStateTransitionError):
        ensure_transition(S.DISMISSED, S.CONFIRMED)
