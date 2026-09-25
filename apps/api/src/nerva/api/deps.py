"""FastAPI dependencies: database session, current user, authorized workspace."""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from nerva.core.config import Settings, get_settings
from nerva.core.context import WorkspaceContext
from nerva.core.errors import AuthenticationError
from nerva.core.security import SESSION_COOKIE_NAME
from nerva.db.session import get_session
from nerva.models import User
from nerva.nerva_ai.client import AIAnalysisClient
from nerva.nerva_ai.providers.openai import OpenAIProvider
from nerva.nerva_ai.unconfigured import UnconfiguredAIClient
from nerva.nerva_connect.google.gmail import GmailClient
from nerva.nerva_connect.google.oauth import GoogleOAuthClient
from nerva.services.auth import AuthService
from nerva.services.identity import WorkspaceService

SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def get_current_user(session: SessionDep, request: Request) -> User:
    """Identify the caller from the session cookie.

    The identity comes only from a server-verified session; a `user_id` the browser
    might supply anywhere else (a header, a body field) is never trusted. Everything
    downstream (membership checks, workspace scoping) is unchanged by this: it only
    answers "who is this user?", never "what can they access?".
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise AuthenticationError("Authentication required.")
    user = AuthService(session).authenticate(token)
    if user is None:
        raise AuthenticationError("Session is invalid or has expired.")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def get_workspace_context(
    workspace_id: uuid.UUID, user: CurrentUser, session: SessionDep
) -> WorkspaceContext:
    """Authorize access to the workspace named in the URL.

    This is the single entry point for tenant access: it verifies the current user is
    a member of `workspace_id` (404 otherwise) and returns the only object services and
    repositories accept as proof of authorization.
    """
    return WorkspaceService(session).get_context(user_id=user.id, workspace_id=workspace_id)


WorkspaceCtx = Annotated[WorkspaceContext, Depends(get_workspace_context)]


def get_google_oauth_client(settings: SettingsDep) -> GoogleOAuthClient:
    return GoogleOAuthClient(settings)


GoogleOAuthDep = Annotated[GoogleOAuthClient, Depends(get_google_oauth_client)]


def get_gmail_client() -> GmailClient:
    return GmailClient()


GmailClientDep = Annotated[GmailClient, Depends(get_gmail_client)]


def get_ai_client(settings: SettingsDep) -> AIAnalysisClient:
    """Returns the real OpenAI-backed client once `OPENAI_API_KEY` is set;
    otherwise every call fails safely and explicitly rather than faking a
    result — see `nerva.nerva_ai.unconfigured`."""
    if settings.ai_provider_configured:
        return OpenAIProvider(settings)
    return UnconfiguredAIClient()


AIClientDep = Annotated[AIAnalysisClient, Depends(get_ai_client)]


@dataclass
class Pagination:
    limit: int = Query(50, ge=1, le=100, description="Maximum number of items to return.")
    offset: int = Query(0, ge=0, description="Number of items to skip.")


PaginationDep = Annotated[Pagination, Depends()]
