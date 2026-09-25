"""AI analysis of conversations: analyze/re-analyze one conversation, and a
bounded, owner-only backfill across a workspace.

Orchestrates `AnalysisService` (analysis only) and `SignalService`
(deterministic signal materialization only) — this module decides *when* to
call each, never *what* either of them decides on its own. No AI/business rule
is duplicated here.
"""

import uuid
from typing import NoReturn

from fastapi import APIRouter, Query

from nerva.api.deps import AIClientDep, SessionDep, WorkspaceCtx
from nerva.core.enums import BusinessSignalStatus, BusinessSignalType, MemberRole
from nerva.core.errors import AuthorizationError, RateLimitedError, ServiceUnavailableError
from nerva.nerva_ai.prompts import PROMPT_VERSION
from nerva.nerva_ai.schemas import SCHEMA_VERSION
from nerva.schemas.analysis import AnalysisBackfillResult
from nerva.schemas.common import WORKSPACE_ERRORS, ErrorResponse
from nerva.schemas.crm import ConversationAnalysisRead
from nerva.services.analysis import (
    AnalysisAuthError,
    AnalysisOutputError,
    AnalysisProviderError,
    AnalysisRateLimitError,
    AnalysisService,
)
from nerva.services.conversations import ConversationService
from nerva.services.signals import SignalService

router = APIRouter(
    prefix="/workspaces/{workspace_id}",
    tags=["analysis"],
    responses=WORKSPACE_ERRORS,
)

# A hard cap, not just a default: Milestone 4 backfill is explicit/manual, and
# a single request must stay cheap and predictable regardless of workspace
# size — see docs on `nerva.services.analysis`'s own per-conversation bounds
# for the same reasoning applied one level up.
DEFAULT_BACKFILL_BATCH_SIZE = 20
MAX_BACKFILL_BATCH_SIZE = 20

AI_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    429: {"model": ErrorResponse, "description": "The AI provider is rate-limiting requests."},
    503: {
        "model": ErrorResponse,
        "description": (
            "The AI provider is unavailable, not configured, or returned an unexpected result."
        ),
    },
}
OWNER_ONLY_RESPONSES: dict[int | str, dict[str, object]] = {
    403: {"model": ErrorResponse, "description": "Only the workspace owner can do this."}
}


def _raise_for_ai_failure(exc: AnalysisAuthError | AnalysisProviderError) -> NoReturn:
    """Translates `AnalysisService`'s typed failures into the project's
    standard error shape. Never re-raises anything beyond what
    `AnalysisService._safe_error_message` already made safe — no raw provider
    output or prompt content ever reaches these messages."""
    if isinstance(exc, AnalysisAuthError):
        raise ServiceUnavailableError(
            "AI analysis is not available right now.", details={"code": "ai_not_configured"}
        ) from exc
    if isinstance(exc, AnalysisRateLimitError):
        raise RateLimitedError(
            "The AI provider is rate-limiting requests. Try again shortly.",
            details={"code": "rate_limited"},
        ) from exc
    if isinstance(exc, AnalysisOutputError):
        raise ServiceUnavailableError(
            "The AI provider returned an unexpected result. Try again shortly.",
            details={"code": "invalid_ai_output"},
        ) from exc
    raise ServiceUnavailableError(
        "Could not reach the AI provider. Try again shortly.", details={"code": "ai_unavailable"}
    ) from exc


@router.post(
    "/conversations/{conversation_id}/analyze",
    response_model=ConversationAnalysisRead,
    responses=AI_ERROR_RESPONSES,
)
def analyze_conversation(
    workspace_id: uuid.UUID,
    conversation_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    ai_client: AIClientDep,
    force: bool = Query(False, description="Re-run analysis even if a cached result is current."),
) -> ConversationAnalysisRead:
    """Any workspace member may trigger this — unlike Gmail's connect/sync,
    analysis touches no external credentials. Idempotency (a cache hit costs
    zero provider calls) lives entirely in `AnalysisService`; this route never
    re-decides that."""
    analysis_service = AnalysisService(session, ctx, ai_client=ai_client)
    try:
        analysis = analysis_service.analyze(conversation_id, force=force)
    except (AnalysisAuthError, AnalysisProviderError) as exc:
        _raise_for_ai_failure(exc)

    SignalService(session, ctx).materialize_potential_opportunity(conversation_id)
    return ConversationAnalysisRead.model_validate(analysis)


@router.post(
    "/analysis/backfill", response_model=AnalysisBackfillResult, responses=OWNER_ONLY_RESPONSES
)
def backfill_analysis(
    workspace_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    ai_client: AIClientDep,
    batch_size: int = Query(DEFAULT_BACKFILL_BATCH_SIZE, ge=1, le=MAX_BACKFILL_BATCH_SIZE),
) -> AnalysisBackfillResult:
    """Owner-only: this spends external AI resources. Explicit and manual —
    nothing schedules this automatically. Selection
    (`ConversationService.list_needing_analysis`) is a cheap, deliberately
    imperfect pre-filter; the authoritative idempotency guarantee stays
    `AnalysisService`'s fingerprint cache, so a conversation this pre-filter
    includes but whose content has not actually changed still costs zero
    provider calls. One conversation's failure never aborts the batch."""
    if ctx.role != MemberRole.OWNER:
        raise AuthorizationError("Only the workspace owner can run an analysis backfill.")

    conversations = ConversationService(session, ctx)
    analysis_service = AnalysisService(session, ctx, ai_client=ai_client)
    signal_service = SignalService(session, ctx)

    considered = analyzed = cached = failed = signals_created = 0

    for conversation in conversations.list_needing_analysis(
        limit=batch_size,
        current_schema_version=SCHEMA_VERSION,
        current_prompt_version=PROMPT_VERSION,
        current_model_id=ai_client.model_id,
    ):
        considered += 1
        existing_analysis = conversations.get_analysis(conversation.id)
        attempts_before = existing_analysis.attempts if existing_analysis is not None else 0
        existing_signal = signal_service.signals.get_for_conversation_and_type(
            conversation.id, BusinessSignalType.POTENTIAL_OPPORTUNITY
        )
        was_actionable_before = (
            existing_signal is not None and existing_signal.status == BusinessSignalStatus.PENDING
        )

        try:
            result = analysis_service.analyze(conversation.id)
        except (AnalysisAuthError, AnalysisProviderError):
            failed += 1
            continue

        if result.attempts > attempts_before:
            analyzed += 1
        else:
            cached += 1

        signal = signal_service.materialize_potential_opportunity(conversation.id)
        if (
            signal is not None
            and signal.status == BusinessSignalStatus.PENDING
            and not was_actionable_before
        ):
            signals_created += 1

    return AnalysisBackfillResult(
        considered=considered,
        analyzed=analyzed,
        cached=cached,
        failed=failed,
        signals_created=signals_created,
    )
