"""Workspaces and their members."""

import uuid

from fastapi import APIRouter, status

from nerva.api.deps import CurrentUser, SessionDep, WorkspaceCtx
from nerva.core.enums import MemberRole
from nerva.models import Workspace
from nerva.schemas.common import WORKSPACE_ERRORS
from nerva.schemas.identity import MemberRead, WorkspaceCreate, WorkspaceRead
from nerva.services.identity import WorkspaceService

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _to_read(workspace: Workspace, role: MemberRole) -> WorkspaceRead:
    return WorkspaceRead(
        id=workspace.id,
        name=workspace.name,
        slug=workspace.slug,
        created_at=workspace.created_at,
        role=role,
    )


@router.post("", response_model=WorkspaceRead, status_code=status.HTTP_201_CREATED)
def create_workspace(
    body: WorkspaceCreate, user: CurrentUser, session: SessionDep
) -> WorkspaceRead:
    """Create a workspace. The caller becomes its owner."""
    workspace, role = WorkspaceService(session).create(owner=user, name=body.name)
    return _to_read(workspace, role)


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces(user: CurrentUser, session: SessionDep) -> list[WorkspaceRead]:
    """Workspaces the caller belongs to."""
    return [_to_read(ws, role) for ws, role in WorkspaceService(session).list_for_user(user.id)]


@router.get("/{workspace_id}", response_model=WorkspaceRead, responses=WORKSPACE_ERRORS)
def read_workspace(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> WorkspaceRead:
    workspace, role = WorkspaceService(session).get(ctx)
    return _to_read(workspace, role)


@router.get("/{workspace_id}/members", response_model=list[MemberRead], responses=WORKSPACE_ERRORS)
def list_members(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> list[MemberRead]:
    return [
        MemberRead(
            user_id=user.id,
            email=user.email,
            name=user.name,
            role=member.role,
            joined_at=member.created_at,
        )
        for member, user in WorkspaceService(session).list_members(ctx)
    ]
