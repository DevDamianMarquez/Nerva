"""Recording and resolving workspace memory.

`remember` is the one place a `Memory` row is created. It validates the
structured content contract (`nerva_memory.content`) and, when a source or a
subject is given, that it belongs to this workspace — exactly the way
`ActionService.propose` validates `opportunity_id` via
`ScopedService._require_opportunity`. It does **not** decide what is worth
remembering: that decision belongs to whatever calls it (a human, or Nerva's
own reasoning layer in a later milestone) — this only enforces the contract
once a decision has already been made. No AI provider is called here.

`supersede` and `discard` are the only two ways a memory ever leaves `active`
(see `nerva_memory.lifecycle`); neither rewrites `content` in place, so what a
memory said at the time it was active is never retroactively changed. All
three methods take an optional `actor` (default: the authenticated caller,
`self.ctx.actor`) — an automated caller (e.g. M6.3's memory-formation engine,
reacting to a completed analysis with no human in the loop) passes
`Actor.nerva()` explicitly so the resulting `ActivityEvent` attributes the
change honestly, the same convention `ActionService.propose` already uses.

All three also take an optional `commit` (default `True`, preserving every
existing caller's behavior unchanged).

`replace` composes `remember` and `supersede` into the one atomic operation a
value change actually needs: creating the new fact and resolving the old one
as *two* separate commits would leave a real window (a crash between them)
where either both are active (violates "no two contradictory active memories
for the same fact") or, had the order been reversed, neither is.
`ScopedRepository.add` already flushes (not commits), so the replacement
`remember` creates with `commit=False` is fully visible — by id — to the
`supersede` call that follows in the same transaction; `replace` commits once,
itself, after both have run (M6.3 Block 2). This is exactly the composition
M6.3's memory-formation engine (`nerva.services.memory_formation`) used to do
by hand before `replace` existed, and the operation `POST /memories/{id}/
replace` exposes for any caller — `remember`/`supersede` individually still
back the two-step `POST /memories` + `POST /memories/{id}/supersede` flow for
a caller that genuinely wants two separate decisions (e.g. drafting a
replacement before committing to resolving the original).

`list` is the one place retrieval policy lives: by default it returns only
`active` memories — this is meant to feed future context/agent features, and a
superseded or discarded belief must never leak into that silently. An explicit
`status` (including `MemoryStatus.ACTIVE` itself) is always honored, so a
historical state stays fully reachable to a caller that asks for it on
purpose; it is never hidden, only never the default.
"""

import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import (
    ActivityEventType,
    MemoryKind,
    MemoryOrigin,
    MemorySourceType,
    MemoryStatus,
)
from nerva.core.errors import InvalidReferenceError, InvalidStateTransitionError, NotFoundError
from nerva.db.base import utcnow
from nerva.models import Memory
from nerva.nerva_memory.content import MEMORY_SCHEMA_VERSION, parse_memory_content
from nerva.nerva_memory.lifecycle import ensure_transition
from nerva.repositories.workspace_data import ConversationAnalysisRepository, MemoryRepository
from nerva.services.base import ScopedService


