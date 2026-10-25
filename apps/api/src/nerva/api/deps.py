"""FastAPI dependencies: database session, current user, authorized workspace."""

import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session

from nerva.core.config import Settings, get_settings
from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionType
from nerva.core.errors import AuthenticationError
from nerva.core.security import SESSION_COOKIE_NAME
from nerva.db.session import get_session
from nerva.models import User
from nerva.nerva_actions.engine import ActionExecutorRegistry
from nerva.nerva_actions.opportunity_stage_executor import UpdateOpportunityStageExecutor
from nerva.nerva_ai.agent_client import AgentReasoningClient
from nerva.nerva_ai.client import AIAnalysisClient
from nerva.nerva_ai.providers.groq_agent_reasoning import GroqAgentReasoningProvider
from nerva.nerva_ai.providers.openai import OpenAIProvider
from nerva.nerva_ai.providers.openai_agent_reasoning import OpenAIAgentReasoningProvider
from nerva.nerva_ai.unconfigured import UnconfiguredAgentReasoningClient, UnconfiguredAIClient
from nerva.nerva_connect.google.gmail import GmailClient
from nerva.nerva_connect.google.oauth import GoogleOAuthClient
from nerva.nerva_connect.google.send_executor import GmailSendExecutor
from nerva.nerva_connect.google.sender import GmailAPISender, GmailSender
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


def get_gmail_sender() -> GmailSender:
    """The real `GmailAPISender`. Overridden in tests (`tests/conftest.py`) to a
    `tests.fakes.google.FakeGmailSender`, the same pattern `get_google_oauth_client`/
    `get_gmail_client` already use — so an HTTP test of the execute endpoint never
    makes a real network call or sends a real email."""
    return GmailAPISender()


GmailSenderDep = Annotated[GmailSender, Depends(get_gmail_sender)]


def get_action_executors(
    session: SessionDep,
    ctx: WorkspaceCtx,
    oauth_client: GoogleOAuthDep,
    sender: GmailSenderDep,
) -> ActionExecutorRegistry:
    """The one place a production `ActionExecutorRegistry` is built: composes
    `nerva_actions`' domain-only `UpdateOpportunityStageExecutor` with
    `nerva_connect`'s Google-backed `GmailSendExecutor`. Lives here, in the API
    dependency layer, rather than in either package — `nerva_actions` must never
    import Google or an HTTP transport module (enforced by
    `test_nerva_actions_package_never_imports_google_or_http_transport_modules`), and
    `nerva_connect` has no reason to know `update_opportunity_stage` exists.
    `create_task` has no registered executor yet (see `nerva_actions.payloads`); an
    action of that type still fails safely with `ActionExecutorUnavailableError`,
    exactly as it does in every test today.
    """
    return {
        ActionType.UPDATE_OPPORTUNITY_STAGE: UpdateOpportunityStageExecutor(session, ctx),
        ActionType.SEND_EMAIL: GmailSendExecutor(
            session, ctx, oauth_client=oauth_client, sender=sender
        ),
    }


ActionExecutorsDep = Annotated[ActionExecutorRegistry, Depends(get_action_executors)]


def get_ai_client(settings: SettingsDep) -> AIAnalysisClient:
    """Returns the real OpenAI-backed client once `OPENAI_API_KEY` is set;
    otherwise every call fails safely and explicitly rather than faking a
    result — see `nerva.nerva_ai.unconfigured`."""
    if settings.ai_provider_configured:
        return OpenAIProvider(settings)
    return UnconfiguredAIClient()


AIClientDep = Annotated[AIAnalysisClient, Depends(get_ai_client)]


def get_agent_reasoning_client(settings: SettingsDep) -> AgentReasoningClient:
    """Returns the real agent-reasoning client for whichever provider
    `Settings.nerva_agent_reasoning_provider` names, once that provider's own
    credentials (and, for Groq, model) are set; otherwise every call fails
    safely and explicitly — see `nerva.nerva_ai.unconfigured`.

    Selection is explicit and fail-closed, never a fallback between the two:
    an unreachable or misconfigured Groq never silently tries OpenAI, and
    vice versa — switching providers mid-flight would be a reproducibility,
    audit, and cost change, not something this dependency is allowed to do
    on its own (see the M7.1g design audit). A provider name outside
    `{"openai", "groq"}` can never reach this function at all: `Settings`'
    own `Literal` field rejects it at application startup, before any
    request.

    `"openai"` mirrors the exact M7.1e behavior unchanged — same
    `ai_provider_configured` flag, same fallback-bearing `agent_reasoning_model`
    — so an environment with `nerva_agent_reasoning_provider` left at its
    default sees no behavior change from adding Groq support at all. `"groq"`
    requires *both* `GROQ_API_KEY` and `NERVA_GROQ_AGENT_REASONING_MODEL`
    (`Settings.groq_provider_configured`) — there is no fallback model to
    guess, unlike OpenAI's.

    Not called by any production code path yet (M7.1e/M7.1g introduce no
    route and no automatic trigger — see `nerva.services.agent_reasoning`'s
    own module docstring): this dependency exists so a future trigger can
    depend on `AgentReasoningClient` the same way every other service already
    depends on its own Protocol, without first having to invent this wiring
    under time pressure. Tests override it exactly like `get_ai_client`
    (`tests/conftest.py`)."""
    if settings.nerva_agent_reasoning_provider == "groq":
        if settings.groq_provider_configured:
            return GroqAgentReasoningProvider(settings)
        return UnconfiguredAgentReasoningClient()
    if settings.ai_provider_configured:
        return OpenAIAgentReasoningProvider(settings)
    return UnconfiguredAgentReasoningClient()


AgentReasoningClientDep = Annotated[AgentReasoningClient, Depends(get_agent_reasoning_client)]


@dataclass
class Pagination:
    limit: int = Query(50, ge=1, le=100, description="Maximum number of items to return.")
    offset: int = Query(0, ge=0, description="Number of items to skip.")


PaginationDep = Annotated[Pagination, Depends()]
