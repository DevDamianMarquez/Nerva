"""Deterministic selection of memory candidates for a future analysis context
(M6.4, Block 1 — selection only).

Pure — no database, no ORM, no AI client, no I/O — mirroring the same
discipline as `nerva_core.rules` and `nerva_core.memory_rules`: every rule here
is exhaustively unit-testable, and this is the *only* code allowed to decide
which already-active memories are relevant enough, and in what order, to be
considered for a future prompt. It never reads a `Memory` row, never calls
`MemoryRepository`, and never renders a prompt — a future caller (Block 2, not
built here) is responsible for building `MemoryCandidate`/`MemoryContextTarget`
from real `Memory`/`Conversation`/`Contact`/`Opportunity` rows, and a future
caller (Block 3, not built here either) is responsible for turning the result
into actual prompt text, extending `nerva_ai.client.AnalysisInput` and
`nerva_ai.prompts.build_user_prompt`. Neither of those exists yet; nothing here
depends on them.

This module never resolves contradictions between two active memories (two
contradictory `ACTIVE` memories about the same subject are both passed through
untouched if both fit) and never changes `origin`/`confidence`/`kind`: an
`inferred` memory is still `inferred`, with whatever `confidence` it carried
in, in the object this module returns. See `nerva.nerva_memory.lifecycle` for
why resolving a contradiction is a lifecycle decision (`supersede`/`discard`)
made elsewhere, never an inference this module performs by omission.

Eligibility (a candidate not meeting every rule below is excluded outright,
never merely deprioritized):

| Condition                                              | Result                         |
|----------------------------------------------------------|-------------------------------|
| `status != ACTIVE`                                      | excluded                       |
| `source_conversation_id == target.conversation_id`      | excluded (self-reference)      |
| `opportunity_id` set, equals `target.opportunity_id`     | eligible, scope `OPPORTUNITY`  |
| `opportunity_id` set, does not equal (or target has none)| excluded (unrelated subject)   |
| `contact_id` set, equals `target.contact_id`             | eligible, scope `CONTACT`      |
| `contact_id` set, does not equal (or target has none)    | excluded (unrelated subject)   |
| `company_id` set, equals `target.company_id`             | eligible, scope `COMPANY`      |
| `company_id` set, does not equal (or target has none)    | excluded (unrelated subject)   |
| no subject set at all (workspace-level memory)           | eligible, scope `WORKSPACE`    |

`Memory` already guarantees at most one of `contact_id`/`company_id`/
`opportunity_id` is set per row (`at_most_one_subject`, `nerva.models.memory`),
so `classify_scope` never has to arbitrate between two subjects on one
candidate. Only an *exact* match to `target`'s own resolved subject chain is
relevant — a memory about a different contact/company/opportunity than the one
in play is not merely lower priority, it is excluded, so it can never crowd out
or dilute what actually applies to this conversation.

Ordering, once a candidate is eligible, from most to least significant:

1. `scope`: `OPPORTUNITY` < `CONTACT` < `COMPANY` < `WORKSPACE` — the more
   specific the subject, the more relevant it is assumed to be.
2. `origin`: `OBSERVED` < `INFERRED` — a directly stated fact is surfaced
   ahead of a guess when the budget is tight; this never changes what either
   *is*, only which is more likely to be dropped first.
3. `observed_at`, descending — the most recently-true belief first.
4. `id`, ascending (as a string) — the final, deterministic tiebreak.

Implemented as four chained *stable* sorts, least significant key first (the
standard trick for multi-key stable sorting): each pass only reorders ties
left by the previous one, so the final pass (`scope`) dominates while `id`
remains the ultimate tiebreak. Deliberately not `datetime.timestamp()` for the
date comparison — comparing `datetime` objects directly sorts correctly
regardless of timezone-awareness and avoids `OverflowError`/`OSError` that
`.timestamp()` can raise on Windows for dates before 1970.

Truncation (`max_items`, `max_chars`) is applied *after* the full ordering
above, walking the already-prioritized list once:

- `max_items` is a hard count ceiling: once reached, iteration stops entirely
  (every remaining candidate is equal- or lower-priority, so nothing further
  could improve the selection).
- `max_chars` bounds the *sum of `len(candidate.statement)`* for every
  included candidate — nothing else (no `kind`/`origin`/provenance label is
  counted here; that textual overhead belongs to a future prompt renderer,
  Block 3, which does not exist yet). A candidate whose own `statement` is
  longer than `max_chars` can never fit and is skipped outright — a memory is
  never truncated mid-sentence, it is included whole or not at all. Skipping a
  candidate that does not fit in the *remaining* budget does not stop the
  scan: a later, shorter, lower-priority candidate may still fit and is still
  considered.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import Enum, auto

from nerva.core.enums import MemoryKind, MemoryOrigin, MemoryStatus


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    """The minimal, already-resolved slice of an active `Memory` row this
    module needs — never the ORM row itself (a future caller, Block 2, is
    responsible for building this from `MemoryRepository` results).

    `status` is still carried here, not assumed already filtered by the
    caller, so this module enforces "only ACTIVE is eligible" itself rather
    than trusting every future caller to remember to filter — the same
    defense-in-depth discipline `docs/ARCHITECTURE.md` §4 already applies to
    workspace isolation (several independent layers, so no single omission is
    enough to leak something that should not be visible).

    `kind` plays no role in selection or ordering in this block (no
    requirement orders by it) — it is carried through unchanged purely so a
    future prompt renderer (Block 3) has it without this module needing to be
    revisited.

    Only `source_conversation_id` is checked for self-reference (per this
    block's scope): a memory sourced from a completed *analysis* of the same
    conversation has no `source_conversation_id` of its own (`Memory`'s
    `source_conversation_matches_type`/`source_analysis_matches_type` CHECK
    constraints make `source_conversation_id` and `source_analysis_id`
    mutually exclusive) and is not excluded by this check. Filtering those out
    too, if ever wanted, is a Block 2 concern (resolving which analysis
    belongs to which conversation before a candidate is ever built), not
    something this pure function can decide from a `Memory` row alone.
    """

    id: uuid.UUID
    status: MemoryStatus
    kind: MemoryKind
    origin: MemoryOrigin
    confidence: float | None
    statement: str
    observed_at: datetime
    contact_id: uuid.UUID | None
    company_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    source_conversation_id: uuid.UUID | None


@dataclass(frozen=True, slots=True)
class MemoryContextTarget:
    """The already-resolved subject chain of the conversation being analyzed —
    built by a future caller (Block 2) from `Conversation`/`Contact`/
    `Opportunity` rows, never from this module. `conversation_id` is always
    the conversation currently being analyzed, and is what self-reference
    exclusion is checked against; the other three fields may legitimately all
    be `None` (e.g. no contact resolved yet for this conversation)."""

    conversation_id: uuid.UUID
    contact_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    opportunity_id: uuid.UUID | None = None


class MemoryScope(Enum):
    """How specifically a candidate's subject matches `MemoryContextTarget`'s
    own resolved subject chain. Not a database-backed enum — like
    `nerva_core.rules.MaterializationDecision`, this never leaves this pure
    decision, so it does not belong in `core.enums`."""

    OPPORTUNITY = auto()
    CONTACT = auto()
    COMPANY = auto()
    WORKSPACE = auto()


_SCOPE_PRIORITY: dict[MemoryScope, int] = {
    MemoryScope.OPPORTUNITY: 0,
    MemoryScope.CONTACT: 1,
    MemoryScope.COMPANY: 2,
    MemoryScope.WORKSPACE: 3,
}

_ORIGIN_PRIORITY: dict[MemoryOrigin, int] = {
    MemoryOrigin.OBSERVED: 0,
    MemoryOrigin.INFERRED: 1,
}


def classify_scope(candidate: MemoryCandidate, target: MemoryContextTarget) -> MemoryScope | None:
    """The exact-match scope `candidate` is relevant under for `target`, or
    `None` if it belongs to a different, unrelated subject and must be
    excluded entirely — see the module docstring's eligibility table. `Memory`
    already guarantees at most one of `contact_id`/`company_id`/
    `opportunity_id` is set per row (`at_most_one_subject`,
    `nerva.models.memory`), so this never has to arbitrate between two
    subjects on the same candidate."""
    if candidate.opportunity_id is not None:
        if target.opportunity_id is not None and candidate.opportunity_id == target.opportunity_id:
            return MemoryScope.OPPORTUNITY
        return None
    if candidate.contact_id is not None:
        if target.contact_id is not None and candidate.contact_id == target.contact_id:
            return MemoryScope.CONTACT
        return None
    if candidate.company_id is not None:
        if target.company_id is not None and candidate.company_id == target.company_id:
            return MemoryScope.COMPANY
        return None
    return MemoryScope.WORKSPACE


def select_context_memories(
    candidates: Sequence[MemoryCandidate],
    *,
    target: MemoryContextTarget,
    max_items: int,
    max_chars: int,
) -> Sequence[MemoryCandidate]:
    """Returns the subsequence of `candidates` that is eligible for `target`
    (see the module docstring's eligibility table), ordered most-to-least
    relevant (scope, then origin, then recency, then `id`), and truncated to
    `max_items`/`max_chars` without ever exceeding either — see the module
    docstring's truncation rules for exactly how length is counted and what
    happens to an individual oversized memory.

    Never mutates or reconstructs a candidate: every returned object is one of
    the exact instances passed in, so `origin`/`confidence`/`kind` are
    preserved, never reinterpreted — an `inferred` memory is still `inferred`
    in the output, with whatever `confidence` it carried in. Two contradictory
    `ACTIVE` memories about the same subject are never reconciled here: both
    are selected (subject to the same ordering/truncation as anything else)
    if both are eligible and fit; resolving a contradiction is a lifecycle
    decision (`supersede`/`discard`, `nerva.nerva_memory.lifecycle`) made
    elsewhere, never something this function infers by picking a "winner"."""
    if max_items < 0:
        raise ValueError("max_items must be >= 0.")
    if max_chars < 0:
        raise ValueError("max_chars must be >= 0.")

    eligible: list[tuple[MemoryCandidate, MemoryScope]] = []
    for candidate in candidates:
        if candidate.status is not MemoryStatus.ACTIVE:
            continue
        if (
            candidate.source_conversation_id is not None
            and candidate.source_conversation_id == target.conversation_id
        ):
            continue
        scope = classify_scope(candidate, target)
        if scope is None:
            continue
        eligible.append((candidate, scope))

    # Stable, least-significant-key-first: each pass only reorders ties left
    # by the one before it, so `id` (first pass) is the ultimate tiebreak and
    # `scope` (last pass) dominates — see the module docstring.
    eligible.sort(key=lambda pair: str(pair[0].id))
    eligible.sort(key=lambda pair: pair[0].observed_at, reverse=True)
    eligible.sort(key=lambda pair: _ORIGIN_PRIORITY[pair[0].origin])
    eligible.sort(key=lambda pair: _SCOPE_PRIORITY[pair[1]])

    selected: list[MemoryCandidate] = []
    running_chars = 0
    for candidate, _scope in eligible:
        if len(selected) >= max_items:
            break
        length = len(candidate.statement)
        if running_chars + length > max_chars:
            continue
        selected.append(candidate)
        running_chars += length

    return tuple(selected)
