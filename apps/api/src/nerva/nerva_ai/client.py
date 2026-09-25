"""The provider-agnostic boundary the rest of Nerva depends on.

Nothing outside `nerva_ai.providers` may import a vendor SDK. Services depend only
on `AIAnalysisClient`; tests substitute `tests.fakes.ai.FakeAIClient` the same way
Google OAuth/Gmail are faked (see `nerva.api.deps` for how those are wired as
FastAPI dependencies — the real provider added in a later block follows the same
pattern).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from nerva.core.enums import MessageDirection
from nerva.nerva_ai.schemas import ConversationAnalysisResult


@dataclass(frozen=True, slots=True)
class AnalysisMessage:
    """One message's already-safe-to-send content. `sender_label` is a display
    name or "You" — never a raw email address — so callers decide how much
    identity to expose to the provider; nothing here is PII by construction other
    than whatever the sender chose to put in `body_text` itself, which is why it
    must already be plain text (see `nerva_connect.google.mapper`) and, ideally,
    quote-stripped (see `nerva_ai.quoting`) before it reaches this dataclass."""

    direction: MessageDirection
    sender_label: str
    sent_at: datetime
    body_text: str


@dataclass(frozen=True, slots=True)
class AnalysisInput:
    """Everything one `analyze()` call needs, and nothing it does not: no
    conversation id, no workspace id, no contact/company ids. The client cannot
    leak or misuse an identifier it is never given."""

    subject: str | None
    messages: Sequence[AnalysisMessage] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AnalysisOutcome:
    """A successful `analyze()` call: the validated result plus the operational
    metadata `nerva.services.analysis` persists for observability and versioning
    (see `nerva_ai.schemas.SCHEMA_VERSION` / `nerva_ai.prompts.PROMPT_VERSION`).
    Never carries the raw provider response — only what has already been reduced
    to safe, typed values."""

    result: ConversationAnalysisResult
    schema_version: int
    prompt_version: str
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int


class AIAnalysisClient(Protocol):
    """Implemented by exactly one real provider client (added in a later block)
    and by `tests.fakes.ai.FakeAIClient`. Raises an `nerva_ai.errors.AIError`
    subclass on any failure — never returns a partially-valid or best-guess
    result.

    `provider` and `model_id` are known before any call is made (a fixed,
    configured value — which vendor, which pinned model), not something only the
    response reveals. `nerva.services.analysis` reads them to compute an
    idempotency fingerprint *before* deciding whether to call `analyze` at all;
    `AnalysisOutcome.model_id` is what actually produced a given result, in case
    a provider ever reports something more specific than what was requested."""

    provider: str
    model_id: str

    def analyze(self, request: AnalysisInput) -> AnalysisOutcome: ...
