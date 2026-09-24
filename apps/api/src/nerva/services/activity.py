"""Recording and reading the audit trail."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from nerva.core.context import Actor
from nerva.core.enums import ActivityEventType
from nerva.models import ActivityEvent
from nerva.repositories.workspace_data import ActivityEventRepository


class ActivityService:
    """Records events in the caller's transaction (it flushes but never commits), so an
    event is stored if and only if the operation it describes is stored."""

    def __init__(self, session: Session, workspace_id: uuid.UUID) -> None:
        self.repo = ActivityEventRepository(session, workspace_id)

    def record(
        self,
        *,
        actor: Actor,
        event_type: ActivityEventType,
        entity_type: str,
        entity_id: uuid.UUID,
        summary: str,
        data: dict[str, Any] | None = None,
    ) -> ActivityEvent:
        return self.repo.add(
            ActivityEvent(
                actor_type=actor.type,
                actor_user_id=actor.user_id,
                event_type=event_type.value,
                entity_type=entity_type,
                entity_id=entity_id,
                summary=summary,
                data=data or {},
            )
        )

    def list(
        self,
        *,
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[ActivityEvent]:
        return self.repo.list_filtered(
            entity_type=entity_type, entity_id=entity_id, limit=limit, offset=offset
        )
