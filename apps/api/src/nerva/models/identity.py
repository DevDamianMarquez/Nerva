"""Users, workspaces (tenants) and membership."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import MemberRole
from nerva.db.base import Base, CreatedAtMixin, IdMixin, UpdatedAtMixin, enum_type


class User(Base, IdMixin, CreatedAtMixin, UpdatedAtMixin):
    __tablename__ = "users"
    __table_args__ = (CheckConstraint("email = lower(email)", name="email_lowercase"),)

    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # A bcrypt hash. Never a plaintext password, and never returned in a response schema.
    password_hash: Mapped[str] = mapped_column(String(255))


class UserSession(Base, IdMixin, CreatedAtMixin):
    """A logged-in browser session.

    The cookie holds the raw token; only its SHA-256 hash is stored here, so leaking the
    database cannot be used to forge a login. Logging out (or expiry) is `revoked_at` /
    `expires_at`, not a row deletion, so a session's history is not silently lost.
    """

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column()
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)


class Workspace(Base, IdMixin, CreatedAtMixin, UpdatedAtMixin):
    """A tenant. All business data belongs to exactly one workspace."""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(63), unique=True)


class WorkspaceMember(Base, CreatedAtMixin):
    """Grants a user access to a workspace. This table is the authorization source of truth."""

    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[MemberRole] = mapped_column(
        enum_type(MemberRole, "member_role"),
        default=MemberRole.MEMBER,
        server_default=MemberRole.MEMBER.value,
    )
