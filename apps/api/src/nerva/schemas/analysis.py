"""Schemas for AI conversation analysis, business signals, and analysis
backfill — the public contract over `nerva.services.analysis` and
`nerva.services.signals`.

Never a raw prompt or raw provider response field: both services already stop
that content from ever reaching a `ConversationAnalysis`/`BusinessSignal` row
(see `nerva_ai` and `nerva_core.rules`), so these schemas only need to avoid
re-adding what was never stored — see each model's own docstring for what
"safe" means here.
"""

import uuid
from datetime import datetime

from nerva.core.enums import (
    BusinessSignalStatus,
    BusinessSignalType,
    ConversationCategory,
    ConversationPriority,
)
from nerva.schemas.common import ResponseModel
from nerva.schemas.crm import OpportunityRead

# `ConversationAnalysisRead` lives in `nerva.schemas.crm` alongside
# `ConversationDetailRead` (which embeds it) — keeping the dependency between
# this module and `crm` one-directional (this module -> crm, never the
# reverse) avoids a circular import between the two.


class AnalysisBackfillResult(ResponseModel):
    considered: int
    analyzed: int
    cached: int
    failed: int
    signals_created: int


class BusinessSignalRead(ResponseModel):
    """Only the fields a human deciding whether to confirm or dismiss needs —
    the signal's `data` JSONB column is flattened here, never returned as a
    raw dict (see `nerva.models.signals` for what that column can hold)."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    analysis_id: uuid.UUID
    type: BusinessSignalType
    status: BusinessSignalStatus
    source_fingerprint: str
    title: str | None
    summary: str | None
    estimated_value_cents: int | None
    currency: str | None
    value_is_explicit: bool | None
    confidence: float | None
    confirmed_by_user_id: uuid.UUID | None
    confirmed_at: datetime | None
    dismissed_by_user_id: uuid.UUID | None
    dismissed_at: datetime | None
    created_opportunity_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class SignalConfirmationRead(ResponseModel):
    """The result of confirming a signal: both the now-`confirmed` signal and
    the exactly-one `Opportunity` it produced — `nerva.services.signals.SignalService.confirm`
    creates the Opportunity, never this route."""

    signal: BusinessSignalRead
    opportunity: OpportunityRead


class ConversationAttentionRead(ResponseModel):
    """One row of the "needs attention" query (`GET .../conversations/attention`):
    a conversation whose completed analysis flagged it as needing a reply or
    as high/urgent priority. Read-only and deterministic — no new signal type,
    see `nerva.repositories.workspace_data.ConversationRepository.list_needing_attention`."""

    conversation_id: uuid.UUID
    subject: str | None
    last_message_at: datetime | None
    category: ConversationCategory | None
    priority: ConversationPriority | None
    requires_response: bool | None
    summary: str | None
    analyzed_at: datetime | None