class MemoryService(ScopedService):
    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        super().__init__(session, ctx)
        self.memories = MemoryRepository(session, ctx.workspace_id)
        self.analyses = ConversationAnalysisRepository(session, ctx.workspace_id)

    def get(self, memory_id: uuid.UUID) -> Memory:
        memory = self.memories.get(memory_id)
        if memory is None:
            raise NotFoundError("Memory not found.")
        return memory

    def list(
        self,
        *,
        kind: MemoryKind | None = None,
        origin: MemoryOrigin | None = None,
        status: MemoryStatus | None = None,
        contact_id: uuid.UUID | None = None,
        company_id: uuid.UUID | None = None,
        opportunity_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Memory]:
        """Query memories in this workspace with explicit, typed filters.

        `status=None` (the default) means "only `active`" — see the module
        docstring. Pass `status` explicitly to see a historical state
        (`superseded`/`discarded`), or `MemoryStatus.ACTIVE` itself for the same
        result as the default, spelled out. A subject filter
        (`contact_id`/`company_id`/`opportunity_id`) naming an id from another
        workspace simply matches no row — `Memory`'s subject columns are
        workspace-scoped by the same `tenant_fk` constraint `remember` already
        validates against at write time, so no cross-tenant memory can ever
        carry such an id in the first place.
        """
        effective_status = status if status is not None else MemoryStatus.ACTIVE
        return self.memories.list_filtered(
            kind=kind,
            origin=origin,
            status=effective_status,
            contact_id=contact_id,
            company_id=company_id,
            opportunity_id=opportunity_id,
            limit=limit,
            offset=offset,
        )

    def remember(
        self,
        *,
        kind: MemoryKind,
        origin: MemoryOrigin,
        content: dict[str, Any],
        observed_at: datetime,
        confidence: float | None = None,
        contact_id: uuid.UUID | None = None,
        company_id: uuid.UUID | None = None,
        opportunity_id: uuid.UUID | None = None,
        source_conversation_id: uuid.UUID | None = None,
        source_analysis_id: uuid.UUID | None = None,
        actor: Actor | None = None,
        commit: bool = True,
    ) -> Memory:
        actor = actor or self.ctx.actor
        validated = parse_memory_content(content)
        self._require_contact(contact_id)
        self._require_company(company_id)
        self._require_opportunity(opportunity_id)
        source_type, fingerprint = self._require_source(source_conversation_id, source_analysis_id)

        memory = self.memories.add(
            Memory(
                kind=kind,
                origin=origin,
                status=MemoryStatus.ACTIVE,
                content=validated.model_dump(mode="json"),
                schema_version=MEMORY_SCHEMA_VERSION,
                confidence=confidence,
                observed_at=observed_at,
                contact_id=contact_id,
                company_id=company_id,
                opportunity_id=opportunity_id,
                source_type=source_type,
                source_conversation_id=source_conversation_id,
                source_analysis_id=source_analysis_id,
                source_fingerprint=fingerprint,
                created_by_type=actor.type,
                created_by_user_id=actor.user_id,
            )
        )
        self.activity.record(
            actor=actor,
            event_type=ActivityEventType.MEMORY_RECORDED,
            entity_type="memory",
            entity_id=memory.id,
            summary=f"Recorded a {kind.value} ({origin.value}): {validated.statement}"[:500],
            data={"kind": kind.value, "origin": origin.value, "source_type": source_type.value},
        )
        if commit:
            self.session.commit()
        return memory

    def supersede(
        self,
        memory_id: uuid.UUID,
        *,
        replacement_id: uuid.UUID,
        actor: Actor | None = None,
        commit: bool = True,
    ) -> Memory:
        """`replacement_id` must already be a `Memory` in this workspace, and must
        not be `memory_id` itself — the new row is created first (via `remember`),
        then the old one is pointed at it; a memory can never supersede itself.
        Checked before any row is read for update or mutated, mirroring `Memory`'s
        own `not_superseded_by_self` CHECK constraint (`nerva.models.memory`) at
        the service boundary — without this, the self-reference would reach the
        database and fail only at `flush()` with a raw `IntegrityError`, surfacing
        as an unhandled 500 instead of the 409 an invalid transition should be.

        `actor` defaults to the authenticated caller (`self.ctx.actor`), same as
        `remember` — an automated supersession (e.g. M6.3's memory-formation
        engine) passes `Actor.nerva()` explicitly instead. `commit=False` defers
        the commit to the caller — see the module docstring."""
        if replacement_id == memory_id:
            raise InvalidStateTransitionError(
                "A memory cannot supersede itself.",
                details={"memory_id": str(memory_id)},
            )
        if self.memories.get(replacement_id) is None:
            raise InvalidReferenceError(
                "replacement_id does not refer to a memory in this workspace."
            )

        def apply(memory: Memory) -> None:
            memory.superseded_by_id = replacement_id

        return self._resolve(
            memory_id,
            MemoryStatus.SUPERSEDED,
            event_type=ActivityEventType.MEMORY_SUPERSEDED,
            apply=apply,
            extra={"superseded_by_id": str(replacement_id)},
            actor=actor,
            commit=commit,
        )

    def replace(
        self,
        memory_id: uuid.UUID,
        *,
        kind: MemoryKind,
        origin: MemoryOrigin,
        content: dict[str, Any],
        observed_at: datetime,
        confidence: float | None = None,
        contact_id: uuid.UUID | None = None,
        company_id: uuid.UUID | None = None,
        opportunity_id: uuid.UUID | None = None,
        source_conversation_id: uuid.UUID | None = None,
        source_analysis_id: uuid.UUID | None = None,
        actor: Actor | None = None,
        commit: bool = True,
    ) -> Memory:
        """Atomically creates a new memory and supersedes `memory_id` with it,
        in one transaction — `remember(..., commit=False)` immediately followed
        by `supersede(memory_id, replacement_id=<the new memory>.id, commit=
        commit)`, exactly the composition `MemoryFormationService` used to do
        by hand before M6.3 Block 2 (see its module docstring), now available
        to any caller, including `POST /memories/{id}/replace`.

        `memory_id` is checked *first* — existence (and, by construction of
        `MemoryRepository.get_for_update`'s workspace-scoped query, that it
        belongs to this workspace) and that it is still `active` — before the
        new memory is created, under the same row lock `supersede` would take
        anyway. An invalid call therefore fails before anything is written:
        never an orphaned replacement with nothing left to point at. All of
        `remember`'s own validation (content contract, subject/source
        references) still applies to the new memory exactly as it would via a
        plain `POST /memories` — this adds no new rules, only the ordering
        guarantee and the single commit.

        On failure between creating the replacement and resolving the
        original, only that work is undone — via a SAVEPOINT (`Session.
        begin_nested()`), not `self.session.rollback()`. A bare
        `session.rollback()` discards the *entire* outer transaction,
        including anything the caller already flushed or committed-onto-a-
        savepoint before calling `replace()`; this is exactly what M6.3 Block
        3's review flagged, since `replace` never owns the whole session, only
        this one operation within it. `remember` and `supersede` therefore
        both run with `commit=False` inside the SAVEPOINT, and releasing it
        (`nested.commit()`) is itself inside the `try`: `nested.commit()`
        sends a real `RELEASE SAVEPOINT` to the database — it is a round
        trip, not a no-op — which folds the two writes into the still-open
        outer transaction without issuing a `COMMIT`; only after that
        succeeds, and only if the caller asked for `commit=True`, is the
        outer transaction itself committed with `Session.commit()` — exactly
        once, same as before. If *anything* in the `try` raises, including
        `nested.commit()` failing (the release itself can fail, not only the
        work it is releasing), the SAVEPOINT is rolled back (`nested.
        rollback()`) instead, which undoes precisely the new memory's flush
        and the old memory's status change — SQLAlchemy expunges the objects
        that SAVEPOINT's flush produced, so neither lingers half-attached in
        the session's identity map — and leaves every earlier pending write,
        and the session itself, exactly as the caller left it. (A failed
        `nested.commit()` deactivates that SAVEPOINT internally regardless of
        cause, so the subsequent `nested.rollback()` is always safe to call —
        it finishes cleanly whether or not the database ever saw the failed
        `RELEASE SAVEPOINT`.) The row lock on `memory_id` (via `get_for_
        update`, below) is acquired *before* the SAVEPOINT begins and is
        unaffected by any of this: a SAVEPOINT rollback undoes writes, not
        locks already held by the surrounding transaction, which is exactly
        what keeps two concurrent `replace` calls for the same memory
        serialized.

        This recovery covers every failure through `nested.commit()`. It does
        not extend past it: if the final, outer `self.session.commit()` fails
        (reached only when `commit=True` and the SAVEPOINT already released
        cleanly), `replace` does not attempt to recover the session itself —
        that is the same, ordinary "a commit failed, the caller must decide
        how to proceed" contract every other commit in this module already
        carries (`remember`, `supersede`, `_resolve`), and nothing about
        composing two writes under one SAVEPOINT changes it: the *session*
        is recoverable by the caller the normal SQLAlchemy way — their own
        `session.rollback()`, never something `replace` does on their behalf,
        for the same reason it never reaches for a bare `session.rollback()`
        on its own failures either (see above). That call is unconditional:
        it checks out a fresh connection on the session's next use and makes
        it fully usable again regardless of why the commit failed, confirmed
        against a real dropped connection during `self.session.commit()`.

        Session-recoverable is not the same claim as "nothing was persisted,"
        though, and `replace` cannot always tell the two apart from the
        exception alone. A failure the database raises *before* acknowledging
        the `COMMIT` — a constraint violation, a serialization failure,
        anything PostgreSQL rejects outright — is a confirmed abort: nothing
        reached disk, both writes are gone, full stop. But a failure from
        losing the connection *during* the commit round trip (the `COMMIT` is
        sent, the network drops before the acknowledgment comes back) is
        genuinely ambiguous: PostgreSQL may have already committed before the
        connection died, in which case both writes *are* persisted even
        though `replace` observed a failure and raised. This is not a gap
        `replace` introduces — it is the ordinary, irreducible
        "commit outcome unknown after a lost connection" problem any
        transaction has, SAVEPOINT or not, and no rollback inside `replace`
        can resolve it: there is no connection left to roll anything back on.
        A caller that must know for certain has to re-check the row's actual
        state on a fresh connection; `replace` does not attempt that itself,
        since guessing what the caller wants done with an ambiguous outcome
        is not this method's call to make."""
        actor = actor or self.ctx.actor
        existing = self.memories.get_for_update(memory_id)
        if existing is None:
            raise NotFoundError("Memory not found.")
        ensure_transition(existing.status, MemoryStatus.SUPERSEDED)

        nested = self.session.begin_nested()
        try:
            replacement = self.remember(
                kind=kind,
                origin=origin,
                content=content,
                observed_at=observed_at,
                confidence=confidence,
                contact_id=contact_id,
                company_id=company_id,
                opportunity_id=opportunity_id,
                source_conversation_id=source_conversation_id,
                source_analysis_id=source_analysis_id,
                actor=actor,
                commit=False,
            )
            self.supersede(memory_id, replacement_id=replacement.id, actor=actor, commit=False)
            nested.commit()
        except Exception:
            nested.rollback()
            raise
        if commit:
            self.session.commit()
        return replacement

    def discard(
        self,
        memory_id: uuid.UUID,
        *,
        reason: str | None = None,
        actor: Actor | None = None,
        commit: bool = True,
    ) -> Memory:
        """`actor` and `commit` default the same way `supersede` does."""

        def apply(memory: Memory) -> None:
            memory.resolution_reason = reason

        return self._resolve(
            memory_id,
            MemoryStatus.DISCARDED,
            event_type=ActivityEventType.MEMORY_DISCARDED,
            apply=apply,
            extra={"reason": reason} if reason else None,
            actor=actor,
            commit=commit,
        )

    def _resolve(
        self,
        memory_id: uuid.UUID,
        target: MemoryStatus,
        *,
        event_type: ActivityEventType,
        apply: Callable[[Memory], None],
        extra: dict[str, Any] | None = None,
        actor: Actor | None = None,
        commit: bool = True,
    ) -> Memory:
        actor = actor or self.ctx.actor
        memory = self.memories.get_for_update(memory_id)
        if memory is None:
            raise NotFoundError("Memory not found.")
        previous = memory.status
        ensure_transition(previous, target)  # raises InvalidStateTransitionError -> 409
        memory.status = target
        memory.resolved_at = utcnow()
        apply(memory)
        self.activity.record(
            actor=actor,
            event_type=event_type,
            entity_type="memory",
            entity_id=memory.id,
            summary=f"Memory marked {target.value}.",
            data={"from_status": previous.value, "to_status": target.value, **(extra or {})},
        )
        if commit:
            self.session.commit()
        return memory

    def _require_source(
        self, conversation_id: uuid.UUID | None, analysis_id: uuid.UUID | None
    ) -> tuple[MemorySourceType, str | None]:
        if conversation_id is not None and analysis_id is not None:
            raise InvalidReferenceError(
                "A memory's source is a conversation or an analysis, never both."
            )
        if analysis_id is not None:
            analysis = self.analyses.get(analysis_id)
            if analysis is None:
                raise InvalidReferenceError(
                    "source_analysis_id does not refer to an analysis in this workspace."
                )
            return MemorySourceType.ANALYSIS, analysis.input_fingerprint
        if conversation_id is not None:
            self._require_conversation(conversation_id)
            return MemorySourceType.CONVERSATION, None
        return MemorySourceType.MANUAL, None
