"""Deterministic rules for forming the `estimated_value` fact memory from a
completed `ConversationAnalysis`.

Pure — no database, no ORM, no LLM/provider imports, no HTTP context — so every
rule here is exhaustively unit-testable, mirroring `nerva_core.rules`'s
discipline (`decide_materialization` for business signals; `decide_memory_formation`
here for memory). This is the *only* code allowed to decide whether a new
`estimated_value` memory should be created, an existing one superseded, or
nothing done at all. It never touches `Memory.content`'s JSON shape or calls
`MemoryService` itself — a caller (the future M6.3 engine) is responsible for
translating a `MemoryFormationDecision` into the actual `remember`/`supersede`
calls, the same separation `nerva.services.signals.SignalService` already keeps
from `decide_materialization`.

Why this scope is limited to `estimated_value` alone: `Memory` has no field
that says *which* fact a `kind=FACT` row represents (see `nerva.models.memory`
and `nerva.nerva_memory.content`) — only `source_analysis_id` ties it back to
the analysis that produced it. M6.3 automates exactly one fact, so "the active
FACT memory sourced from this analysis" and "the active estimated_value memory
for this analysis" are the same set of rows by construction. A second
automated fact would need a discriminator (e.g. a `fact` key in
`MemoryContent.attributes`) before this module — or whatever reads
`MemoryRepository.get_active_for_analysis`'s result — could keep telling them
apart; that is explicitly out of scope here.

Decision table (`decide_memory_formation`):

| existing | same fp? | valid estimate? | same value? | decision  |
|----------|----------|------------------|--------------|-----------|
| none     | n/a      | no               | n/a          | NOOP      |
| none     | n/a      | yes              | n/a          | CREATE    |
| some     | yes      | no/yes           | n/a          | NOOP      |
| some     | no       | no               | n/a          | NOOP      |
| some     | no       | yes              | yes          | NOOP      |
| some     | no       | yes              | no           | SUPERSEDE |

The "same fingerprint" row is what makes memory formation idempotent against
retries and repeated analyses of unchanged content (`source_fingerprint` is
copied from `ConversationAnalysis.input_fingerprint` at write time — see
`nerva.models.memory`'s module docstring on why that, not
`source_analysis_id`, is the specific-analysis-run identity). The "no valid
estimate now" rows are what guarantee a later analysis that stopped mentioning
a number never auto-discards a memory a human may already be relying on —
see the M6.3 invariant of the same name.
"""

from dataclasses import dataclass
from enum import Enum, auto


@dataclass(frozen=True, slots=True)
class EstimatedValueObservation:
    """The minimal slice of a completed `ConversationAnalysis` this rule
    needs — not the ORM row itself, the same "minimal slice" discipline as
    `nerva_core.rules.AnalysisSummary`. `value_is_explicit` is carried through
    for a caller to decide `MemoryOrigin`/`confidence` when acting on a
    `CREATE`/`SUPERSEDE` decision; this rule's own branching never depends on
    it — sentiment-style, see `nerva_core.rules`'s equivalent note."""

    estimated_value_cents: int | None
    value_currency: str | None
    value_is_explicit: bool | None

    @property
    def has_valid_estimate(self) -> bool:
        """A value with no currency (or vice versa) is not a usable estimate —
        both or neither, never half of one propagated into a memory."""
        return self.estimated_value_cents is not None and self.value_currency is not None


@dataclass(frozen=True, slots=True)
class ExistingEstimatedValueMemory:
    """The minimal slice of the currently-active memory representing this fact
    for the same logical source, if one exists — built by the caller from
    `MemoryRepository.get_active_for_analysis`'s result, never passed as an
    ORM row here."""

    estimated_value_cents: int
    value_currency: str
    source_fingerprint: str


class MemoryFormationDecision(Enum):
    """What the caller should do about the (observation, existing memory)
    pair. Not a database-backed enum — this never leaves this pure decision,
    so it does not belong in `core.enums` (mirrors
    `nerva_core.rules.MaterializationDecision`)."""

    NOOP = auto()
    CREATE = auto()
    SUPERSEDE = auto()


def decide_memory_formation(
    *,
    observation: EstimatedValueObservation,
    existing: ExistingEstimatedValueMemory | None,
    current_fingerprint: str,
) -> MemoryFormationDecision:
    """The re-analysis behavior for the `estimated_value` fact memory, as an
    explicit decision table — see the module docstring for the full table and
    the invariants each row protects."""
    if existing is None:
        return (
            MemoryFormationDecision.CREATE
            if observation.has_valid_estimate
            else MemoryFormationDecision.NOOP
        )

    # A retry/re-run of the exact same analysis content must never create or
    # supersede anything a second time — idempotency against retries.
    if existing.source_fingerprint == current_fingerprint:
        return MemoryFormationDecision.NOOP

    # New analysis content, but it no longer carries a usable estimate: never
    # auto-discard what a human may already be relying on.
    if not observation.has_valid_estimate:
        return MemoryFormationDecision.NOOP

    # New content, a usable estimate, but it says the same thing as what is
    # already active — nothing changed, so nothing should churn.
    if (
        observation.estimated_value_cents == existing.estimated_value_cents
        and observation.value_currency == existing.value_currency
    ):
        return MemoryFormationDecision.NOOP

    return MemoryFormationDecision.SUPERSEDE
