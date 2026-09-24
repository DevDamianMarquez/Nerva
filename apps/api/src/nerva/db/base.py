"""Declarative base, mixins and helpers shared by every model."""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    MetaData,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from nerva.core.enums import ActorType

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSONB,
    }


def enum_type[E: StrEnum](enum_cls: type[E], name: str) -> Enum:
    """A VARCHAR column restricted by a CHECK constraint to the enum's values."""
    return Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=32,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


class IdMixin:
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)


class CreatedAtMixin:
    # Python-side default so rows created in one transaction still get distinct,
    # ordered timestamps (PostgreSQL's now() is constant within a transaction).
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=func.now())


class UpdatedAtMixin:
    updated_at: Mapped[datetime] = mapped_column(
        default=utcnow, onupdate=utcnow, server_default=func.now()
    )


class WorkspaceOwnedModel(Base, IdMixin, CreatedAtMixin):
    """Base for every table whose rows belong to exactly one workspace (tenant)."""

    __abstract__ = True

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))


def tenant_fk(
    column: str, target_table: str, *, ondelete: str | None = None
) -> ForeignKeyConstraint:
    """Composite foreign key `(workspace_id, <column>) -> <target_table>(workspace_id, id)`.

    Guarantees at the database level that a row can only reference rows of its own
    workspace. The target table must declare `UniqueConstraint("workspace_id", "id")`.
    The reference is optional when `<column>` is nullable (MATCH SIMPLE semantics).
    """
    return ForeignKeyConstraint(
        ["workspace_id", column],
        [f"{target_table}.workspace_id", f"{target_table}.id"],
        ondelete=ondelete,
    )


class CreatedByMixin:
    """Who created a row: a user, or Nerva (`created_by_user_id` is then NULL)."""

    created_by_type: Mapped[ActorType] = mapped_column(enum_type(ActorType, "actor_type"))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))


def actor_consistency_check(type_column: str, user_column: str) -> CheckConstraint:
    return CheckConstraint(
        f"({type_column} = 'user') = ({user_column} IS NOT NULL)",
        name=f"{type_column}_matches_user",
    )
