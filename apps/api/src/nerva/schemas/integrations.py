"""Schemas for Nerva Connect integrations.

`IntegrationRead` is deliberately narrow: no access token, no refresh token (encrypted
or not), no raw Google response. Only what the Settings page needs to render a card.
"""

import uuid
from datetime import datetime

from nerva.core.enums import IntegrationProvider, IntegrationStatus
from nerva.schemas.common import ResponseModel


class IntegrationRead(ResponseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    provider: IntegrationProvider
    status: IntegrationStatus
    external_account_email: str
    last_synced_at: datetime | None
    last_error: str | None
    created_at: datetime


class GoogleConnectResponse(ResponseModel):
    """Where to send the browser next. The backend never redirects the API caller
    itself, since this is invoked by a `fetch`, not a top-level navigation."""

    authorization_url: str


class SyncResultRead(ResponseModel):
    threads_seen: int
    conversations_created: int
    conversations_updated: int
    messages_created: int
