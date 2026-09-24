"""Who is doing something, and in which workspace.

`WorkspaceContext` is the only way services and repositories learn which workspace
they operate on. It is produced exclusively by `WorkspaceService.get_context`, after
verifying that the authenticated user is a member of the workspace. A `workspace_id`
supplied in a request body is never used.
"""

import uuid
from dataclasses import dataclass

from nerva.core.enums import ActorType, MemberRole


@dataclass(frozen=True, slots=True)
class Actor:
    """The originator of a change: a human user, or Nerva acting on its own."""

    type: ActorType
    user_id: uuid.UUID | None = None

    @classmethod
    def user(cls, user_id: uuid.UUID) -> "Actor":
        return cls(ActorType.USER, user_id)

    @classmethod
    def nerva(cls) -> "Actor":
        return cls(ActorType.NERVA, None)


@dataclass(frozen=True, slots=True)
class WorkspaceContext:
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    role: MemberRole

    @property
    def actor(self) -> Actor:
        return Actor.user(self.user_id)
