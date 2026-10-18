"""Forms the one fact memory M6.3 automates — `estimated_value` — from a
conversation's current completed analysis, via the deterministic rules in
`nerva_core.memory_rules`.

`MemoryFormationService.form_estimated_value_memory` is the only code path
that creates or supersedes an automated `estimated_value` memory. It mirrors
`SignalService.materialize_potential_opportunity`'s shape (safe to call
repeatedly; always re-reads the conversation's *current* analysis rather than
trusting a value a caller might hand it) with one addition, because a
`Memory` is not a `BusinessSignal`:

**Why a read-then-decide-then-insert is not enough.** A `BusinessSignal` is
one mutable row per `(conversation_id, type)` — `materialize_potential_
opportunity` can safely re-fetch, compare, and overwrite it in place, because
there is always at most one row to race over, and two concurrent calls
updating the *same* row serialize on Postgres's own row lock for free. A
`Memory` is append-only by design (see `nerva.models.memory`'s module
docstring: resolving one never rewrites `content`, it always creates a new
row) — so forming one is an *insert*, and two concurrent callers that both
read "no active memory yet" before either has written would, without an
explicit lock, both decide to insert, producing two active memories for the
same fact. Checking "is there already an active one" immediately before
`INSERT` does not close this window: both checks can run before either write
lands.

**The lock this module actually takes.** `ConversationAnalysis` is one
*mutable, overwritten-in-place* row per conversation, and it already exists
by the time this method can do anything useful (it only ever acts on a
COMPLETED analysis). `form_estimated_value_memory` takes a `SELECT ... FOR
UPDATE` on that row via `ConversationAnalysisRepository.get_for_conversation`
at the very start — the same lock, same row, same pattern
`AnalysisService.analyze` already uses to serialize concurrent analyses of
one conversation — and holds it for the method's entire
read-current-memory/decide/write sequence, releasing it only at the one
commit this method or `MemoryService` makes before returning. A second
concurrent call for the *same* conversation therefore blocks at that lock
acquisition until the first is fully done (committed or rolled back), then
re-reads the now-current state — making the whole sequence atomic with
respect to any other call for this conversation, with no new unique
constraint and no SERIALIZABLE isolation required.

**Why the lock also answers the "two different fingerprints race" question.**
This method never accepts a `ConversationAnalysis` as a parameter — only a
`conversation_id` — and always re-reads the row itself, under the lock,
at the moment it runs. A caller that finishes late can therefore never push a
stale result through this method: whatever is currently committed on that
row is what gets acted on, the same trust `SignalService` already places in
it. This is a claim about the *database* row by construction of the lock, and
— since `ConversationAnalysisRepository.get_for_conversation`'s `for_update`
path also forces `populate_existing()` — a claim about the in-memory `ORM`
object handed back here too: without it, a session that already held a
(non-expired — both the production and test session factories disable
`expire_on_commit`) copy of this exact row would get that stale copy back
from the identity map regardless of what the lock just waited on, silently
breaking this guarantee for a session reused across more than one call for
the same conversation (see that repository method's own docstring). (A
separate, narrower question — whether `AnalysisService.analyze`
itself could ever let an older analysis's commit land after, and overwrite,
a newer one, if a new message arrives in the exact window between two
concurrent analyses — is a property of that service's own locking, already
true of the codebase before this module, and out of scope here; seeking
confirmation on this.)

**Why a value change is still one commit, not two.** A changed estimate must
record the new fact and resolve the old one together — leaving a window with
either both active (two contradictory active memories) or, had the order
been reversed, neither active (the fact briefly has no active version) is
exactly what the invariants forbid. `MemoryService.replace` (M6.3 Block 2) is
that one atomic operation — this method used to call `remember(commit=False)`
then `supersede(commit=True)` by hand to get the same guarantee; see
`MemoryService`'s module docstring for why `replace` exists and what it
composes.

## Subject resolution (M6.4 Block 3 correction)

An `estimated_value` fact is about one specific deal, never the workspace at
large. `_resolve_subject` decides what that deal's subject is, in strict
priority order, and `decide_memory_formation` (`nerva_core.memory_rules`)
stays completely ignorant of all of this — it only ever compares values and
fingerprints, exactly as before this correction:

1. **`opportunity_id`** — if exactly one `Opportunity` in this workspace names
   this conversation (`OpportunityRepository.get_by_conversation`, M6.4 Block
   2), use it. `Opportunity.conversation_id` carries no unique constraint
   (`nerva.models.crm`), so *more than one* is a real, legitimate case, not a
   bug — and this module never picks one arbitrarily among them.
2. **`contact_id`** — only if step 1 did not resolve (no linked opportunity,
   or more than one), fall back to `conversation.contact_id` if it is set.
3. **No subject** — if neither resolves, `form_estimated_value_memory` does
   not write anything at all. A `Memory` with no subject means "this concerns
   the workspace as a whole" (`nerva.models.memory`'s own docstring) — that
   is never an honest description of one deal's value just because entity
   resolution failed for it, so this module never produces that row instead.
   No `company_id` fallback either: a company is broader than a contact, and
   this deal-specific fact is no truer of "the whole company" than it is of
   "the whole workspace" when neither a contact nor an opportunity is known.

**CREATE** resolves the subject fresh, at the moment of creation, since there
is nothing yet to compare it against. If it resolves to nothing, the CREATE
that `decide_memory_formation` would otherwise perform is downgraded to a
no-write NOOP by this module — `decide_memory_formation`'s own decision
(driven purely by value/fingerprint) is unchanged; only whether this module
*acts* on a `CREATE` it returned is conditional on a subject existing.

**SUPERSEDE pins the subject to whatever the memory being replaced already
has — it never re-resolves it.** `decide_memory_formation` only returns
`SUPERSEDE` when the *value* changed; nothing about resolving a `SUPERSEDE`
requires (or by itself implies) that the *subject* should change too, and a
memory that was successfully created always has exactly one of `contact_id`/
`opportunity_id` set (see above), so there is never a "no subject to pin to"
case here. This is a deliberate, narrow decision, not an oversight: if an
`Opportunity` gets linked to a conversation *after* a `contact`-scoped
`estimated_value` memory already exists for it, a later value change will
still supersede that memory under its original `contact_id` — never silently
"upgrade" it to the now-resolvable `opportunity_id`. Re-parenting an existing
fact thread to a newly-discovered subject is a real product decision (does
the old belief's history move with it? does any consumer of the old subject
need to know?) that the current model does not define, and this module does
not invent an answer to it. The practical effect is narrow: the one
conversation whose `Opportunity` link arrived after its first `estimated_
value` memory stays `contact`-scoped for that fact going forward, until this
is explicitly revisited.
"""

