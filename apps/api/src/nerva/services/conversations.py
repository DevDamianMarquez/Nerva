"""Reading synchronized conversations (Nerva Inbox).

Read-only over HTTP: conversations and messages are written only by
`nerva.services.integrations`' sync, never by a client request, so what Nerva shows
here always traces back to something it actually imported.
"""

import uuid
from collections.abc import Sequence

from sqlalchemy import Row

from nerva.core.errors import NotFoundError
from nerva.models import Conversation, ConversationAnalysis, Message
from nerva.repositories.workspace_data import (
    ConversationAnalysisRepository,
    ConversationRepository,
    MessageRepository,
)
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

    def get_analysis(self, conversation_id: uuid.UUID) -> ConversationAnalysis | None:
        """The current analysis for a conversation, if any — read-only, for
        embedding into `ConversationDetailRead`. Never `None` because the
        conversation itself is missing (call `get` first for that 404); `None`
        here just means nothing has been analyzed yet."""
        return ConversationAnalysisRepository(
            self.session, self.ctx.workspace_id
        ).get_for_conversation(conversation_id)

    def list_needing_analysis(
        self,
        *,
        limit: int,
        current_schema_version: int,
        current_prompt_version: str,
        current_model_id: str,
    ) -> Sequence[Conversation]:
        return ConversationRepository(self.session, self.ctx.workspace_id).list_needing_analysis(
            limit=limit,
            current_schema_version=current_schema_version,
            current_prompt_version=current_prompt_version,
            current_model_id=current_model_id,
        )

    def list_needing_attention(
        self, *, limit: int, offset: int
    ) -> Sequence[Row[tuple[Conversation, ConversationAnalysis]]]:
        return ConversationRepository(self.session, self.ctx.workspace_id).list_needing_attention(
            limit=limit, offset=offset
        )
