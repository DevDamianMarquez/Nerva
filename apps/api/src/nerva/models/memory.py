"""Workspace memory: durable facts and preferences, stored relationally.

Deliberately simple for v0.1. There are no embeddings and no vector index; retrieval
is plain SQL. A memory is about the workspace as a whole, or about at most one
contact, company or opportunity.
"""

import uuid

from sqlalchemy import CheckConstraint, Index, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import MemoryKind
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
        CheckConstraint(
            "num_nonnulls(contact_id, company_id, opportunity_id) <= 1",
            name="at_most_one_subject",
        ),
        actor_consistency_check("created_by_type", "created_by_user_id"),
        tenant_fk("contact_id", "contacts"),
        tenant_fk("company_id", "companies"),
        tenant_fk("opportunity_id", "opportunities"),
    )

    kind: Mapped[MemoryKind] = mapped_column(
        enum_type(MemoryKind, "memory_kind"),
        default=MemoryKind.FACT,
        server_default=MemoryKind.FACT.value,
    )
    content: Mapped[str] = mapped_column(Text)
    # Subject of the memory. All NULL means it concerns the workspace itself.
    contact_id: Mapped[uuid.UUID | None]
    company_id: Mapped[uuid.UUID | None]
    opportunity_id: Mapped[uuid.UUID | None]
