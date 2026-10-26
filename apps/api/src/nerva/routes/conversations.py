"""Nerva Inbox: reading conversations and messages synchronized by Nerva Connect.

Read-only, except for `POST .../reason` (M8.1): there is no create/update
route for conversations/messages themselves (see
`nerva.services.conversations`).
"""

import uuid
from typing import NoReturn

from fastapi import APIRouter

from nerva.api.deps import AgentReasoningClientDep, PaginationDep, SessionDep, WorkspaceCtx
from nerva.core.errors import ConflictError, RateLimitedError, ServiceUnavailableError
from nerva.models import Conversation
from nerva.nerva_ai.errors import AIAuthError, AIError, AIOutputValidationError, AIRateLimitError
from nerva.schemas.actions import ActionRead, ReasonConversationRead
from nerva.schemas.analysis import ConversationAttentionRead
from nerva.schemas.common import WORKSPACE_ERRORS, ErrorResponse
from nerva.schemas.crm import (
    PREVIEW_LENGTH,
    ConversationAnalysisRead,
    ConversationDetailRead,
    ConversationRead,
    MessageRead,
)
from nerva.services.agent_reasoning import (
    AgentReasoningService,
    AnalysisNotReadyError,
    ReasoningOutcomeStatus,
    RecipientNotResolvableError,
)
from nerva.services.conversations import ConversationService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/conversations",
    tags=["conversations"],
    responses=WORKSPACE_ERRORS,
)

REASON_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    409: {
        "model": ErrorResponse,
        "description": (
            "This conversation has no completed analysis to reason over, or no "
            "authoritative recipient could be resolved for it."
        ),
    },
    429: {"model": ErrorResponse, "description": "The AI provider is rate-limiting requests."},
    503: {
        "model": ErrorResponse,
        "description": (
            "Agent reasoning is not configured, the AI provider is unavailable, or it "
            "returned an unexpected result."
        ),
    },
}

_OUTCOME_LABEL: dict[ReasoningOutcomeStatus, str] = {
    ReasoningOutcomeStatus.NO_ACTION: "no_action",
    ReasoningOutcomeStatus.POLICY_REJECTED: "policy_rejected",
    ReasoningOutcomeStatus.ACTION_CREATED: "action_created",
    ReasoningOutcomeStatus.ACTION_DUPLICATE: "action_duplicate",
}


def _raise_for_reasoning_ai_failure(exc: AIError) -> NoReturn:
    """Translates the agent-reasoning provider boundary's typed failures into
    the project's standard error shape — the same mapping
    `routes.analysis._raise_for_ai_failure` already applies for the analysis
    boundary, reused here rather than duplicated since both are the same
    `nerva_ai.errors.AIError` hierarchy. Never re-raises anything beyond a
    fixed, safe message: no raw provider output ever reaches these."""
    if isinstance(exc, AIAuthError):
        raise ServiceUnavailableError(
            "Agent reasoning is not available right now.", details={"code": "ai_not_configured"}
        ) from exc
    if isinstance(exc, AIRateLimitError):
        raise RateLimitedError(
            "The AI provider is rate-limiting requests. Try again shortly.",
            details={"code": "rate_limited"},
        ) from exc
    if isinstance(exc, AIOutputValidationError):
        raise ServiceUnavailableError(
            "The AI provider returned an unexpected result. Try again shortly.",
            details={"code": "invalid_ai_output"},
        ) from exc
    raise ServiceUnavailableError(
        "Could not reach the AI provider. Try again shortly.", details={"code": "ai_unavailable"}
    ) from exc


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


@router.post(
    "/{conversation_id}/reason",
    response_model=ReasonConversationRead,
    responses=REASON_ERROR_RESPONSES,
)
def reason_about_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    reasoning_client: AgentReasoningClientDep,
) -> ReasonConversationRead:
    """Reasons about one conversation and, at most, proposes one `SEND_EMAIL`
    action for a human to later approve and execute — see
    `nerva.services.agent_reasoning.AgentReasoningService.reason_and_propose`,
    which this route calls unchanged and does not reimplement.

    Any workspace member may call this (not owner-only): like
    `POST .../analyze`, reasoning touches no external credentials and
    approves/executes nothing by itself — those remain gated exactly as
    before (`POST .../approve`/`.../reject` stay member-level,
    `POST .../execute` stays owner-only; see `routes.actions`).

    `NO_ACTION` and `POLICY_REJECTED` are normal, successfully-handled
    domain outcomes — a reasoner declining, or a policy safely rejecting
    unsafe/invalid model output, are not failures of this request, so both
    return `200` with `action: null`, never an HTTP error (see
    `nerva_core.proposal_policy.decide_proposal`'s own docstring). A
    `ACTION_DUPLICATE` outcome (an identical automatic proposal already
    existed) also returns `200`, with the pre-existing `Action` — calling
    this endpoint twice over unchanged state never creates two `Action`s
    (enforced at the database level by `Action`'s partial unique index on
    `proposal_fingerprint`), though it does call the provider again; see
    `AgentReasoningService`'s own module docstring for why that cost is
    accepted rather than papered over with request-level deduplication here.
    """
    service = AgentReasoningService(session, ctx, reasoning_client=reasoning_client)
    try:
        result = service.reason_and_propose(conversation_id)
    except (AnalysisNotReadyError, RecipientNotResolvableError) as exc:
        code = (
            "analysis_not_ready"
            if isinstance(exc, AnalysisNotReadyError)
            else "recipient_not_resolvable"
        )
        raise ConflictError(str(exc), details={"code": code}) from exc
    except AIError as exc:
        _raise_for_reasoning_ai_failure(exc)

    return ReasonConversationRead(
        outcome=_OUTCOME_LABEL[result.status],
        action=ActionRead.model_validate(result.action) if result.action is not None else None,
        detail=result.policy_detail,
    )
