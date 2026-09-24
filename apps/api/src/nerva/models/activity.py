"""Append-only audit trail of important operations."""

import uuid
from typing import Any

from sqlalchemy import ForeignKey, Index, String, event
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, Mapper, mapped_column

from nerva.core.enums import ActorType
from nerva.core.errors import ImmutableRecordError
from nerva.db.base import WorkspaceOwnedModel, actor_consistency_check, enum_type


class ActivityEvent(WorkspaceOwnedModel):
    """An immutable record that something happened.

    `entity_type` / `entity_id` deliberately have no foreign key: the trail must
    outlive the entities it describes. Rows are only ever inserted; the ORM refuses
    updates and deletes (see the listeners below).
    """

    __tablename__ = "activity_events"
    __table_args__ = (
        Index("ix_activity_events_workspace_id_created_at", "workspace_id", "created_at"),
        Index(
            "ix_activity_events_workspace_id_entity_type_entity_id",
            "workspace_id",
            "entity_type",
            "entity_id",
        ),
        actor_consistency_check("actor_type", "actor_user_id"),
    )

    actor_type: Mapped[ActorType] = mapped_column(enum_type(ActorType, "actor_type"))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    # Free text, e.g. "action.approved". See `ActivityEventType`.
    event_type: Mapped[str] = mapped_column(String(64))
    entity_type: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[uuid.UUID]
    # Human-readable one-liner so the feed can render without joining other tables.
    summary: Mapped[str] = mapped_column(String(500))
    data: Mapped[dict[str, Any]] = mapped_column(default=dict, server_default="{}")


@event.listens_for(ActivityEvent, "before_update")
def _reject_update(
    mapper: Mapper[ActivityEvent], connection: Connection, target: ActivityEvent
) -> None:
    raise ImmutableRecordError("Activity events are immutable and cannot be updated.")


@event.listens_for(ActivityEvent, "before_delete")
def _reject_delete(
    mapper: Mapper[ActivityEvent], connection: Connection, target: ActivityEvent
) -> None:
    raise ImmutableRecordError("Activity events are immutable and cannot be deleted.")
