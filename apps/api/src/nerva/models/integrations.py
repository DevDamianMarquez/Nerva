"""Nerva Connect: external account connections (Gmail first) and the OAuth
handshake that creates them.

`Integration` deliberately does not store an access token: Google access tokens are
short-lived (~1 hour) and cheap to mint on demand from the refresh token, so persisting
one would only add attack surface without benefit. Only the refresh token is stored,
encrypted (see `nerva.core.crypto`).
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import IntegrationProvider, IntegrationStatus
from nerva.db.base import (
    Base,
    CreatedAtMixin,
    IdMixin,
    UpdatedAtMixin,
    WorkspaceOwnedModel,
    enum_type,
)


class Integration(WorkspaceOwnedModel, UpdatedAtMixin):
    __tablename__ = "integrations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        # Reconnecting the same external account updates this row rather than
        # creating a duplicate (see IntegrationService.connect_google).
        UniqueConstraint("workspace_id", "provider", "external_account_id"),
    )

    provider: Mapped[IntegrationProvider] = mapped_column(
        enum_type(IntegrationProvider, "integration_provider")
    )
    status: Mapped[IntegrationStatus] = mapped_column(
        enum_type(IntegrationStatus, "integration_status"),
        default=IntegrationStatus.CONNECTED,
        server_default=IntegrationStatus.CONNECTED.value,
    )
    # Google's stable "sub" claim: unlike the email address, it survives renames and
    # alias changes, so it — not the email — is what identifies "the same account".
    external_account_id: Mapped[str] = mapped_column(String(255))
    # Display only; never used as an identity key.
    external_account_email: Mapped[str] = mapped_column(String(320))
    scopes: Mapped[str] = mapped_column(Text, default="", server_default="")
    # Encrypted with Fernet (nerva.core.crypto); NULL once disconnected. Never
    # serialized by any response schema.
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text)
    last_synced_at: Mapped[datetime | None]
    # Set alongside status=error; cleared on the next successful sync. A short,
    # human-safe message — never a raw provider payload or stack trace.
    last_error: Mapped[str | None] = mapped_column(Text)


class OAuthState(Base, IdMixin, CreatedAtMixin):
    """A short-lived, single-use, server-bound value for one OAuth connect attempt.

    Exists above the tenant boundary (like `sessions`): it references a workspace and
    user directly rather than being workspace-owned data. The raw value lives only in
    the redirect URL/browser; only its SHA-256 hash is stored, the same pattern as
    session tokens.
    """

    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    provider: Mapped[IntegrationProvider] = mapped_column(
        enum_type(IntegrationProvider, "integration_provider")
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None]
