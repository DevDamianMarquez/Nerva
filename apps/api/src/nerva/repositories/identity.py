"""Repositories for users, workspaces and membership.

These are the only repositories that are not bound to a single workspace: a user and a
workspace exist above the tenant boundary, and membership is what defines it.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from nerva.core.enums import MemberRole
from nerva.core.security import SESSION_TTL
from nerva.db.base import utcnow
from nerva.models import User, UserSession, Workspace, WorkspaceMember


class UserRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, user_id: uuid.UUID) -> User | None:
        return self.session.get(User, user_id)

    def get_by_email(self, email: str) -> User | None:
        return self.session.scalars(select(User).where(User.email == email)).one_or_none()

    def add(self, user: User) -> User:
        self.session.add(user)
        self.session.flush()
        return user


class WorkspaceRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, workspace: Workspace) -> Workspace:
        self.session.add(workspace)
        self.session.flush()
        return workspace

    def slug_exists(self, slug: str) -> bool:
        return (
            self.session.scalars(select(Workspace.id).where(Workspace.slug == slug)).first()
            is not None
        )

    def get_for_member(
        self, workspace_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[Workspace, MemberRole] | None:
        row = self.session.execute(
            select(Workspace, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(Workspace.id == workspace_id, WorkspaceMember.user_id == user_id)
        ).one_or_none()
        return (row[0], row[1]) if row else None

    def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Workspace, MemberRole]]:
        rows = self.session.execute(
            select(Workspace, WorkspaceMember.role)
            .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
            .where(WorkspaceMember.user_id == user_id)
            .order_by(Workspace.created_at, Workspace.id)
        ).all()
        return [(row[0], row[1]) for row in rows]


class MemberRepository:
    """Membership lookups. `get` is the authorization check behind every workspace route."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, workspace_id: uuid.UUID, user_id: uuid.UUID) -> WorkspaceMember | None:
        return self.session.get(WorkspaceMember, (workspace_id, user_id))

    def add(self, member: WorkspaceMember) -> WorkspaceMember:
        self.session.add(member)
        self.session.flush()
        return member

    def list_for_workspace(self, workspace_id: uuid.UUID) -> Sequence[tuple[WorkspaceMember, User]]:
        rows = self.session.execute(
            select(WorkspaceMember, User)
            .join(User, User.id == WorkspaceMember.user_id)
            .where(WorkspaceMember.workspace_id == workspace_id)
            .order_by(WorkspaceMember.created_at, User.id)
        ).all()
        return [(row[0], row[1]) for row in rows]


class SessionRepository:
    """Login sessions. `get_active` is the check behind every authenticated request."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def add(self, *, user_id: uuid.UUID, token_hash: str) -> UserSession:
        record = UserSession(
            user_id=user_id, token_hash=token_hash, expires_at=utcnow() + SESSION_TTL
        )
        self.session.add(record)
        self.session.flush()
        return record

    def get_active(self, token_hash: str) -> UserSession | None:
        return self.session.scalars(
            select(UserSession).where(
                UserSession.token_hash == token_hash,
                UserSession.revoked_at.is_(None),
                UserSession.expires_at > utcnow(),
            )
        ).one_or_none()

    def revoke(self, token_hash: str) -> None:
        record = self.session.scalars(
            select(UserSession).where(UserSession.token_hash == token_hash)
        ).one_or_none()
        if record is not None:
            record.revoked_at = utcnow()
            self.session.flush()