import uuid
from typing import Any

from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import AnalysisStatus, MemoryKind, MemoryOrigin
from nerva.core.errors import DataIntegrityError, NotFoundError
from nerva.models import Conversation, ConversationAnalysis, Memory
from nerva.nerva_core.memory_rules import (
    EstimatedValueObservation,
    ExistingEstimatedValueMemory,
    MemoryFormationDecision,
    decide_memory_formation,
)
from nerva.repositories.workspace_data import (
    ConversationAnalysisRepository,
    ConversationRepository,
    MemoryRepository,
    OpportunityRepository,
)
from nerva.services.base import ScopedService
from nerva.services.memory import MemoryService


def _observation_from_analysis(analysis: ConversationAnalysis) -> EstimatedValueObservation:
    return EstimatedValueObservation(
        estimated_value_cents=analysis.estimated_value_cents,
        value_currency=analysis.value_currency,
        value_is_explicit=analysis.value_is_explicit,
    )


def _existing_from_memory(memory: Memory) -> ExistingEstimatedValueMemory:
    """Reads back exactly what `_memory_content` below is expected to have
    written. This module controls both sides of that contract for its *own*
    writes, but `get_active_for_analysis` has no way to tell those apart from
    a `FACT` memory a human wrote directly (via `POST /memories`) for the same
    analysis with a different `attributes` shape — nothing elsewhere in the
    codebase currently prevents that (see M6.3 Block 2's review). So this is
    external input to validate, not an internal invariant to assert: a mismatch
    here must raise a clear, controlled error instead of an `AssertionError`
    that disappears under `python -O` and lets a malformed value flow on."""
    attributes = memory.content.get("attributes", {})
    cents = attributes.get("estimated_value_cents")
    currency = attributes.get("value_currency")
    if (
        not isinstance(cents, int)
        or not isinstance(currency, str)
        or memory.source_fingerprint is None
    ):
        raise DataIntegrityError(
            f"Memory {memory.id} is the active FACT memory for analysis "
            f"{memory.source_analysis_id}, but its content does not match the "
            "estimated_value shape this module expects (attributes."
            "estimated_value_cents: int, attributes.value_currency: str, "
            "source_fingerprint set) — likely a memory written directly for "
            "this analysis rather than through MemoryFormationService."
        )
    return ExistingEstimatedValueMemory(
        estimated_value_cents=cents,
        value_currency=currency,
        source_fingerprint=memory.source_fingerprint,
    )


