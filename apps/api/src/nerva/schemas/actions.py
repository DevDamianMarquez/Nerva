"""Schemas for actions and the activity feed."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, JsonValue, StringConstraints

from nerva.core.enums import ActionStatus, ActionType, ActorType
from nerva.schemas.common import RequestModel, ResponseModel


class ActionCreate(RequestModel):
    type: ActionType
    summary: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    opportunity_id: uuid.UUID | None = None


class ActionReject(RequestModel):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2000)] | None = None


class ActionRead(ResponseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    opportunity_id: uuid.UUID | None
    type: str
    status: ActionStatus
    summary: str
    payload: dict[str, Any]
    result: dict[str, Any] | None
    created_by_type: ActorType
    created_by_user_id: uuid.UUID | None
    approved_by_user_id: uuid.UUID | None
    approved_at: datetime | None
    rejected_by_user_id: uuid.UUID | None
    rejected_at: datetime | None
    rejection_reason: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ActivityEventRead(ResponseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    actor_type: ActorType
    actor_user_id: uuid.UUID | None
    event_type: str
    entity_type: str
    entity_id: uuid.UUID
    summary: str
    data: dict[str, Any]
    created_at: datetime
