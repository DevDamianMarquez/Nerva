"""The business-signal lifecycle as an explicit state machine, mirroring
`nerva_actions.lifecycle` for the same reasons: pure code (no database), so the
rules can be tested exhaustively.

    pending ─┬─▶ confirmed   (terminal — a human created the Opportunity)
             └─▶ dismissed   (terminal — a human declined it)

Both `confirmed` and `dismissed` are terminal: a confirmed signal is never
later dismissed, and a dismissed signal is never later confirmed directly. A
repeated confirm/dismiss request on an already-resolved signal is therefore
rejected the same way a repeated `Action` approval already is — a `409
invalid_state_transition`, not a silent no-op — so a client always learns its
request had no additional effect rather than assuming it changed something.
Re-analysis producing a *fresh* pending signal after a dismissal is a
materialization decision, not a lifecycle transition — see
`nerva_core.rules.decide_materialization`.
"""

from collections.abc import Mapping

from nerva.core.enums import BusinessSignalStatus
from nerva.core.errors import InvalidStateTransitionError

ALLOWED_TRANSITIONS: Mapping[BusinessSignalStatus, frozenset[BusinessSignalStatus]] = {
    BusinessSignalStatus.PENDING: frozenset(
        {BusinessSignalStatus.CONFIRMED, BusinessSignalStatus.DISMISSED}
    ),
    BusinessSignalStatus.CONFIRMED: frozenset(),
    BusinessSignalStatus.DISMISSED: frozenset(),
}

TERMINAL_STATUSES: frozenset[BusinessSignalStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def can_transition(current: BusinessSignalStatus, target: BusinessSignalStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: BusinessSignalStatus, target: BusinessSignalStatus) -> None:
    """Raises `InvalidStateTransitionError` (HTTP 409) unless `current -> target`
    is legal."""
    if not can_transition(current, target):
        raise InvalidStateTransitionError(
            f"A signal that is {current.value} cannot become {target.value}.",
            details={"current_status": current.value, "requested_status": target.value},
        )
