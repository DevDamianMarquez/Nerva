"""Nerva Connect: connecting, listing, disconnecting and synchronizing Gmail.

Authorization split: any workspace member may list integrations and read what has been
synchronized (`ConversationService`, in `nerva.services.conversations`); only the
workspace **owner** may connect, disconnect or trigger a sync (`_require_owner`). This
is a deliberate MVP simplification — real per-role permission enforcement is future
work (see docs/ARCHITECTURE.md) — but connecting a third-party account with mailbox
access is sensitive enough to restrict now rather than open it to every member by
default.

`IntegrationService` and `GmailSyncService` never speak Gmail's wire format directly;
that boundary is `nerva.nerva_connect.google`. This file's job is orchestration:
authorize, call the connector, map the result onto `Conversation` / `Message` /
`Integration`, and record what happened.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.crypto import decrypt_secret, encrypt_secret
from nerva.core.enums import (
    ActivityEventType,
    ConversationChannel,
    IntegrationProvider,
    IntegrationStatus,
    MemberRole,
    MessageDirection,
)
from nerva.core.errors import AuthorizationError, ConflictError, NotFoundError
from nerva.core.security import generate_session_token, hash_session_token
from nerva.db.base import utcnow
from nerva.models import Contact, Conversation, Integration, Message, User
from nerva.nerva_connect.google.errors import GoogleApiError, GoogleAuthError
from nerva.nerva_connect.google.gmail import GmailClient
from nerva.nerva_connect.google.mapper import NormalizedMessage, parse_gmail_message
from nerva.nerva_connect.google.oauth import GoogleOAuthClient
from nerva.repositories.identity import OAuthStateRepository
from nerva.repositories.workspace_data import (
    ContactRepository,
    ConversationRepository,
    IntegrationRepository,
    MessageRepository,
)
from nerva.services.activity import ActivityService
from nerva.services.identity import WorkspaceService

# Sync bounds: recent mail only, and never more than a fixed number of messages in one
# run, so a very active mailbox cannot be accidentally downloaded in full.
SYNC_WINDOW_DAYS = 30
MAX_MESSAGES_PER_SYNC = 200


class IntegrationAuthError(Exception):
    """Google rejected the stored credentials. The integration has already been
    marked `needs_reauth` and the failure recorded before this is raised."""


class IntegrationSyncError(Exception):
    """A sync attempt failed for a reason other than authorization. The integration
    has already been marked `error` and the failure recorded before this is raised."""


@dataclass(frozen=True, slots=True)
class SyncResult:
    threads_seen: int
    conversations_created: int
    conversations_updated: int
    messages_created: int


@dataclass(frozen=True, slots=True)
class ConnectCallbackResult:
    ok: bool
    workspace_id: uuid.UUID | None = None
    reason: str | None = None


def _require_owner(ctx: WorkspaceContext) -> None:
    if ctx.role != MemberRole.OWNER:
        raise AuthorizationError("Only the workspace owner can manage integrations.")


class IntegrationService:
    """Listing, connecting and disconnecting. See `GmailSyncService` for sync."""

    def __init__(
        self, session: Session, ctx: WorkspaceContext, *, oauth_client: GoogleOAuthClient
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.oauth_client = oauth_client
        self.repo = IntegrationRepository(session, ctx.workspace_id)
        self.activity = ActivityService(session, ctx.workspace_id)

    def list(self) -> Sequence[Integration]:
        return self.repo.list_all()

    def get(self, integration_id: uuid.UUID) -> Integration:
        integration = self.repo.get(integration_id)
        if integration is None:
            raise NotFoundError("Integration not found.")
        return integration

    def begin_google_connect(self) -> str:
        """Creates a short-lived, single-use state bound to the current user and
        workspace, and returns the Google consent URL the browser should visit."""
        _require_owner(self.ctx)
        token = generate_session_token()
        OAuthStateRepository(self.session).add(
            state_hash=hash_session_token(token),
            provider=IntegrationProvider.GOOGLE,
            workspace_id=self.ctx.workspace_id,
            user_id=self.ctx.user_id,
        )
        self.session.commit()
        return self.oauth_client.build_authorization_url(token)

    def disconnect(self, integration_id: uuid.UUID) -> Integration:
        """Revokes the credential and marks the integration disconnected. Imported
        conversations and messages are kept — disconnecting removes access, not
        history."""
        _require_owner(self.ctx)
        integration = self.get(integration_id)
        if integration.encrypted_refresh_token:
            self.oauth_client.revoke(decrypt_secret(integration.encrypted_refresh_token))
        integration.status = IntegrationStatus.DISCONNECTED
        integration.encrypted_refresh_token = None
        self.activity.record(
            actor=self.ctx.actor,
            event_type=ActivityEventType.INTEGRATION_DISCONNECTED,
            entity_type="integration",
            entity_id=integration.id,
            summary=f"Gmail ({integration.external_account_email}) was disconnected.",
        )
        self.session.commit()
        return integration


def complete_google_connect(
    session: Session,
    *,
    user: User,
    code: str,
    raw_state: str,
    oauth_client: GoogleOAuthClient,
) -> ConnectCallbackResult:
    """Validates the OAuth `state`, exchanges `code` for tokens, and upserts the
    `Integration`. Called from the callback route, which has no `WorkspaceContext` of
    its own — the authorized workspace comes only from the validated state, and
    membership is re-checked here via the real authorization gate
    (`WorkspaceService.get_context`), never trusted from the browser.
    """
    state_record = OAuthStateRepository(session).consume(hash_session_token(raw_state))
    if state_record is None:
        return ConnectCallbackResult(False, reason="invalid_state")
    if state_record.user_id != user.id:
        return ConnectCallbackResult(False, reason="state_user_mismatch")

    try:
        ctx = WorkspaceService(session).get_context(
            user_id=user.id, workspace_id=state_record.workspace_id
        )
    except NotFoundError:
        return ConnectCallbackResult(False, reason="not_a_member")

    workspace_id = ctx.workspace_id
    try:
        tokens = oauth_client.exchange_code(code)
        if not tokens.id_token:
            return ConnectCallbackResult(
                False, workspace_id=workspace_id, reason="missing_identity"
            )
        identity = oauth_client.verify_id_token(tokens.id_token)
    except (GoogleAuthError, GoogleApiError):
        return ConnectCallbackResult(False, workspace_id=workspace_id, reason="google_auth_failed")

    if not tokens.refresh_token:
        # Should not happen: `prompt=consent` forces one. Fail loudly rather than
        # storing an integration Nerva could never actually sync.
        return ConnectCallbackResult(
            False, workspace_id=workspace_id, reason="missing_refresh_token"
        )

    repo = IntegrationRepository(session, ctx.workspace_id)
    integration = repo.get_by_external_account(IntegrationProvider.GOOGLE, identity.subject)
    encrypted_refresh_token = encrypt_secret(tokens.refresh_token)
    if integration is None:
        integration = repo.add(
            Integration(
                provider=IntegrationProvider.GOOGLE,
                external_account_id=identity.subject,
                external_account_email=identity.email,
                scopes=tokens.scope,
                encrypted_refresh_token=encrypted_refresh_token,
                status=IntegrationStatus.CONNECTED,
            )
        )
    else:
        # Reconnecting the same account updates this row instead of duplicating it.
        integration.external_account_email = identity.email
        integration.scopes = tokens.scope
        integration.encrypted_refresh_token = encrypted_refresh_token
        integration.status = IntegrationStatus.CONNECTED
        integration.last_error = None

    ActivityService(session, ctx.workspace_id).record(
        actor=ctx.actor,
        event_type=ActivityEventType.INTEGRATION_CONNECTED,
        entity_type="integration",
        entity_id=integration.id,
        summary=f"Gmail ({integration.external_account_email}) was connected.",
    )
    session.commit()
    return ConnectCallbackResult(True, workspace_id=ctx.workspace_id)


class GoogleCredentialRefresher:
    """Mints a short-lived Gmail access token from a workspace's stored, encrypted
    refresh token — the one place that happens. Shared by `GmailSyncService`
    (mailbox import) and `nerva.nerva_connect.google.send_executor.GmailSendExecutor`
    (Gmail send, Milestone 5 Block 5), so a second caller of the Gmail API never
    re-implements decrypt/refresh/re-encrypt or duplicates the `needs_reauth` vs
    `error` classification below — see docs/ARCHITECTURE.md §5.5.
    """

    def __init__(
        self, session: Session, ctx: WorkspaceContext, *, oauth_client: GoogleOAuthClient
    ) -> None:
        self.session = session
        self.oauth_client = oauth_client
        self.activity = ActivityService(session, ctx.workspace_id)

    def refresh_access_token(self, integration: Integration) -> str:
        """Raises `IntegrationAuthError` if Google rejected the stored grant (the
        integration is marked `needs_reauth` first, committed immediately) or
        `IntegrationSyncError` for any other failure (marked `error`, likewise
        committed) — exactly the same split every caller already handles from
        `GmailSyncService.sync`."""
        if not integration.encrypted_refresh_token:
            raise IntegrationAuthError("Integration has no stored refresh token.")
        refresh_token = decrypt_secret(integration.encrypted_refresh_token)
        try:
            tokens = self.oauth_client.refresh(refresh_token)
        except GoogleAuthError as exc:
            integration.status = IntegrationStatus.NEEDS_REAUTH
            integration.last_error = "Google rejected the stored credentials."
            self.activity.record(
                actor=Actor.nerva(),
                event_type=ActivityEventType.INTEGRATION_NEEDS_REAUTH,
                entity_type="integration",
                entity_id=integration.id,
                summary=f"Gmail ({integration.external_account_email}) needs to be reconnected.",
            )
            self.session.commit()
            raise IntegrationAuthError(str(exc)) from exc
        except GoogleApiError as exc:
            integration.status = IntegrationStatus.ERROR
            integration.last_error = "Could not reach Google to refresh access."
            self.session.commit()
            raise IntegrationSyncError(str(exc)) from exc

        if tokens.refresh_token:  # Google does not always issue a new one
            integration.encrypted_refresh_token = encrypt_secret(tokens.refresh_token)
        return tokens.access_token


class GmailSyncService:
    def __init__(
        self,
        session: Session,
        ctx: WorkspaceContext,
        *,
        oauth_client: GoogleOAuthClient,
        gmail_client: GmailClient,
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.gmail_client = gmail_client
        self.integrations = IntegrationRepository(session, ctx.workspace_id)
        self.conversations = ConversationRepository(session, ctx.workspace_id)
        self.messages = MessageRepository(session, ctx.workspace_id)
        self.contacts = ContactRepository(session, ctx.workspace_id)
        self.activity = ActivityService(session, ctx.workspace_id)
        self.credentials = GoogleCredentialRefresher(session, ctx, oauth_client=oauth_client)

    def sync(self, integration_id: uuid.UUID) -> SyncResult:
        _require_owner(self.ctx)
        integration = self.integrations.get(integration_id)
        if integration is None:
            raise NotFoundError("Integration not found.")
        disconnected = integration.status == IntegrationStatus.DISCONNECTED
        if disconnected or not integration.encrypted_refresh_token:
            raise ConflictError("This integration is disconnected; reconnect it first.")

        access_token = self.credentials.refresh_access_token(integration)
        message_ids = self._list_recent_message_ids(integration, access_token)

        conversations_created = 0
        conversations_updated = 0
        messages_created = 0
        threads_seen: set[str] = set()

        for message_id in message_ids:
            if self.messages.get_by_external_id(message_id) is not None:
                continue  # already synced: makes repeated syncs idempotent
            try:
                raw = self.gmail_client.get_message(access_token, message_id)
            except GoogleApiError:
                continue  # skip one unreadable message rather than failing the sync

            normalized = parse_gmail_message(raw, account_email=integration.external_account_email)
            threads_seen.add(normalized.thread_external_id)
            created = self._upsert_conversation(integration, normalized)
            conversations_created += created
            conversations_updated += 0 if created else 1

            self.messages.add(
                Message(
                    conversation_id=self._conversation_id(normalized.thread_external_id),
                    direction=normalized.direction,
                    sender_email=normalized.sender_email,
                    sender_name=normalized.sender_name,
                    recipients=normalized.recipients,
                    body_text=normalized.body_text,
                    sent_at=normalized.sent_at,
                    external_id=normalized.external_id,
                )
            )
            messages_created += 1

        integration.status = IntegrationStatus.CONNECTED
        integration.last_error = None
        integration.last_synced_at = utcnow()
        self.activity.record(
            actor=Actor.nerva(),
            event_type=ActivityEventType.INTEGRATION_SYNCED,
            entity_type="integration",
            entity_id=integration.id,
            summary=f"Gmail ({integration.external_account_email}) was synchronized.",
            data={
                "messages_created": messages_created,
                "conversations_created": conversations_created,
                "conversations_updated": conversations_updated,
            },
        )
        self.session.commit()
        return SyncResult(
            threads_seen=len(threads_seen),
            conversations_created=conversations_created,
            conversations_updated=conversations_updated,
            messages_created=messages_created,
        )

    def _list_recent_message_ids(self, integration: Integration, access_token: str) -> list[str]:
        after = date.today() - timedelta(days=SYNC_WINDOW_DAYS)
        try:
            return self.gmail_client.list_recent_message_ids(
                access_token, after=after, max_results=MAX_MESSAGES_PER_SYNC
            )
        except GoogleApiError as exc:
            integration.status = IntegrationStatus.ERROR
            integration.last_error = "Could not list recent Gmail messages."
            self.session.commit()
            raise IntegrationSyncError(str(exc)) from exc

    def _upsert_conversation(self, integration: Integration, normalized: NormalizedMessage) -> int:
        """Returns 1 if a new `Conversation` was created, 0 if an existing one was
        found (and refreshed)."""
        conversation = self.conversations.get_by_external_id(
            ConversationChannel.EMAIL, normalized.thread_external_id
        )
        counterpart_email = self._counterpart_email(integration, normalized)
        contact_id = self._find_or_create_contact(counterpart_email) if counterpart_email else None

        if conversation is None:
            self.conversations.add(
                Conversation(
                    channel=ConversationChannel.EMAIL,
                    external_id=normalized.thread_external_id,
                    subject=normalized.subject,
                    contact_id=contact_id,
                    integration_id=integration.id,
                    last_message_at=normalized.sent_at,
                )
            )
            return 1

        last_message_at = conversation.last_message_at
        is_newer = last_message_at is None or normalized.sent_at > last_message_at
        if is_newer:
            conversation.last_message_at = normalized.sent_at
        if conversation.subject is None:
            conversation.subject = normalized.subject
        if conversation.contact_id is None:
            conversation.contact_id = contact_id
        return 0

    def _conversation_id(self, thread_external_id: str) -> uuid.UUID:
        conversation = self.conversations.get_by_external_id(
            ConversationChannel.EMAIL, thread_external_id
        )
        assert conversation is not None  # just upserted by _upsert_conversation
        return conversation.id

    @staticmethod
    def _counterpart_email(integration: Integration, normalized: NormalizedMessage) -> str | None:
        account_email = integration.external_account_email.lower()
        if normalized.direction == MessageDirection.INBOUND:
            return normalized.sender_email or None
        for email in (part.strip() for part in normalized.recipients.split(",")):
            if email and email != account_email:
                return email
        return None

    def _find_or_create_contact(self, email: str) -> uuid.UUID:
        email = email.lower()
        existing = self.contacts.get_by_email(email)
        if existing is not None:
            return existing.id
        return self.contacts.add(Contact(email=email)).id
