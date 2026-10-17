"""Workspace memory: record, list, read, supersede, replace and discard.

Any workspace member may read or write memory — recording or resolving a
memory touches no external credential or provider, the same reasoning that
already makes `signals`' confirm/dismiss and `actions`' approve/reject
member-readable/writable (only `actions.execute_action`, which spends a real
external resource, is owner-only). A memory is data Nerva can recall, never an
instruction it can act on — nothing here lets a client make the API execute
anything beyond writing or resolving a `Memory` row.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Query

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.core.enums import MemoryKind, MemoryOrigin, MemoryStatus
from nerva.core.errors import InvalidContentError
from nerva.models import Memory
from nerva.nerva_memory.errors import MemoryContentError
from nerva.schemas.common import WORKSPACE_ERRORS, ErrorResponse
from nerva.schemas.memory import MemoryCreate, MemoryDiscard, MemoryRead, MemorySupersede
from nerva.services.memory import MemoryService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/memories",
    tags=["memory"],
    responses=WORKSPACE_ERRORS,
)

CREATE_ERRORS: dict[int | str, dict[str, Any]] = {
    422: {
        "model": ErrorResponse,
        "description": "Invalid content, or a subject/source id outside this workspace.",
    }
}


def _to_read(memory: Memory) -> MemoryRead:
    """`content` is a validated JSONB payload (see `nerva.models.memory`); flattened
    here into typed fields rather than ever returned as a raw dict, the same way
    `routes.signals._to_read` flattens `BusinessSignal.data`."""
    content = memory.content
    return MemoryRead(
        id=memory.id,
        workspace_id=memory.workspace_id,
        kind=memory.kind,
        origin=memory.origin,
        status=memory.status,
        statement=content["statement"],
        attributes=content.get("attributes", {}),
        schema_version=memory.schema_version,
        confidence=memory.confidence,
        observed_at=memory.observed_at,
        contact_id=memory.contact_id,
        company_id=memory.company_id,
        opportunity_id=memory.opportunity_id,
        source_type=memory.source_type,
        source_conversation_id=memory.source_conversation_id,
        source_analysis_id=memory.source_analysis_id,
        source_fingerprint=memory.source_fingerprint,
        resolved_at=memory.resolved_at,
        superseded_by_id=memory.superseded_by_id,
        resolution_reason=memory.resolution_reason,
        created_at=memory.created_at,
        updated_at=memory.updated_at,
    )


@router.post("", response_model=MemoryRead, status_code=201, responses=CREATE_ERRORS)
def remember(
    workspace_id: uuid.UUID, body: MemoryCreate, ctx: WorkspaceCtx, session: SessionDep
) -> MemoryRead:
    """Record a memory. Never decides what is worth remembering — that decision has
    already been made by the caller (a human, today; a future reasoning layer,
    later) — this only enforces the content contract and provenance rules."""
    service = MemoryService(session, ctx)
    try:
        memory = service.remember(
            kind=body.kind,
            origin=body.origin,
            content={"statement": body.statement, "attributes": body.attributes},
            observed_at=body.observed_at,
            confidence=body.confidence,
            contact_id=body.contact_id,
            company_id=body.company_id,
            opportunity_id=body.opportunity_id,
            source_conversation_id=body.source_conversation_id,
            source_analysis_id=body.source_analysis_id,
        )
    except MemoryContentError as exc:
        raise InvalidContentError(str(exc)) from exc
    return _to_read(memory)


@router.get("", response_model=list[MemoryRead])
def list_memories(
    workspace_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    page: PaginationDep,
    kind: MemoryKind | None = None,
    origin: MemoryOrigin | None = None,
    status: Annotated[
        MemoryStatus | None,
        Query(
            description=(
                "Only memories in this lifecycle state. Omitted: only `active` "
                "memories — a superseded or discarded one is never returned by "
                "default, only when this is passed explicitly (including "
                "`active` itself, spelled out)."
            )
        ),
    ] = None,
    contact_id: uuid.UUID | None = None,
    company_id: uuid.UUID | None = None,
    opportunity_id: uuid.UUID | None = None,
) -> list[MemoryRead]:
    """Newest first (`created_at` desc, `id` as a stable tiebreaker)."""
    memories = MemoryService(session, ctx).list(
        kind=kind,
        origin=origin,
        status=status,
        contact_id=contact_id,
        company_id=company_id,
        opportunity_id=opportunity_id,
        limit=page.limit,
        offset=page.offset,
    )
    return [_to_read(memory) for memory in memories]


@router.get("/{memory_id}", response_model=MemoryRead)
def read_memory(
    workspace_id: uuid.UUID, memory_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> MemoryRead:
    return _to_read(MemoryService(session, ctx).get(memory_id))


@router.post("/{memory_id}/supersede", response_model=MemoryRead, responses=CREATE_ERRORS)
def supersede_memory(
    workspace_id: uuid.UUID,
    memory_id: uuid.UUID,
    body: MemorySupersede,
    ctx: WorkspaceCtx,
    session: SessionDep,
) -> MemoryRead:
    """`replacement_id` must already be an existing memory in this workspace —
    typically created with a prior call to `POST /memories` — never created as a
    side effect of this call. A memory that is not `active` (including an already
    superseded or discarded one) raises `409 invalid_state_transition`."""
    return _to_read(
        MemoryService(session, ctx).supersede(memory_id, replacement_id=body.replacement_id)
    )


@router.post(
    "/{memory_id}/replace", response_model=MemoryRead, status_code=201, responses=CREATE_ERRORS
)
def replace_memory(
    workspace_id: uuid.UUID,
    memory_id: uuid.UUID,
    body: MemoryCreate,
    ctx: WorkspaceCtx,
    session: SessionDep,
) -> MemoryRead:
    """Atomically creates a new memory from `body` and supersedes `memory_id`
    with it, in one transaction — the single-request alternative to calling
    `POST /memories` and then `POST /memories/{id}/supersede` separately (both
    still work on their own; see `MemoryService.replace`'s docstring for when
    each shape is the right one). `body` is exactly a `MemoryCreate`: no field
    is added for this operation, since the new memory's shape is identical
    either way. Returns the **new, active** memory — not `memory_id`, which
    this leaves `superseded` (contrast with `supersede_memory` above, which
    returns the old, now-resolved one). `memory_id` not found, in another
    workspace, or not `active` raises `404`/`409` before anything is written."""
    service = MemoryService(session, ctx)
    try:
        memory = service.replace(
            memory_id,
            kind=body.kind,
            origin=body.origin,
            content={"statement": body.statement, "attributes": body.attributes},
            observed_at=body.observed_at,
            confidence=body.confidence,
            contact_id=body.contact_id,
            company_id=body.company_id,
            opportunity_id=body.opportunity_id,
            source_conversation_id=body.source_conversation_id,
            source_analysis_id=body.source_analysis_id,
        )
    except MemoryContentError as exc:
        raise InvalidContentError(str(exc)) from exc
    return _to_read(memory)


@router.post("/{memory_id}/discard", response_model=MemoryRead)
def discard_memory(
    workspace_id: uuid.UUID,
    memory_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    body: Annotated[MemoryDiscard | None, Body()] = None,
) -> MemoryRead:
    """A memory that is not `active` (including an already discarded one) raises
    `409 invalid_state_transition`."""
    reason = body.reason if body else None
    return _to_read(MemoryService(session, ctx).discard(memory_id, reason=reason))
