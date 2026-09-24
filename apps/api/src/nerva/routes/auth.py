"""Signup, login, logout and the current session.

Answers "who is this user?" only; see `nerva.routes.workspaces` for "can they access
this workspace?".
"""

from fastapi import APIRouter, Request, Response, status

from nerva.api.deps import CurrentUser, SessionDep, SettingsDep
from nerva.core.cookies import clear_session_cookie, set_session_cookie
from nerva.core.security import SESSION_COOKIE_NAME
from nerva.schemas.common import ErrorResponse
from nerva.schemas.identity import (
    LoginRequest,
    MeRead,
    SignupRequest,
    UserRead,
    UserUpdate,
    WorkspaceRead,
)
from nerva.services.auth import AuthService
from nerva.services.identity import UserService, WorkspaceService

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/signup",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorResponse, "description": "Email already registered."}},
)
def signup(
    body: SignupRequest, session: SessionDep, response: Response, settings: SettingsDep
) -> UserRead:
    user, token = AuthService(session).signup(
        email=body.email, name=body.name, password=body.password
    )
    set_session_cookie(response, token, settings)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=UserRead,
    responses={401: {"model": ErrorResponse, "description": "Incorrect email or password."}},
)
def login(
    body: LoginRequest, session: SessionDep, response: Response, settings: SettingsDep
) -> UserRead:
    user, token = AuthService(session).login(email=body.email, password=body.password)
    set_session_cookie(response, token, settings)
    return UserRead.model_validate(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request, session: SessionDep, response: Response, settings: SettingsDep
) -> None:
    """Idempotent: logging out with no session, or an already-expired one, still succeeds."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        AuthService(session).logout(token)
    clear_session_cookie(response, settings)


@router.get(
    "/me",
    response_model=MeRead,
    responses={401: {"model": ErrorResponse, "description": "Not authenticated."}},
)
def read_me(user: CurrentUser, session: SessionDep) -> MeRead:
    """The current user and the workspaces they belong to.

    An empty `workspaces` list is what routes a freshly signed-up user to onboarding.
    """
    workspaces = WorkspaceService(session).list_for_user(user.id)
    return MeRead(
        user=UserRead.model_validate(user),
        workspaces=[
            WorkspaceRead(id=ws.id, name=ws.name, slug=ws.slug, created_at=ws.created_at, role=role)
            for ws, role in workspaces
        ],
    )


@router.patch("/me", response_model=UserRead)
def update_me(body: UserUpdate, user: CurrentUser, session: SessionDep) -> UserRead:
    """Update the current user. Used by onboarding's optional display-name field."""
    if body.name is not None:
        user = UserService(session).update_name(user, name=body.name)
    return UserRead.model_validate(user)
