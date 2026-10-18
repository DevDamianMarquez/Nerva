"""Retrieves and assembles a bounded, workspace-safe memory context for a
conversation (M6.4, Block 2 — database retrieval only).

`MemoryContextService.get_context_for_conversation` is the only code that
builds a `nerva_core.memory_context.MemoryContextTarget`/`MemoryCandidate`
from real rows and hands them to `select_context_memories` — the pure
selector from Block 1, which remains the sole authority on eligibility,
ranking and truncation. This module never re-implements any of that: it only
resolves *which* candidates exist and *what* subject chain they are being
evaluated against, both as plain workspace-scoped reads.

Nothing here sends anything to an LLM, builds a prompt, or creates/changes a
`Memory` row — see `nerva.nerva_ai.client.AnalysisInput` (unchanged) and
`nerva.services.memory_formation` (the only writer) respectively. Block 3
(wiring this into `AnalysisService`/`AnalysisInput`) and Block 4 (recording
which memory ids were actually used) do not exist yet; nothing here depends
on them or anticipates their shape.

## Tenant isolation

Every read in this module goes through a `ScopedRepository` subclass bound to
`ctx.workspace_id` at construction (`ConversationRepository`,
`ContactRepository`, `OpportunityRepository`, `MemoryRepository` —
`nerva.repositories.workspace_data`). Each one's `_select()` filters by
`workspace_id` before any other condition, so there is no raw, unscoped
`select(Memory)`/`session.query(...)` anywhere in this file followed by a
Python-side filter standing in for tenant isolation. An id read off one
already-scoped row (e.g. `conversation.contact_id`) is only ever used as an
equality filter *inside* another already-scoped query; even a value that
happened to collide with another workspace's id could not pull that other
workspace's row through a `ScopedRepository`, because the composite tenant
foreign keys on `Memory`/`Contact`/`Opportunity` (`nerva.models.memory`,
`nerva.models.crm`) make that cross-workspace reference impossible to create
in the first place (`docs/ARCHITECTURE.md` §4) — there is no row to leak even
if an id were guessed correctly.

## Known limitation: `opportunity_id` is not always resolvable

`Opportunity.conversation_id` (`nerva.models.crm`) carries no unique
constraint — nothing in the schema stops two or more opportunities from
naming the same conversation as where they were identified (two independent
deals surfaced from one thread, or a human linking a second `Opportunity` to
it manually via `POST /opportunities`). "The opportunity for this
conversation" is therefore not a well-defined, unambiguous relationship the
way `Conversation.contact_id` or `Contact.company_id` are (both plain,
single-valued foreign keys). This service resolves `opportunity_id` only when
it is genuinely unambiguous — exactly one `Opportunity` names this
conversation (`OpportunityRepository.get_by_conversation`) — and leaves it
unresolved (`None`) when there are zero or more than one, rather than
guessing which one is "the" opportunity. This is deliberate: inventing a
tie-break rule (most recent, first created, …) would be exactly the kind of
invented relationship this block was asked not to add. A product decision
(e.g. a uniqueness constraint, or an explicit "primary opportunity" concept)
would be required to close this gap; none exists today, so opportunity-scoped
memory is simply unreachable for a conversation with zero or ambiguous
opportunity links.

## Candidate volume: no cap upstream of the selector

`MemoryRepository.list_candidates_for_context` fetches the **complete**
`ACTIVE`, subject-relevant candidate set — no `limit`. An earlier version of
this service capped it at an arbitrary `_MAX_CANDIDATES` as a "safety
ceiling" independent of the caller's `max_items`; that was a correctness bug,
not a safe optimization: the database's cheap, available ordering
(`created_at`, when Nerva *recorded* the belief) does not match
`select_context_memories`'s own ranking criteria (`scope`, then `origin`,
then `observed_at` — when the belief was *true* — then `id`), so capping the
candidate set before the selector ever sees it could silently drop a
higher-priority memory (e.g. the one opportunity-scoped fact) in favor of
keeping a lower-priority one (e.g. an old workspace-level note) that merely
happened to be more recently *recorded*. `select_context_memories` is
documented, both in its own module and in this one, as the sole authority
over final selection — a cap applied before it runs contradicts that claim
regardless of how unlikely it is to matter in practice, so none is applied
here. `MemoryRepository.list_candidates_for_context`'s own docstring carries
the full argument.

This is the minimal correct fix for the product's current state, not a
performance stance for every future one: the query is already narrowed to
`ACTIVE` plus an exact subject match (never "every memory the workspace has
ever had"), and there is no evidence today of a workspace with enough active,
subject-relevant memories to make one unbounded query a real concern. If that
changes, the right next step is either exhaustive pagination of this same
query (fetch every page, never truncate) or a `ORDER BY` provably equivalent
to the selector's own priority before applying any limit — not a second,
diverging ranking implementation in SQL.

## Failure behavior

This service raises, it does not swallow. An unknown or cross-workspace
`conversation_id` raises `NotFoundError` (404), exactly like every other
service in this codebase that takes one (`AnalysisService.analyze`,
`MemoryFormationService.form_estimated_value_memory`); a `Memory.content`
value that fails its own content contract on read raises `MemoryContentError`
(`nerva.nerva_memory.content`) rather than being silently skipped — a
security, isolation, or data-integrity failure must fail closed, never
degrade into an empty context. A conversation with no relevant memories at
all is a *legitimate* empty result (`()`), not an error, and is returned as
such. Graceful degradation for a pipeline that must complete regardless (e.g.
"if memory retrieval breaks, proceed with no context anyway") is explicitly a
decision about *how this service is called* for a future block to make, not
something this service decides about itself.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.errors import NotFoundError
from nerva.models import Conversation, Memory
from nerva.nerva_core.memory_context import (
    MemoryCandidate,
    MemoryContextTarget,
    select_context_memories,
)
from nerva.nerva_memory.content import parse_memory_content
from nerva.repositories.workspace_data import (
    ContactRepository,
    ConversationRepository,
    MemoryRepository,
    OpportunityRepository,
)
from nerva.services.base import ScopedService


class MemoryContextService(ScopedService):
    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        super().__init__(session, ctx)
        self.conversations = ConversationRepository(session, ctx.workspace_id)
        self.contacts = ContactRepository(session, ctx.workspace_id)
        self.opportunities = OpportunityRepository(session, ctx.workspace_id)
        self.memories = MemoryRepository(session, ctx.workspace_id)

    def get_context_for_conversation(
        self,
        *,
        conversation_id: uuid.UUID,
        max_items: int,
        max_chars: int,
    ) -> Sequence[MemoryCandidate]:
        """Raises `NotFoundError` if `conversation_id` is not a conversation
        in this workspace — see the module docstring's "Failure behavior".
        Otherwise always returns a sequence (possibly empty): see
        `nerva_core.memory_context.select_context_memories` for the
        eligibility, ranking and truncation rules actually applied — nothing
        here re-implements any part of them."""
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.")

        target = self._resolve_target(conversation)
        rows = self.memories.list_candidates_for_context(
            contact_id=target.contact_id,
            company_id=target.company_id,
            opportunity_id=target.opportunity_id,
        )
        candidates = [_to_candidate(row) for row in rows]
        return select_context_memories(
            candidates, target=target, max_items=max_items, max_chars=max_chars
        )

    def _resolve_target(self, conversation: Conversation) -> MemoryContextTarget:
        """Builds the subject chain `select_context_memories` ranks against,
        from relationships that exist unambiguously today —
        `Conversation.contact_id` and `Contact.company_id`, both plain,
        single-valued foreign keys — plus `opportunity_id`, resolved only
        when exactly one `Opportunity` names this conversation; see the
        module docstring's "Known limitation" for why zero or more than one
        leaves it unresolved rather than guessed."""
        company_id: uuid.UUID | None = None
        if conversation.contact_id is not None:
            contact = self.contacts.get(conversation.contact_id)
            if contact is not None:
                company_id = contact.company_id

        opportunities = self.opportunities.get_by_conversation(conversation.id)
        opportunity_id = opportunities[0].id if len(opportunities) == 1 else None

        return MemoryContextTarget(
            conversation_id=conversation.id,
            contact_id=conversation.contact_id,
            company_id=company_id,
            opportunity_id=opportunity_id,
        )


def _to_candidate(memory: Memory) -> MemoryCandidate:
    """Maps one `Memory` row to the minimal, already-resolved slice Block 1's
    selector needs, without reinterpreting any of it: `origin`/`confidence`/
    `kind`/timestamps/subject ids/`source_conversation_id` are copied through
    unchanged. `statement` is read via `parse_memory_content` rather than a
    raw `memory.content["statement"]` lookup, so a `Memory.content` value
    that somehow fails its own contract raises `MemoryContentError`
    (`nerva.nerva_memory.content`) instead of a bare `KeyError` or a silently
    wrong value — failing closed, per the module docstring."""
    content = parse_memory_content(memory.content)
    return MemoryCandidate(
        id=memory.id,
        status=memory.status,
        kind=memory.kind,
        origin=memory.origin,
        confidence=memory.confidence,
        statement=content.statement,
        observed_at=memory.observed_at,
        contact_id=memory.contact_id,
        company_id=memory.company_id,
        opportunity_id=memory.opportunity_id,
        source_conversation_id=memory.source_conversation_id,
    )
