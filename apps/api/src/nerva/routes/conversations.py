"""Nerva Inbox: reading conversations and messages synchronized by Nerva Connect.

Read-only: there is no create/update route here on purpose (see
`nerva.services.conversations`).
"""

import uuid

from fastapi import APIRouter

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.models import Conversation
from nerva.schemas.analysis import ConversationAttentionRead
from nerva.schemas.common import WORKSPACE_ERRORS
from nerva.schemas.crm import (
    PREVIEW_LENGTH,
    ConversationAnalysisRead,
    ConversationDetailRead,
    ConversationRead,
    MessageRead,
)
from nerva.services.conversations import ConversationService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/conversations",
    tags=["conversations"],
    responses=WORKSPACE_ERRORS,
)


def _to_read(service: ConversationService, conversation: Conversation) -> ConversationRead:
    latest = service.latest_message(conversation.id)
    preview = latest.body_text[:PREVIEW_LENGTH].strip() if latest else None
    return ConversationRead(
        id=conversation.id,
        workspace_id=conversation.workspace_id,
        contact_id=conversation.contact_id,
        integration_id=conversation.integration_id,
        channel=conversation.channel,
        subject=conversation.subject,
        last_message_at=conversation.last_message_at,
        preview=preview,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


@router.get("", response_model=list[ConversationRead])
def list_conversations(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep, page: PaginationDep
) -> list[ConversationRead]:
    """Most recently active conversations first."""
    service = ConversationService(session, ctx)
    conversations = service.list(limit=page.limit, offset=page.offset)
    return [_to_read(service, conversation) for conversation in conversations]


@router.get("/attention", response_model=list[ConversationAttentionRead])
def list_conversations_needing_attention(
    workspace_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep, page: PaginationDep
) -> list[ConversationAttentionRead]:
    """Conversations whose current completed analysis flagged them as needing
    a reply, or as high/urgent priority — for the future Today/Inbox "needs
    attention" view. Read-only and deterministic: no new signal type is
    created for this (see `nerva_core.rules`'s module docstring). Registered
    before `/{conversation_id}` so the literal path `attention` is never
    parsed as a conversation id."""
    service = ConversationService(session, ctx)
    rows = service.list_needing_attention(limit=page.limit, offset=page.offset)
    return [
        ConversationAttentionRead(
            conversation_id=conversation.id,
            subject=conversation.subject,
            last_message_at=conversation.last_message_at,
            category=analysis.category,
            priority=analysis.priority,
            requires_response=analysis.requires_response,
            summary=analysis.summary,
            analyzed_at=analysis.analyzed_at,
        )
        for conversation, analysis in rows
    ]


@router.get("/{conversation_id}", response_model=ConversationDetailRead)
def read_conversation(
    workspace_id: uuid.UUID, conversation_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> ConversationDetailRead:
    """Messages are returned in chronological order. `analysis` is `None`
    until the conversation has been analyzed at least once (see
    `POST .../analyze` in `routes/analysis.py`)."""
    service = ConversationService(session, ctx)
    conversation = service.get(conversation_id)
    messages = service.list_messages(conversation_id)
    analysis = service.get_analysis(conversation_id)
    analysis_read = (
        ConversationAnalysisRead.model_validate(analysis) if analysis is not None else None
    )
    return ConversationDetailRead(
        conversation=_to_read(service, conversation),
        messages=[MessageRead.model_validate(message) for message in messages],
        analysis=analysis_read,
    )
