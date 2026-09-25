"""Nerva Connect: Google/Gmail integrations.

Two routers: `router` is workspace-scoped (list, connect, sync, disconnect); `callback_router`
is not, since Google's redirect names no workspace — the authorized workspace comes only
from the OAuth `state` it echoes back (see `nerva.services.integrations.complete_google_connect`).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import RedirectResponse

from nerva.api.deps import (
    CurrentUser,
    GmailClientDep,
    GoogleOAuthDep,
    SessionDep,
    SettingsDep,
    WorkspaceCtx,
)
from nerva.core.config import Settings
from nerva.core.errors import ConflictError, ServiceUnavailableError
from nerva.nerva_connect.google.errors import GoogleApiError, GoogleAuthError
from nerva.schemas.common import WORKSPACE_ERRORS, ErrorResponse
from nerva.schemas.integrations import GoogleConnectResponse, IntegrationRead, SyncResultRead
from nerva.services.integrations import (
    GmailSyncService,
    IntegrationAuthError,
    IntegrationService,
    IntegrationSyncError,
    complete_google_connect,
)

router = APIRouter(
    prefix="/workspaces/{workspace_id}/integrations",
    tags=["integrations"],
    responses=WORKSPACE_ERRORS,
)
callback_router = APIRouter(prefix="/integrations", tags=["integrations"])

FORBIDDEN: dict[int | str, dict[str, Any]] = {
    403: {"model": ErrorResponse, "description": "Only the workspace owner can do this."}
}


@router.get("", response_model=list[IntegrationRead])
def list_integrations(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep, oauth_client: GoogleOAuthDep
) -> list[IntegrationRead]:
    """Any workspace member may view what is connected."""
    return [
        IntegrationRead.model_validate(integration)
        for integration in IntegrationService(session, ctx, oauth_client=oauth_client).list()
    ]


@router.post(
    "/google/connect",
    response_model=GoogleConnectResponse,
    responses={
        **FORBIDDEN,
        503: {
            "model": ErrorResponse,
            "description": "Google OAuth is not configured on this server.",
        },
    },
)
def connect_google(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep, oauth_client: GoogleOAuthDep
) -> GoogleConnectResponse:
    """Owner-only: starts the OAuth flow and returns the URL to send the browser to."""
    try:
        url = IntegrationService(session, ctx, oauth_client=oauth_client).begin_google_connect()
    except GoogleApiError as exc:
        raise ServiceUnavailableError(
            "Gmail is not configured on this server yet.", details={"code": "google_not_configured"}
        ) from exc
    return GoogleConnectResponse(authorization_url=url)


@router.post(
    "/{integration_id}/sync",
    response_model=SyncResultRead,
    responses={
        **FORBIDDEN,
        409: {"model": ErrorResponse, "description": "The integration needs to be reconnected."},
    },
)
def sync_integration(
    workspace_id: uuid.UUID,
    integration_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    oauth_client: GoogleOAuthDep,
    gmail_client: GmailClientDep,
) -> SyncResultRead:
    """Owner-only. Fetches at most the last 30 days of mail, capped at 200 messages
    per run (see `nerva.services.integrations`), and never claims success if Google
    could not be reached or the credential was rejected."""
    service = GmailSyncService(session, ctx, oauth_client=oauth_client, gmail_client=gmail_client)
    try:
        result = service.sync(integration_id)
    except IntegrationAuthError as exc:
        raise ConflictError(
            "Gmail needs to be reconnected before syncing again.", details={"code": "needs_reauth"}
        ) from exc
    except IntegrationSyncError as exc:
        raise ConflictError(
            "Could not reach Gmail. Try again shortly.", details={"code": "sync_failed"}
        ) from exc
    return SyncResultRead(
        threads_seen=result.threads_seen,
        conversations_created=result.conversations_created,
        conversations_updated=result.conversations_updated,
        messages_created=result.messages_created,
    )


@router.post("/{integration_id}/disconnect", response_model=IntegrationRead, responses=FORBIDDEN)
def disconnect_integration(
    workspace_id: uuid.UUID,
    integration_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    oauth_client: GoogleOAuthDep,
) -> IntegrationRead:
    """Owner-only. Revokes the credential; imported conversations and messages are
    kept (see `IntegrationService.disconnect`)."""
    service = IntegrationService(session, ctx, oauth_client=oauth_client)
    return IntegrationRead.model_validate(service.disconnect(integration_id))


@callback_router.get("/google/callback", include_in_schema=False)
def google_callback(
    user: CurrentUser,
    session: SessionDep,
    oauth_client: GoogleOAuthDep,
    settings: SettingsDep,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> RedirectResponse:
    """Google redirects the browser here after consent. Always redirects onward to
    the frontend Settings page — never a raw JSON error — since this response is a
    mid-navigation browser redirect, not an API call."""
    if error or not code or not state:
        return _settings_redirect(settings, status="error", reason=error or "missing_parameters")

    try:
        result = complete_google_connect(
            session, user=user, code=code, raw_state=state, oauth_client=oauth_client
        )
    except (GoogleAuthError, GoogleApiError):
        return _settings_redirect(settings, status="error", reason="google_unavailable")

    if not result.ok:
        return _settings_redirect(settings, status="error", reason=result.reason or "unknown")
    return _settings_redirect(settings, status="connected")


def _settings_redirect(
    settings: Settings, *, status: str, reason: str | None = None
) -> RedirectResponse:
    url = f"{settings.frontend_base_url}/settings?integration={status}"
    if reason:
        url += f"&reason={reason}"
    return RedirectResponse(url, status_code=302)
