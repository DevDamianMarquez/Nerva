"""Business signals: read pending (or any) signals, and the two human-only
decisions on a `potential_opportunity` — confirm or dismiss.

Read-only where the domain is read-only, and a thin pass-through to
`SignalService` everywhere else: this module never decides whether a signal
should exist (that's `nerva_core.rules`, via `SignalService.materialize_potential_opportunity`
in `routes/analysis.py`) and never creates an `Opportunity` itself
(`SignalService.confirm` does).
"""

import uuid

from fastapi import APIRouter

from nerva.api.deps import PaginationDep, SessionDep, WorkspaceCtx
from nerva.core.enums import BusinessSignalStatus, BusinessSignalType
from nerva.models import BusinessSignal
from nerva.schemas.analysis import BusinessSignalRead, SignalConfirmationRead
from nerva.schemas.common import WORKSPACE_ERRORS
from nerva.schemas.crm import OpportunityRead
from nerva.services.signals import SignalService

router = APIRouter(
    prefix="/workspaces/{workspace_id}/signals",
    tags=["signals"],
    responses=WORKSPACE_ERRORS,
)


def _to_read(signal: BusinessSignal) -> BusinessSignalRead:
    """`data` is a JSONB payload (see `nerva.models.signals`); flattened here
    into typed fields rather than ever returned as a raw dict."""
    data = signal.data
    return BusinessSignalRead(
        id=signal.id,
        conversation_id=signal.conversation_id,
        analysis_id=signal.analysis_id,
        type=signal.type,
        status=signal.status,
        source_fingerprint=signal.source_fingerprint,
        title=data.get("title"),
        summary=data.get("summary"),
        estimated_value_cents=data.get("estimated_value_cents"),
        currency=data.get("currency"),
        value_is_explicit=data.get("value_is_explicit"),
        confidence=data.get("confidence"),
        confirmed_by_user_id=signal.confirmed_by_user_id,
        confirmed_at=signal.confirmed_at,
        dismissed_by_user_id=signal.dismissed_by_user_id,
        dismissed_at=signal.dismissed_at,
        created_opportunity_id=signal.created_opportunity_id,
        created_at=signal.created_at,
        updated_at=signal.updated_at,
    )


@router.get("", response_model=list[BusinessSignalRead])
def list_signals(
    workspace_id: uuid.UUID,
    ctx: WorkspaceCtx,
    session: SessionDep,
    page: PaginationDep,
    status: BusinessSignalStatus | None = None,
    signal_type: BusinessSignalType | None = None,
) -> list[BusinessSignalRead]:
    """Any workspace member may read signals — matches conversations and
    integrations, which are also member-readable. Pass `?status=pending` for
    the queue a human still needs to act on."""
    signals = SignalService(session, ctx).signals.list_filtered(
        status=status, signal_type=signal_type, limit=page.limit, offset=page.offset
    )
    return [_to_read(signal) for signal in signals]


@router.get("/{signal_id}", response_model=BusinessSignalRead)
def read_signal(
    workspace_id: uuid.UUID, signal_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> BusinessSignalRead:
    return _to_read(SignalService(session, ctx).get(signal_id))


@router.post("/{signal_id}/confirm", response_model=SignalConfirmationRead)
def confirm_signal(
    workspace_id: uuid.UUID, signal_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> SignalConfirmationRead:
    """Human-only by construction: `SignalService.confirm` always attributes
    the created Opportunity to `ctx`'s authenticated member, never to Nerva —
    there is no parameter here or in the service that could change that. A
    signal that is not `pending` (including an already-confirmed one) raises
    the project's standard `409 invalid_state_transition`."""
    service = SignalService(session, ctx)
    opportunity = service.confirm(signal_id)
    signal = service.get(signal_id)  # re-read: now `confirmed`, in the same session
    return SignalConfirmationRead(
        signal=_to_read(signal), opportunity=OpportunityRead.model_validate(opportunity)
    )


@router.post("/{signal_id}/dismiss", response_model=BusinessSignalRead)
def dismiss_signal(
    workspace_id: uuid.UUID, signal_id: uuid.UUID, ctx: WorkspaceCtx, session: SessionDep
) -> BusinessSignalRead:
    """Never creates an Opportunity. A signal that is not `pending` (including
    an already-dismissed one) raises `409 invalid_state_transition`."""
    signal = SignalService(session, ctx).dismiss(signal_id)
    return _to_read(signal)
