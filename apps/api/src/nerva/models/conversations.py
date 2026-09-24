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


class Message(WorkspaceOwnedModel):
    __tablename__ = "messages"
    __table_args__ = (
        Index(
            "ix_messages_workspace_id_conversation_id_sent_at",
            "workspace_id",
            "conversation_id",
            "sent_at",
        ),
        tenant_fk("conversation_id", "conversations", ondelete="CASCADE"),
    )

    conversation_id: Mapped[uuid.UUID]
    direction: Mapped[MessageDirection] = mapped_column(
        enum_type(MessageDirection, "message_direction")
    )
    sender_email: Mapped[str] = mapped_column(String(320))
    sender_name: Mapped[str | None] = mapped_column(String(200))
    body_text: Mapped[str] = mapped_column(Text, default="", server_default="")
    sent_at: Mapped[datetime]
    external_id: Mapped[str | None] = mapped_column(String(255))
