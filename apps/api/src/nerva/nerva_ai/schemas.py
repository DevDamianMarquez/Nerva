"""The one contract every AI provider must produce: `ConversationAnalysisResult`.

This is the entire trust boundary. A provider's raw response is never used for
anything until it has been parsed into this shape — every field is type-, range-,
enum- or length-constrained, `extra="forbid"` rejects anything unexpected, and
there is deliberately no field that can name a database id, a workspace, or
another conversation. Matching this output to existing `Contact`/`Company` rows,
and deciding what (if anything) gets created from it, both happen later and
deterministically — never by trusting a value from here directly (see
`nerva_core.rules`, once it exists).

Uncertainty is a first-class outcome, not an error: `intent`, `sentiment`,
`estimated_value`, `deadline` and `suggested_next_step` are all optional, and the
model is expected to return `None` rather than invent a plausible-sounding value
when the conversation does not clearly support one. `category`, `priority`,
`requires_response`, `commercial_signal` and `confidence` are always required —
they are the fields deterministic rules key off, so a best-effort classification
(with `other`/low `confidence` as the honest fallback) is always expected.
"""

from datetime import date
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from nerva.core.enums import ConversationCategory, ConversationPriority, SentimentLabel

# Bumped whenever this shape changes in a way that should trigger re-analysis of
# already-analyzed conversations (a new field, a changed constraint, …). See
# `nerva.services.analysis` (added in a later block) for how this feeds the
# idempotency fingerprint.
SCHEMA_VERSION = 1

_Name = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
_ShortText = Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
_Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
_CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class MonetaryEstimate(BaseModel):
    """A monetary value the model found in the conversation. `is_explicit`
    distinguishes an amount stated outright from one the model inferred (e.g. from
    a per-seat price times a headcount mentioned elsewhere) — deterministic rules
    may choose to trust the two very differently. This must never be fabricated:
    omit `estimated_value` entirely rather than guess."""

    model_config = ConfigDict(extra="forbid")

    amount_cents: Annotated[int, Field(ge=0, le=10**12)]
    currency: _CurrencyCode
    is_explicit: bool


class DeadlineEstimate(BaseModel):
    """A date the conversation implies matters (a deadline, a requested meeting
    date, …). Deliberately a `date`, not a `datetime`: inventing a time-of-day the
    text never stated would itself be a fabrication. `is_explicit` mirrors
    `MonetaryEstimate.is_explicit`."""

    model_config = ConfigDict(extra="forbid")

    date: date
    is_explicit: bool


class ConversationAnalysisResult(BaseModel):
    """The complete, validated output of analyzing one conversation."""

    model_config = ConfigDict(extra="forbid")

    summary: Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
    # Short, free-text description of what the sender wants (e.g. "request_quote",
    # "schedule_meeting") — bounded but deliberately not a closed enum (unlike
    # `category`), and never used on its own to trigger anything.
    intent: _ShortText | None = None
    category: ConversationCategory
    priority: ConversationPriority
    sentiment: SentimentLabel | None = None
    requires_response: bool
    commercial_signal: bool
    mentioned_people: Annotated[list[_Name], Field(max_length=20)] = Field(default_factory=list)
    mentioned_companies: Annotated[list[_Name], Field(max_length=20)] = Field(default_factory=list)
    estimated_value: MonetaryEstimate | None = None
    deadline: DeadlineEstimate | None = None
    suggested_next_step: (
        Annotated[str, StringConstraints(min_length=1, max_length=300, strip_whitespace=True)]
        | None
    ) = None
    confidence: _Confidence
