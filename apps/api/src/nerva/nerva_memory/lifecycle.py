"""The memory lifecycle as an explicit state machine, mirroring
`nerva_core.signal_lifecycle` for the same reasons: pure code (no database), so
the rules can be tested exhaustively.

    active ─┬─▶ superseded   (terminal — a fresher memory replaced this one)
            └─▶ discarded    (terminal — found wrong, stale, or no longer useful)

Both `superseded` and `discarded` are terminal: neither reactivates a memory in
place. A fact later confirmed again after being discarded, or a fresher
observation replacing a superseded inference, is always a *new* `Memory` row
with its own provenance — never a mutation of an old one. This is what keeps
memory verifiable: a row's `content` never silently changes meaning after the
fact, and what Nerva believed at any point in time stays reconstructable from
the chain of `superseded_by_id` links plus the activity trail.
"""

from collections.abc import Mapping

from nerva.core.enums import MemoryStatus
from nerva.core.errors import InvalidStateTransitionError

ALLOWED_TRANSITIONS: Mapping[MemoryStatus, frozenset[MemoryStatus]] = {
    MemoryStatus.ACTIVE: frozenset({MemoryStatus.SUPERSEDED, MemoryStatus.DISCARDED}),
    MemoryStatus.SUPERSEDED: frozenset(),
    MemoryStatus.DISCARDED: frozenset(),
}

TERMINAL_STATUSES: frozenset[MemoryStatus] = frozenset(
    status for status, targets in ALLOWED_TRANSITIONS.items() if not targets
)


def can_transition(current: MemoryStatus, target: MemoryStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def ensure_transition(current: MemoryStatus, target: MemoryStatus) -> None:
    """Raise `InvalidStateTransitionError` (HTTP 409) unless `current -> target` is legal."""
    if not can_transition(current, target):
        raise InvalidStateTransitionError(
            f"A memory that is {current.value} cannot become {target.value}.",
            details={"current_status": current.value, "requested_status": target.value},
        )
