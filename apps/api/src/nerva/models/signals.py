"""Business signals: deterministic, inert conclusions derived from a completed
`ConversationAnalysis`, queued for a human to explicitly confirm or dismiss.

Never created directly by the LLM: `nerva_core.rules` is the only code allowed
to decide one should exist, and `nerva.services.signals` is the only code
allowed to persist or resolve one. See both for why — this table only stores
the outcome of that already-deterministic decision.

`type` is a closed enum (only `potential_opportunity` in Milestone 4) so a
future signal kind is a migration, not a new table; `data` carries whatever
that kind's payload is — the columns here stay the same shape regardless of
how many kinds exist, the same way `Action.payload` already works for the
several kinds of `Action`. Only validated/derived structured data belongs in
`data` — never a raw email body or a raw provider response.

One row per (conversation, type), ever: re-evaluation updates this row in
place rather than appending a new one, the same single-mutable-row design
`ConversationAnalysis` already uses — see `nerva_core.rules.decide_materialization`
for exactly when that happens.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import BusinessSignalStatus, BusinessSignalType
from nerva.db.base import UpdatedAtMixin, WorkspaceOwnedModel, enum_type, tenant_fk


class BusinessSignal(WorkspaceOwnedModel, UpdatedAtMixin):
    __tablename__ = "business_signals"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "conversation_id", "type"),
        tenant_fk("conversation_id", "conversations", ondelete="CASCADE"),
        tenant_fk("analysis_id", "conversation_analyses", ondelete="CASCADE"),
        tenant_fk("created_opportunity_id", "opportunities", ondelete="SET NULL"),
        CheckConstraint(
            "(status = 'confirmed') = "
            "(confirmed_by_user_id IS NOT NULL AND confirmed_at IS NOT NULL "
            "AND created_opportunity_id IS NOT NULL)",
            name="confirmed_signal_is_fully_resolved",
        ),
        CheckConstraint(
            "(status = 'dismissed') = "
            "(dismissed_by_user_id IS NOT NULL AND dismissed_at IS NOT NULL)",
            name="dismissed_signal_is_fully_resolved",
        ),
    )

    conversation_id: Mapped[uuid.UUID]
    # The `ConversationAnalysis` row this signal currently reflects — a join
    # convenience; which *content* actually produced it is `source_fingerprint`
    # below, since that row is itself mutated in place by later re-analysis.
    analysis_id: Mapped[uuid.UUID]
    type: Mapped[BusinessSignalType] = mapped_column(
        enum_type(BusinessSignalType, "business_signal_type")
    )
    status: Mapped[BusinessSignalStatus] = mapped_column(
        enum_type(BusinessSignalStatus, "business_signal_status"),
        default=BusinessSignalStatus.PENDING,
        server_default=BusinessSignalStatus.PENDING.value,
    )
    # Copied from `ConversationAnalysis.input_fingerprint` at the moment this
    # signal's content was last (re)computed — decoupled from that row's
    # current value on purpose, so a later re-analysis cannot retroactively
    # change what fingerprint *this* signal was actually derived from.
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")

    confirmed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    confirmed_at: Mapped[datetime | None]
    dismissed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    dismissed_at: Mapped[datetime | None]
    # Set only once confirmed: the one Opportunity this signal produced.
    created_opportunity_id: Mapped[uuid.UUID | None]
