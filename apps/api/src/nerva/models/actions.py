"""Actions: things Nerva proposes to do, gated by human approval."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, text
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

# Named explicitly (not left to the `uq_`/`ix_` naming-convention machinery in
# `db.base`) because a *partial* unique index has no `UniqueConstraint`
# equivalent in SQLAlchemy — it can only be expressed as an `Index(unique=True,
# postgresql_where=...)`, which the naming convention treats as a plain `ix_`,
# not a `uq_`. Spelled out here so `services.proposal_materialization` can
# match on this exact name when distinguishing *this* violation from any
# other `IntegrityError` `Action` could raise — see that module's own
# docstring for why that distinction matters.
PROPOSAL_FINGERPRINT_UNIQUE_INDEX = "uq_actions_workspace_id_proposal_fingerprint_partial"


class Action(WorkspaceOwnedModel, UpdatedAtMixin, CreatedByMixin):
    """Lifecycle: proposed -> approved -> executing -> completed | failed,
    or proposed -> rejected. Legal transitions live in `nerva_actions.lifecycle`.

    `proposal_fingerprint` (M7.1c) is `NULL` for every action a human
    proposes through `POST /actions` — only Nerva's own automatic proposals
    (`services.proposal_materialization.ProposalMaterializationService`) ever
    set it, to a value computed by `nerva_core.proposal_fingerprint.
    compute_proposal_fingerprint`. The partial unique index below is what
    turns "same automatic proposal identity" into "at most one persisted
    Action," atomically, even under real concurrency — a `SELECT-then-INSERT`
    check alone cannot close that race; the database constraint is the actual
    guarantee, not the selection services.proposal_materialization performs
    before relying on it.
    """

    __tablename__ = "actions"
    __table_args__ = (
        Index("ix_actions_workspace_id_status_created_at", "workspace_id", "status", "created_at"),
        actor_consistency_check("created_by_type", "created_by_user_id"),
        tenant_fk("opportunity_id", "opportunities"),
        # Multiple NULLs coexist freely (every human-proposed action); among
        # non-NULL values, unique per workspace — the same fingerprint may
        # recur in a *different* workspace without conflict, since
        # `workspace_id` is part of the index.
        Index(
            PROPOSAL_FINGERPRINT_UNIQUE_INDEX,
            "workspace_id",
            "proposal_fingerprint",
            unique=True,
            postgresql_where=text("proposal_fingerprint IS NOT NULL"),
        ),
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
    # SHA-256 hex digest, same length/type as `ConversationAnalysis.
    # input_fingerprint` / `Memory.source_fingerprint` / `BusinessSignal.
    # source_fingerprint` — see the class docstring.
    proposal_fingerprint: Mapped[str | None] = mapped_column(String(64))

    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    approved_at: Mapped[datetime | None]
    rejected_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    rejected_at: Mapped[datetime | None]
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
