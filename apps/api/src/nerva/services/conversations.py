"""Reading synchronized conversations (Nerva Inbox).

Read-only over HTTP: conversations and messages are written only by
`nerva.services.integrations`' sync, never by a client request, so what Nerva shows
here always traces back to something it actually imported.
"""

import uuid
from collections.abc import Sequence

from nerva.core.errors import NotFoundError
from nerva.models import Conversation, Message
from nerva.repositories.workspace_data import ConversationRepository, MessageRepository
from nerva.services.base import ScopedService


class ConversationService(ScopedService):
    def list(self, *, limit: int, offset: int) -> Sequence[Conversation]:
        return ConversationRepository(self.session, self.ctx.workspace_id).list_recent(
            limit=limit, offset=offset
        )

    def get(self, conversation_id: uuid.UUID) -> Conversation:
        conversation = ConversationRepository(self.session, self.ctx.workspace_id).get(
            conversation_id
        )
        if conversation is None:
            raise NotFoundError("Conversation not found.")
        return conversation

    def list_messages(self, conversation_id: uuid.UUID) -> Sequence[Message]:
        self.get(conversation_id)  # 404 if it does not belong to this workspace
        return MessageRepository(self.session, self.ctx.workspace_id).list_for_conversation(
            conversation_id
        )

    def latest_message(self, conversation_id: uuid.UUID) -> Message | None:
        return MessageRepository(self.session, self.ctx.workspace_id).get_latest_for_conversation(
            conversation_id
        )
