"""Conversations and their messages. Populated by connectors (e.g. Gmail) in a later version."""

import uuid
from datetime import datetime

from sqlalchemy import Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import ConversationChannel, MessageDirection
from nerva.db.base import UpdatedAtMixin, WorkspaceOwnedModel, enum_type, tenant_fk


class Conversation(WorkspaceOwnedModel, UpdatedAtMixin):
    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        # `external_id` (e.g. a Gmail thread id) is unique per channel within a workspace.
        UniqueConstraint("workspace_id", "channel", "external_id"),
        tenant_fk("contact_id", "contacts"),
        tenant_fk("integration_id", "integrations", ondelete="SET NULL"),
    )

    # The primary counterpart of the conversation, if known.
    contact_id: Mapped[uuid.UUID | None]
    channel: Mapped[ConversationChannel] = mapped_column(
        enum_type(ConversationChannel, "conversation_channel"),
        default=ConversationChannel.EMAIL,
        server_default=ConversationChannel.EMAIL.value,
    )
    external_id: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(998))
    last_message_at: Mapped[datetime | None]
    # The connector that imported this conversation, if any (NULL for one created
    # directly in Nerva). SET NULL on delete: a disconnected integration's history
    # outlives it (see docs/ARCHITECTURE.md).
    integration_id: Mapped[uuid.UUID | None]


class Message(WorkspaceOwnedModel):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "ix_messages_workspace_id_conversation_id_sent_at",
            "workspace_id",
            "conversation_id",
            "sent_at",
        ),
        # Makes sync idempotent: re-importing the same provider message is a no-op
        # (NULLs, for messages not from an external provider, do not collide).
        UniqueConstraint("workspace_id", "external_id"),
        tenant_fk("conversation_id", "conversations", ondelete="CASCADE"),
    )

    conversation_id: Mapped[uuid.UUID]
    direction: Mapped[MessageDirection] = mapped_column(
        enum_type(MessageDirection, "message_direction")
    )
    sender_email: Mapped[str] = mapped_column(String(320))
    sender_name: Mapped[str | None] = mapped_column(String(200))
    # Comma-separated display list, not a structured/joinable relationship — enough
    # to show "To: …" without a full recipients table this MVP does not need yet.
    recipients: Mapped[str] = mapped_column(Text, default="", server_default="")
    body_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    sent_at: Mapped[datetime]
    external_id: Mapped[str | None] = mapped_column(String(255))
