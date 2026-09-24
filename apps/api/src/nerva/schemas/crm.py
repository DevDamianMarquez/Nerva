"""Schemas for companies, contacts and opportunities.

Note that no request schema has a `workspace_id` field: the workspace always comes
from the URL, after membership has been verified.
"""

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import EmailStr, Field, StringConstraints, field_validator

from nerva.core.enums import OpportunityStage
from nerva.schemas.common import RequestModel, ResponseModel

ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class CompanyCreate(RequestModel):
    name: ShortText
    domain: Annotated[str, StringConstraints(min_length=1, max_length=253)] | None = None

    @field_validator("domain")
    @classmethod
    def _lowercase_domain(cls, value: str | None) -> str | None:
        return value.lower() if value else value


class CompanyRead(ResponseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    name: str
    domain: str | None
    created_at: datetime


class ContactCreate(RequestModel):
    email: EmailStr
    name: ShortText | None = None
    title: ShortText | None = None
    company_id: uuid.UUID | None = None

    @field_validator("email")
    @classmethod
    def _lowercase_email(cls, value: str) -> str:
        return value.lower()


class ContactRead(ResponseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    company_id: uuid.UUID | None
    email: str
    name: str | None
    title: str | None
    created_at: datetime


class OpportunityCreate(RequestModel):
    title: ShortText
    summary: Annotated[str, StringConstraints(max_length=5000)] | None = None
    stage: OpportunityStage = OpportunityStage.NEW
    estimated_value_cents: Annotated[int, Field(ge=0, le=10**12)] | None = None
    currency: CurrencyCode = "USD"
    contact_id: uuid.UUID | None = None
    company_id: uuid.UUID | None = None
    conversation_id: uuid.UUID | None = None


class OpportunityRead(ResponseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    contact_id: uuid.UUID | None
    company_id: uuid.UUID | None
    conversation_id: uuid.UUID | None
    title: str
    summary: str | None
    stage: OpportunityStage
    estimated_value_cents: int | None
    currency: str
    created_at: datetime
    updated_at: datetime
