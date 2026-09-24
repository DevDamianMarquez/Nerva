"""The action lifecycle as an explicit state machine.

    proposed -> approved -> executing -> completed
        |                        |
        v                        v
     rejected                  failed

`completed`, `failed` and `rejected` are terminal. Retrying a failed action means
proposing a new one, so every attempt has its own approval and audit trail.

This module is pure (no database, no HTTP) so the rules can be tested exhaustively.
"""

from collections.abc import Mapping

from nerva.core.enums import ActionStatus
from nerva.core.errors import InvalidStateTransitionError

ALLOWED_TRANSITIONS: Mapping[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PROPOSED: frozenset({ActionStatus.APPROVED, ActionStatus.REJECTED}),
    ActionStatus.APPROVED: frozenset({ActionStatus.EXECUTING}),
    ActionStatus.EXECUTING: frozenset({ActionStatus.COMPLETED, ActionStatus.FAILED}),
    ActionStatus.COMPLETED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.REJECTED: frozenset(),
}

TERMINAL_STATUSES: frozenset[ActionStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def can_transition(current: ActionStatus, target: ActionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: ActionStatus, target: ActionStatus) -> None:
    """Raise `InvalidStateTransitionError` (HTTP 409) unless `current -> target` is legal."""
    if not can_transition(current, target):
        raise InvalidStateTransitionError(
            f"An action that is {current.value} cannot become {target.value}.",
            details={"current_status": current.value, "requested_status": target.value},
        )
