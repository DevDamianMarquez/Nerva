"""Schemas for users, workspaces and members."""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import EmailStr, Field, StringConstraints, field_validator

from nerva.core.enums import MemberRole
from nerva.schemas.common import RequestModel, ResponseModel

Name = Annotated[str, StringConstraints(min_length=1, max_length=200)]
# bcrypt ignores bytes past 72; reject an over-long password instead of silently
# truncating it.
Password = Annotated[str, StringConstraints(min_length=8, max_length=72)]


class SignupRequest(RequestModel):
    email: EmailStr
    name: Name
    password: Password

    @field_validator("email")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        return value.lower()


class LoginRequest(RequestModel):
    email: EmailStr
    password: Annotated[str, StringConstraints(min_length=1, max_length=72)]

    @field_validator("email")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        return value.lower()


class UserUpdate(RequestModel):
    name: Name | None = None


class UserRead(ResponseModel):
    id: uuid.UUID
    email: str
    name: str
    created_at: datetime


class WorkspaceCreate(RequestModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]


class WorkspaceRead(ResponseModel):
    id: uuid.UUID
    name: str
    slug: str
    created_at: datetime
    # The calling user's role in this workspace.
    role: MemberRole


class MemberRead(ResponseModel):
    user_id: uuid.UUID
    email: str
    name: str
    role: MemberRole
    joined_at: datetime


class MeRead(ResponseModel):
    user: UserRead
    workspaces: list[WorkspaceRead] = Field(default_factory=list)