def _memory_content(cents: int, currency: str) -> dict[str, Any]:
    return {
        "statement": f"Estimated deal value: {currency} {cents / 100:,.2f}.",
        "attributes": {"estimated_value_cents": cents, "value_currency": currency},
    }


class MemoryFormationService(ScopedService):
    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        super().__init__(session, ctx)
        self.conversations = ConversationRepository(session, ctx.workspace_id)
        self.analyses = ConversationAnalysisRepository(session, ctx.workspace_id)
        self.memories = MemoryRepository(session, ctx.workspace_id)
        self.opportunities = OpportunityRepository(session, ctx.workspace_id)
        self.memory_service = MemoryService(session, ctx)

    def _resolve_subject(
        self, conversation: Conversation
    ) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """`(contact_id, opportunity_id)`, with at most one set — see the
        module docstring's "Subject resolution" for the exact priority order
        and why there is no `company_id` fallback. Both `None` means no
        subject resolves at all; the caller must never write a `Memory` in
        that case (see the module docstring)."""
        opportunities = self.opportunities.get_by_conversation(conversation.id)
        if len(opportunities) == 1:
            return None, opportunities[0].id
        if conversation.contact_id is not None:
            return conversation.contact_id, None
        return None, None

    def form_estimated_value_memory(self, conversation_id: uuid.UUID) -> Memory | None:
        """Evaluates the conversation's current completed analysis against
        `nerva_core.memory_rules.decide_memory_formation` and creates,
        supersedes, or leaves alone the `estimated_value` fact memory
        accordingly. Returns the current active memory for this fact (which
        may be the one just created, the one left untouched, or `None` if
        there has never been a valid estimate *or* if one exists but no
        unambiguous `Opportunity`/`contact_id` subject resolves for this
        conversation — see the module docstring's "Subject resolution").
        Raises `NotFoundError` if `conversation_id` is not in this workspace.
        Safe to call repeatedly — see the module docstring for the
        concurrency and staleness guarantees this relies on."""
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.")

        # Row lock: see the module docstring. Held until this method's one
        # commit (CREATE or SUPERSEDE), or released on rollback for a NOOP —
        # mirrors SignalService.materialize_potential_opportunity's NOOP path,
        # which likewise commits nothing when there is nothing to change.
        analysis = self.analyses.get_for_conversation(conversation_id, for_update=True)
        if analysis is None or analysis.status != AnalysisStatus.COMPLETED:
            return None
        # Not DB-enforced (see `ConversationAnalysis`'s columns) — a `COMPLETED` row
        # missing either is a bug in whatever persisted it (`AnalysisService`), not
        # something this call caused or can recover from on its own.
        if analysis.analyzed_at is None or analysis.confidence is None:
            raise DataIntegrityError(
                f"ConversationAnalysis {analysis.id} is COMPLETED but missing "
                "analyzed_at and/or confidence, which every COMPLETED row is "
                "expected to carry."
            )
        analyzed_at = analysis.analyzed_at
        confidence = analysis.confidence

        active_rows = self.memories.get_active_for_analysis(analysis.id, kind=MemoryKind.FACT)
        if len(active_rows) > 1:
            # Never pick one arbitrarily and never touch the data — this needs a
            # human to investigate how a second active FACT was written for this
            # analysis (see nerva_core.memory_rules and M6.3 Block 2's review).
            raise DataIntegrityError(
                f"{len(active_rows)} active FACT memories found for analysis "
                f"{analysis.id}; at most one is expected (see nerva_core.memory_rules)."
            )
        existing_memory = active_rows[0] if active_rows else None
        existing = _existing_from_memory(existing_memory) if existing_memory is not None else None

        observation = _observation_from_analysis(analysis)
        decision = decide_memory_formation(
            observation=observation,
            existing=existing,
            current_fingerprint=analysis.input_fingerprint,
        )

        if decision == MemoryFormationDecision.NOOP:
            return existing_memory

        # CREATE/SUPERSEDE only ever follow a valid estimate — see the decision table
        # in nerva_core.memory_rules. A violation here would be a bug in
        # decide_memory_formation itself, not bad data; still checked explicitly
        # (not asserted) so it can never silently fall through under `python -O`.
        if observation.estimated_value_cents is None or observation.value_currency is None:
            raise DataIntegrityError(
                "decide_memory_formation returned CREATE/SUPERSEDE for an observation "
                "with no valid estimate — see nerva_core.memory_rules's decision table."
            )
        cents = observation.estimated_value_cents
        currency = observation.value_currency

        origin = MemoryOrigin.OBSERVED if observation.value_is_explicit else MemoryOrigin.INFERRED
        content = _memory_content(cents, currency)
        confidence_for_memory = None if observation.value_is_explicit else confidence

        if decision == MemoryFormationDecision.CREATE:
            contact_id, opportunity_id = self._resolve_subject(conversation)
            if contact_id is None and opportunity_id is None:
                # No unambiguous Opportunity and no known contact: never
                # promote this to a WORKSPACE-level memory — see the module
                # docstring's "Subject resolution". `decide_memory_formation`
                # said CREATE based on value/fingerprint alone; this module
                # declines to act on it. No existing memory for this fact
                # exists either (CREATE only happens when `existing_memory`
                # is `None`), so there is nothing to return but `None`.
                return None
            return self.memory_service.remember(
                kind=MemoryKind.FACT,
                origin=origin,
                content=content,
                observed_at=analyzed_at,
                confidence=confidence_for_memory,
                contact_id=contact_id,
                opportunity_id=opportunity_id,
                source_analysis_id=analysis.id,
                actor=Actor.nerva(),
                commit=True,
            )

        # SUPERSEDE only ever follows an existing active row (see the decision
        # table) — same reasoning as the check above.
        if existing_memory is None:
            raise DataIntegrityError(
                "decide_memory_formation returned SUPERSEDE with no existing active "
                "memory to supersede — see nerva_core.memory_rules's decision table."
            )
        # Subject is pinned to whatever `existing_memory` already has, never
        # re-resolved — see the module docstring's "Subject resolution" for
        # why re-parenting to a since-discovered Opportunity is an explicit
        # future decision, not made here. A successfully created memory
        # always has exactly one of these two set, so this is never `(None,
        # None)` here the way it can be for CREATE.
        #
        # `replace` is the single commit for this whole create-and-resolve
        # operation (M6.3 Block 2): the new memory, its MEMORY_RECORDED event,
        # this resolution, and MEMORY_SUPERSEDED all land in one transaction.
        # Its own lock on `existing_memory`'s row is additional to, never a
        # replacement for, the `ConversationAnalysis` row lock this method has
        # held since the top — both are the same connection's own locks, so
        # neither blocks the other, and the analysis lock keeps serializing
        # concurrent calls for this conversation exactly as before.
        return self.memory_service.replace(
            existing_memory.id,
            kind=MemoryKind.FACT,
            origin=origin,
            content=content,
            observed_at=analyzed_at,
            confidence=confidence_for_memory,
            contact_id=existing_memory.contact_id,
            opportunity_id=existing_memory.opportunity_id,
            source_analysis_id=analysis.id,
            actor=Actor.nerva(),
            commit=True,
        )
