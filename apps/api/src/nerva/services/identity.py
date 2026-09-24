"""Users, workspaces and the workspace authorization check."""

import re
import secrets
import uuid
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import ActivityEventType, MemberRole
from nerva.core.errors import ConflictError, NotFoundError
from nerva.models import User, Workspace, WorkspaceMember
from nerva.repositories.identity import MemberRepository, UserRepository, WorkspaceRepository
from nerva.services.activity import ActivityService


class UserService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.users = UserRepository(session)

    def update_name(self, user: User, *, name: str) -> User:
        """Used by onboarding: an optional display-name change alongside creating a
        workspace. Signup already required a name, so this only overrides it."""
        user.name = name
        self.session.commit()
        return user


class WorkspaceService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.workspaces = WorkspaceRepository(session)
        self.members = MemberRepository(session)

    def get_context(self, *, user_id: uuid.UUID, workspace_id: uuid.UUID) -> WorkspaceContext:
        """The authorization gate for every workspace-scoped request.

        Returns 404 (never 403) for non-members, so that the existence of another
        tenant's workspace cannot be probed.
        """
        member = self.members.get(workspace_id, user_id)
        if member is None:
            raise NotFoundError("Workspace not found.")
        return WorkspaceContext(workspace_id=workspace_id, user_id=user_id, role=member.role)

    def create(self, *, owner: User, name: str) -> tuple[Workspace, MemberRole]:
        workspace = self.workspaces.add(Workspace(name=name, slug=self._unique_slug(name)))
        self.members.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=owner.id, role=MemberRole.OWNER)
        )
        ActivityService(self.session, workspace.id).record(
            actor=Actor.user(owner.id),
            event_type=ActivityEventType.WORKSPACE_CREATED,
            entity_type="workspace",
            entity_id=workspace.id,
            summary=f"Workspace “{workspace.name}” was created.",
        )
        try:
            self.session.commit()
        except IntegrityError as exc:  # slug taken between the check and the insert
            self.session.rollback()
            raise ConflictError("Could not create the workspace; please try again.") from exc
        return workspace, MemberRole.OWNER

    def get(self, ctx: WorkspaceContext) -> tuple[Workspace, MemberRole]:
        found = self.workspaces.get_for_member(ctx.workspace_id, ctx.user_id)
        if found is None:
            raise NotFoundError("Workspace not found.")
        return found

    def list_for_user(self, user_id: uuid.UUID) -> list[tuple[Workspace, MemberRole]]:
        return self.workspaces.list_for_user(user_id)

    def list_members(self, ctx: WorkspaceContext) -> Sequence[tuple[WorkspaceMember, User]]:
        return self.members.list_for_workspace(ctx.workspace_id)

    def _unique_slug(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:48] or "workspace"
        slug = base
        while self.workspaces.slug_exists(slug):
            slug = f"{base}-{secrets.token_hex(2)}"
        return slug
