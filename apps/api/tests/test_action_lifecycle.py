"""The action state machine, tested exhaustively and without a database."""

import itertools

import pytest

from nerva.core.enums import ActionStatus
from nerva.core.errors import InvalidStateTransitionError
from nerva.nerva_actions.lifecycle import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATUSES,
    can_transition,
    ensure_transition,
)

S = ActionStatus

LEGAL = {
    (S.PROPOSED, S.APPROVED),
    (S.PROPOSED, S.REJECTED),
    (S.APPROVED, S.EXECUTING),
    (S.EXECUTING, S.COMPLETED),
    (S.EXECUTING, S.FAILED),
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


def test_terminal_statuses_have_no_way_out() -> None:
    assert {S.COMPLETED, S.FAILED, S.REJECTED} == TERMINAL_STATUSES
    for status in TERMINAL_STATUSES:
        assert ALLOWED_TRANSITIONS[status] == frozenset()


def test_every_status_is_covered_by_the_transition_table() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(S)


def test_an_action_cannot_skip_approval() -> None:
    assert not can_transition(S.PROPOSED, S.EXECUTING)
    assert not can_transition(S.PROPOSED, S.COMPLETED)
