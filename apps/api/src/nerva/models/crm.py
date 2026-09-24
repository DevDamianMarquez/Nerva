"""Companies, contacts and opportunities."""

import uuid

from sqlalchemy import BigInteger, CheckConstraint, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import OpportunityStage
from nerva.db.base import (
    CreatedByMixin,
    UpdatedAtMixin,
    WorkspaceOwnedModel,
    actor_consistency_check,
    enum_type,
    tenant_fk,
)


class Company(WorkspaceOwnedModel, UpdatedAtMixin):
    __tablename__ = "companies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index("ix_companies_workspace_id_name", "workspace_id", "name"),
    )

    name: Mapped[str] = mapped_column(String(200))
    domain: Mapped[str | None] = mapped_column(String(253))


class Contact(WorkspaceOwnedModel, UpdatedAtMixin):
    __tablename__ = "contacts"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        UniqueConstraint("workspace_id", "email"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
        tenant_fk("company_id", "companies"),
    )

    company_id: Mapped[uuid.UUID | None]
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str | None] = mapped_column(String(200))


class Opportunity(WorkspaceOwnedModel, UpdatedAtMixin, CreatedByMixin):
    __tablename__ = "opportunities"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        Index("ix_opportunities_workspace_id_stage", "workspace_id", "stage"),
        CheckConstraint(
            "estimated_value_cents IS NULL OR estimated_value_cents >= 0",
            name="estimated_value_non_negative",
        ),
        actor_consistency_check("created_by_type", "created_by_user_id"),
        tenant_fk("contact_id", "contacts"),
        tenant_fk("company_id", "companies"),
        tenant_fk("conversation_id", "conversations"),
    )

    contact_id: Mapped[uuid.UUID | None]
    company_id: Mapped[uuid.UUID | None]
    # The conversation this opportunity was identified in, if any.
    conversation_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str | None] = mapped_column(Text)
    stage: Mapped[OpportunityStage] = mapped_column(
        enum_type(OpportunityStage, "opportunity_stage"),
        default=OpportunityStage.NEW,
        server_default=OpportunityStage.NEW.value,
    )
    estimated_value_cents: Mapped[int | None] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="USD", server_default="USD")
