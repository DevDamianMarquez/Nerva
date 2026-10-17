"""Workspace memory: durable, provenanced facts and preferences Nerva can recall
later, stored relationally. Deliberately simple for v0.1 — no embeddings, no
vector index; retrieval is plain SQL (see `nerva.nerva_memory`'s module
docstring for what is explicitly future work).

A memory is about the workspace as a whole, or about at most one contact,
company or opportunity (`contact_id`/`company_id`/`opportunity_id`, same
"at most one subject" shape `Memory` already used before this block).

Three things make this a *verifiable* memory rather than a free-text blob a
model once said:

1. **Structured, validated content.** `content` is a JSONB payload that must
   satisfy `nerva_memory.content.MemoryContent` before it is ever written —
   never a raw email body, a raw provider response, or unstructured prose
   nothing downstream can safely parse. `schema_version` is stamped at write
   time so that shape can evolve later without a migration touching old rows
   (mirrors `ConversationAnalysis.schema_version`).
2. **Provenance.** `source_type` says what produced the memory — an entire
   `Conversation`, a completed `ConversationAnalysis`, or nothing at all
   (`manual` — a human or Nerva recorded it directly). `source_fingerprint`
   pins an analysis-derived memory to the *specific* analysis content that
   produced it, decoupled from that row's current (mutable-in-place) value —
   the same reason `BusinessSignal.source_fingerprint` exists.
3. **An honest epistemic status.** `origin` says whether the content was
   stated directly (`observed`) or derived/guessed by a model (`inferred`);
   `confidence` is only ever set for an inference — see the `MemoryOrigin`
   docstring and `nerva.nerva_memory.lifecycle` for why an inference never
   silently becomes an observation in place.

`status` is the lifecycle (`active -> superseded | discarded`, both terminal —
`nerva.nerva_memory.lifecycle`). Resolving a memory never rewrites `content`:
a fresher observation or inference is always a *new* row, linked back via
`superseded_by_id` on the old one, so what Nerva believed at any point in time
stays reconstructable.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import MemoryKind, MemoryOrigin, MemorySourceType, MemoryStatus
from nerva.db.base import (
    CreatedByMixin,
    UpdatedAtMixin,
    WorkspaceOwnedModel,
    actor_consistency_check,
    enum_type,
    tenant_fk,
)


class Memory(WorkspaceOwnedModel, UpdatedAtMixin, CreatedByMixin):
    __tablename__ = "memories"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index("ix_memories_workspace_id_kind", "workspace_id", "kind"),
        Index("ix_memories_workspace_id_status", "workspace_id", "status"),
        CheckConstraint(
            "num_nonnulls(contact_id, company_id, opportunity_id) <= 1",
            name="at_most_one_subject",
        ),
        actor_consistency_check("created_by_type", "created_by_user_id"),
        tenant_fk("contact_id", "contacts"),
        tenant_fk("company_id", "companies"),
        tenant_fk("opportunity_id", "opportunities"),
        # A disconnected integration's conversations/analyses outlive it (§10 of
        # docs/ARCHITECTURE.md); losing the specific upstream row on delete should
        # likewise never take the memory it produced down with it. The ondelete
        # action is restricted to the referencing column itself (PostgreSQL 15+
        # `SET NULL (column)` syntax) — a plain `SET NULL` on a *composite* FK nulls
        # every column in the constraint, including `workspace_id`, which is
        # NOT NULL here and would make the delete fail instead of succeeding.
        tenant_fk(
            "source_conversation_id", "conversations", ondelete="SET NULL (source_conversation_id)"
        ),
        tenant_fk(
            "source_analysis_id",
            "conversation_analyses",
            ondelete="SET NULL (source_analysis_id)",
        ),
        tenant_fk("superseded_by_id", "memories"),
        CheckConstraint(
            "(origin = 'inferred') = (confidence IS NOT NULL)",
            name="confidence_only_when_inferred",
        ),
        # One-directional: a set id always matches its declared source_type, but
        # `source_type` may legitimately outlive the id once the row it pointed at
        # is deleted and the FK above nulls it — see the ondelete note above.
        CheckConstraint(
            "(source_conversation_id IS NULL) OR (source_type = 'conversation')",
            name="source_conversation_matches_type",
        ),
        CheckConstraint(
            "(source_analysis_id IS NULL) OR (source_type = 'analysis')",
            name="source_analysis_matches_type",
        ),
        CheckConstraint(
            "(source_type = 'analysis') = (source_fingerprint IS NOT NULL)",
            name="source_fingerprint_only_for_analysis",
        ),
        CheckConstraint(
            "(status = 'active') = (resolved_at IS NULL)",
            name="resolved_at_matches_status",
        ),
        CheckConstraint(
            "(status = 'superseded') = (superseded_by_id IS NOT NULL)",
            name="superseded_by_only_when_superseded",
        ),
        CheckConstraint(
            "superseded_by_id IS DISTINCT FROM id",
            name="not_superseded_by_self",
        ),
    )

    kind: Mapped[MemoryKind] = mapped_column(
        enum_type(MemoryKind, "memory_kind"),
        default=MemoryKind.FACT,
        server_default=MemoryKind.FACT.value,
    )
    origin: Mapped[MemoryOrigin] = mapped_column(enum_type(MemoryOrigin, "memory_origin"))
    status: Mapped[MemoryStatus] = mapped_column(
        enum_type(MemoryStatus, "memory_status"),
        default=MemoryStatus.ACTIVE,
        server_default=MemoryStatus.ACTIVE.value,
    )
    # Validated against `nerva_memory.content.MemoryContent` before it ever reaches
    # this column — see the module docstring. Never a raw email body.
    content: Mapped[dict[str, Any]]
    schema_version: Mapped[int]
    # Set if and only if `origin` is `inferred` (see the CHECK above) — a directly
    # observed fact is not a probabilistic guess and must not claim one.
    confidence: Mapped[float | None]
    # When the underlying fact was true/stated — not when Nerva got around to
    # recording it (`created_at`), which may be days later via a backfill.
    observed_at: Mapped[datetime]

    # Subject of the memory. All NULL means it concerns the workspace itself.
    contact_id: Mapped[uuid.UUID | None]
    company_id: Mapped[uuid.UUID | None]
    opportunity_id: Mapped[uuid.UUID | None]

    source_type: Mapped[MemorySourceType] = mapped_column(
        enum_type(MemorySourceType, "memory_source_type")
    )
    source_conversation_id: Mapped[uuid.UUID | None]
    source_analysis_id: Mapped[uuid.UUID | None]
    # Copied from `ConversationAnalysis.input_fingerprint` at the moment this memory
    # was derived from it — decoupled from that row's current value on purpose, the
    # same reason `BusinessSignal.source_fingerprint` exists. NULL for any source
    # type other than `analysis`, whose own rows never change in place.
    source_fingerprint: Mapped[str | None] = mapped_column(String(64))

    # Set together, once, the moment `status` leaves `active` — never touched again.
    resolved_at: Mapped[datetime | None]
    # The memory that replaced this one, if resolution was a supersession. Who made
    # the decision and when is the activity trail (`memory.superseded` /
    # `memory.discarded`), not a second copy of it on this row.
    superseded_by_id: Mapped[uuid.UUID | None]
    resolution_reason: Mapped[str | None] = mapped_column(Text)
