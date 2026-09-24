"""Actions: things Nerva proposes to do, gated by human approval."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import ActionStatus
from nerva.db.base import (
    CreatedByMixin,
    UpdatedAtMixin,
    WorkspaceOwnedModel,
    actor_consistency_check,
    enum_type,
    tenant_fk,
)


class Action(WorkspaceOwnedModel, UpdatedAtMixin, CreatedByMixin):
    """Lifecycle: proposed -> approved -> executing -> completed | failed,
    or proposed -> rejected. Legal transitions live in `nerva_actions.lifecycle`."""

    __tablename__ = "actions"
    __table_args__ = (
        Index("ix_actions_workspace_id_status_created_at", "workspace_id", "status", "created_at"),
        actor_consistency_check("created_by_type", "created_by_user_id"),
        tenant_fk("opportunity_id", "opportunities"),
    )

    opportunity_id: Mapped[uuid.UUID | None]
    # Free text (validated against `ActionType` at the API) so new types need no migration.
    type: Mapped[str] = mapped_column(String(64))
    status: Mapped[ActionStatus] = mapped_column(
        enum_type(ActionStatus, "action_status"),
        default=ActionStatus.PROPOSED,
        server_default=ActionStatus.PROPOSED.value,
    )
    # Short human-readable description shown in approval queues.
    summary: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")
    # Outcome of execution; for failed actions, holds the error description.
    result: Mapped[dict[str, Any] | None]

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    rejected_at: Mapped[datetime | None]
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
