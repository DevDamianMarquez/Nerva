"""AI analysis of a conversation: the validated, versioned result of asking an LLM
provider to classify and summarize a conversation, plus enough operational
metadata to make re-analysis idempotent and its cost observable.

This table stores only what already passed `nerva_ai.schemas.ConversationAnalysisResult`'s
validation, or a safe failure summary — never a raw provider response, a rendered
prompt, a duplicated copy of the message bodies it was computed from, or any
secret. See `nerva.nerva_ai` for the trust boundary this sits on the other side of.

One row per conversation: re-analysis overwrites this row in place rather than
appending a new one, the same way `Integration` is a single mutable row rather
than a log of every sync — the *history* of what happened is the `ActivityEvent`
trail (`conversation.analyzed` / `conversation.analysis_failed`), recorded by
whatever writes this row, not a second copy kept here.

`memory_ids_used` (M6.4 Block 4) is the audit snapshot of exactly which `Memory`
rows (Block 1/2's authoritative selection — `nerva_core.memory_context.
select_context_memories` via `MemoryContextService`) were folded into
`AnalysisInput.memory_context` when this row's *current* result was produced —
see `nerva.services.analysis._persist_success`, the only writer. Deliberately a
plain `UUID[]`, not a foreign key: a `Memory` later superseded or discarded must
never retroactively change what this row says was actually used to produce its
result (and `Memory` rows are never physically deleted at the application layer
at all — see `nerva.nerva_memory.lifecycle` — so there is no live-reference
problem to guard against either). Order matches selection order exactly; `[]`
means no relevant memory existed at the time. This column is internal audit
data, never serialized to the API (see `nerva.schemas.crm.ConversationAnalysisRead`,
which does not declare it — Pydantic's `from_attributes` only ever serializes
fields a schema explicitly lists).
"""

import uuid
from datetime import date, datetime

from sqlalchemy import BigInteger, String, Text, UniqueConstraint, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from nerva.core.enums import (
    AnalysisStatus,
    ConversationCategory,
    ConversationPriority,
    SentimentLabel,
)
from nerva.db.base import UpdatedAtMixin, WorkspaceOwnedModel, enum_type, tenant_fk


class ConversationAnalysis(WorkspaceOwnedModel, UpdatedAtMixin):
    __tablename__ = "conversation_analyses"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id"),
        # One current analysis per conversation — see module docstring.
        UniqueConstraint("workspace_id", "conversation_id"),
        tenant_fk("conversation_id", "conversations", ondelete="CASCADE"),
    )

    conversation_id: Mapped[uuid.UUID]
    status: Mapped[AnalysisStatus] = mapped_column(
        enum_type(AnalysisStatus, "analysis_status"),
        default=AnalysisStatus.PENDING,
        server_default=AnalysisStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    last_attempted_at: Mapped[datetime | None]
    # Short and human-safe — never a raw provider response or a traceback.
    # Cleared on the next successful analysis.
    last_error: Mapped[str | None] = mapped_column(Text)

    # --- Versioning: what produced the current result. `input_fingerprint`
    # (a hash of normalized conversation content + the three version stamps
    # below) is the idempotency key a future AnalysisService compares against
    # before ever calling a provider again. ------------------------------------
    schema_version: Mapped[int]
    prompt_version: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model_id: Mapped[str] = mapped_column(String(64))
    input_fingerprint: Mapped[str] = mapped_column(String(64))

    # --- Queryable result fields. NULL both before the first attempt and,
    # deliberately, whenever the model itself was not confident enough to fill
    # them in — see nerva_ai.schemas's uncertainty design; NULL here is a valid
    # analysis outcome, not just an unset default. ------------------------------
    category: Mapped[ConversationCategory | None] = mapped_column(
        enum_type(ConversationCategory, "conversation_category")
    )
    priority: Mapped[ConversationPriority | None] = mapped_column(
        enum_type(ConversationPriority, "conversation_priority")
    )
    sentiment: Mapped[SentimentLabel | None] = mapped_column(
        enum_type(SentimentLabel, "sentiment_label")
    )
    requires_response: Mapped[bool | None]
    commercial_signal: Mapped[bool | None]
    confidence: Mapped[float | None]
    estimated_value_cents: Mapped[int | None] = mapped_column(BigInteger)
    value_currency: Mapped[str | None] = mapped_column(String(3))
    value_is_explicit: Mapped[bool | None]
    deadline_at: Mapped[date | None]
    deadline_is_explicit: Mapped[bool | None]

    # --- Flexible/textual result fields ----------------------------------------
    summary: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(100))
    suggested_next_step: Mapped[str | None] = mapped_column(Text)
    mentioned_people: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")
    mentioned_companies: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default="[]")

    # --- Observability. Counts and a duration only — never the response itself. -
    input_tokens: Mapped[int | None]
    output_tokens: Mapped[int | None]
    latency_ms: Mapped[int | None]

    # --- Audit (M6.4 Block 4): see the module docstring. -------------------------
    memory_ids_used: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(Uuid()), default=list, server_default="{}"
    )

    analyzed_at: Mapped[datetime | None]  # set only when a run completes successfully
