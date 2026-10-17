"""AI analysis of conversations: analyze/re-analyze one conversation, and a
bounded, owner-only backfill across a workspace.

Orchestrates `AnalysisService` (analysis only), `SignalService` (deterministic
signal materialization only), and `MemoryFormationService` (the one automated
`estimated_value` fact memory, M6.3) — this module decides *when* to call
each, never *what* any of them decides on its own. No AI/business rule is
duplicated here.

Memory formation always runs last, after analysis and signal materialization
have already committed: a `DataIntegrityError` or `InvalidStateTransitionError`
from it (see `nerva.services.memory_formation`'s module docstring and
`backfill_analysis`'s own comments — these are the two documented, non-bug
failures that service can raise for reasons outside this call's control: a
pre-existing data inconsistency, or a live race with another resolution of
the same memory) must never be mistaken for an AI/signal failure, and must
never silently turn into a misleading success either. In
`analyze_conversation`, both are left uncaught, exactly like `SignalService`'s
call just above it — the request fails (500 or 409, respectively) rather than
return a 200 that omits a fact formation was supposed to produce. In
`backfill_analysis`, one conversation's memory-formation failure is isolated
the same way an `AnalysisService` failure already is: logged, counted via the
batch's existing `failed` counter, and never allowed to abort the remaining
conversations — see that route's own comments for how `considered == analyzed
+ cached + failed` stays true without double-counting.
"""

import logging
import uuid
from typing import NoReturn

from fastapi import APIRouter, Query

from nerva.api.deps import AIClientDep, SessionDep, WorkspaceCtx
from nerva.core.enums import BusinessSignalStatus, BusinessSignalType, MemberRole
from nerva.core.errors import (
    AuthorizationError,
    DataIntegrityError,
    InvalidStateTransitionError,
    RateLimitedError,
    ServiceUnavailableError,
)
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
from nerva.services.memory_formation import MemoryFormationService
from nerva.services.signals import SignalService

logger = logging.getLogger(__name__)

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
    # Left uncaught on purpose — see the module docstring. A `DataIntegrityError`
    # here must 500 the request, never return a 200 that silently omits the
    # memory this analysis was otherwise supposed to produce.
    MemoryFormationService(session, ctx).form_estimated_value_memory(conversation_id)
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
    memory_formation = MemoryFormationService(session, ctx)

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

        signal = signal_service.materialize_potential_opportunity(conversation.id)
        if (
            signal is not None
            and signal.status == BusinessSignalStatus.PENDING
            and not was_actionable_before
        ):
            signals_created += 1

        # Isolated per conversation, same as an AnalysisService failure above.
        # Two distinct, non-bug failures can reach here, both documented on
        # `MemoryFormationService.form_estimated_value_memory` and its module
        # docstring — never something retrying immediately would fix, but
        # also never a reason to abort every other conversation in this
        # batch: `DataIntegrityError` for pre-existing, inconsistent data (more
        # than one active FACT memory already sourced from this analysis), and
        # `InvalidStateTransitionError` for a *live* race — the memory this
        # analysis would supersede was just resolved by someone else (e.g. a
        # human calling `POST /memories/{id}/replace` or `/supersede` directly)
        # in the window between this service's own read and its locking
        # re-read (see `test_memory_formation.py`'s own
        # `test_a_concurrent_external_resolution_of_the_active_memory_raises_
        # invalid_state_transition`).
        # The analysis and signal this conversation already produced stay
        # committed either way; only this conversation's own outcome is
        # counted as `failed` instead of `analyzed`/`cached`, so `considered ==
        # analyzed + cached + failed` still holds with no double-counting.
        try:
            memory_formation.form_estimated_value_memory(conversation.id)
        except (DataIntegrityError, InvalidStateTransitionError):
            logger.error(
                "Memory formation failed for conversation %s in workspace %s "
                "(analysis and signal materialization already committed).",
                conversation.id,
                workspace_id,
                exc_info=True,
            )
            failed += 1
            continue

        if result.attempts > attempts_before:
            analyzed += 1
        else:
            cached += 1

    return AnalysisBackfillResult(
        considered=considered,
        analyzed=analyzed,
        cached=cached,
        failed=failed,
        signals_created=signals_created,
    )
