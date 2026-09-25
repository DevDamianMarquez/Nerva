"""Orchestrates AI analysis of one conversation.

Builds a bounded, deterministic `AnalysisInput` from already-imported
`Conversation`/`Message` rows, decides — via a content-and-version fingerprint —
whether a cached analysis is still valid, and only when it is not, calls the
provider-agnostic `AIAnalysisClient` and persists the validated result.

This module never imports a vendor SDK and never touches Gmail: it only reads
rows a connector already imported (see `docs/ARCHITECTURE.md` §10's "email
content is untrusted input" — that boundary is inherited here, not
re-litigated). It also never decides what business object a result should
create (an `Opportunity`, say) — that is deterministic rules' job, in a later
block; this service's only output is a persisted `ConversationAnalysis` row and
its activity trail.
"""

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import ActivityEventType, AnalysisStatus, MessageDirection
from nerva.core.errors import NotFoundError
from nerva.db.base import utcnow
from nerva.models import Conversation, ConversationAnalysis, Message
from nerva.nerva_ai.client import AIAnalysisClient, AnalysisInput, AnalysisMessage, AnalysisOutcome
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from nerva.nerva_ai.prompts import PROMPT_VERSION
from nerva.nerva_ai.quoting import strip_quoted_reply
from nerva.nerva_ai.schemas import SCHEMA_VERSION
from nerva.repositories.workspace_data import (
    ConversationAnalysisRepository,
    ConversationRepository,
    MessageRepository,
)
from nerva.services.activity import ActivityService

# Bounds on what one analysis request may send to a provider, so a single call's
# cost and latency stay predictable regardless of how long a thread is or how
# large any one message is. Chosen for v1, not derived from a provider's actual
# context window: comfortably small relative to any current model, and easy to
# raise later without a migration. Kept as module constants (not buried in a
# function body) so tests exercise the real values, never a stand-in for them.
MAX_MESSAGES_PER_ANALYSIS = 20
MAX_CHARS_PER_MESSAGE = 4_000
MAX_TOTAL_CHARS = 20_000

_TRUNCATION_SUFFIX = "…"


class AnalysisAuthError(Exception):
    """The AI provider rejected our credentials, or is not configured. The
    analysis has already been marked `failed` and recorded before this is
    raised — mirrors `IntegrationAuthError` in `nerva.services.integrations`."""


class AnalysisProviderError(Exception):
    """The provider call failed for a reason other than credentials: a
    transient/outage failure, or (via its subclasses below) a rate limit or
    output that failed schema validation. The analysis has already been
    marked `failed` and recorded before this is raised. Callers that only
    need the coarse "did it fail" answer can catch this base class — the API
    layer (Block 5) catches the more specific subclasses first to map a rate
    limit to `429` and invalid output to a distinct safe response, rather than
    treating every provider failure alike."""


class AnalysisRateLimitError(AnalysisProviderError):
    """The AI provider is rate-limiting requests."""


class AnalysisOutputError(AnalysisProviderError):
    """The AI provider's output did not match the expected schema."""


def build_analysis_input(conversation: Conversation, messages: Sequence[Message]) -> AnalysisInput:
    """Deterministic: the same `messages` always produce the same
    `AnalysisInput`, which is what makes `compute_fingerprint` meaningful. Never
    mutates `messages` — quote-stripping and truncation only ever affect the
    copy returned here, never the stored `Message.body_text`.

    When a bound forces a choice, the newest messages are kept: they are the
    most likely to matter for an intent/priority/next-step read on a thread,
    and quoted history (already trimmed per-message by
    `nerva_ai.quoting.strip_quoted_reply`) means older context is usually
    already folded into what the newest message quotes anyway.
    """
    prepared = [(message, _prepare_body(message.body_text)) for message in messages]
    newest_first = list(reversed(prepared))[:MAX_MESSAGES_PER_ANALYSIS]
    selected = list(reversed(newest_first))
    while len(selected) > 1 and _total_chars(selected) > MAX_TOTAL_CHARS:
        selected.pop(0)  # drop the oldest of what remains; newest always survives

    return AnalysisInput(
        subject=conversation.subject,
        messages=tuple(
            AnalysisMessage(
                direction=message.direction,
                sender_label=_sender_label(message),
                sent_at=message.sent_at,
                body_text=body,
            )
            for message, body in selected
        ),
    )


def _prepare_body(body_text: str) -> str:
    stripped = strip_quoted_reply(body_text)
    if len(stripped) <= MAX_CHARS_PER_MESSAGE:
        return stripped
    return stripped[:MAX_CHARS_PER_MESSAGE].rstrip() + _TRUNCATION_SUFFIX


def _total_chars(selected: Sequence[tuple[Message, str]]) -> int:
    return sum(len(body) for _, body in selected)


def _sender_label(message: Message) -> str:
    if message.direction == MessageDirection.OUTBOUND:
        return "You"
    return message.sender_name or message.sender_email


def compute_fingerprint(
    analysis_input: AnalysisInput, *, schema_version: int, prompt_version: str, model_id: str
) -> str:
    """A stable SHA-256 of exactly what a provider call would depend on: the
    built `AnalysisInput` plus the three version stamps that should also
    invalidate a cached result (requirement: a schema, prompt, or model change
    makes every conversation eligible for re-analysis). No timestamp of "now"
    and no random value ever enters this — the same input always yields the
    same fingerprint, which is what makes zero-provider-call caching possible.
    `sent_at` is included because it is content (which message this is), not a
    "now"; it never changes between two calls over the same stored messages.
    """
    normalized = {
        "subject": analysis_input.subject or "",
        "messages": [
            {
                "direction": message.direction.value,
                "sender_label": message.sender_label,
                "sent_at": message.sent_at.isoformat(),
                "body_text": message.body_text,
            }
            for message in analysis_input.messages
        ],
        "schema_version": schema_version,
        "prompt_version": prompt_version,
        "model_id": model_id,
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _safe_error_message(error: AIError) -> str:
    """A bounded, generic classification — never the exception's own message,
    which (from a real provider) could echo back response text we do not want
    stored even in a "safe" field. Matches every failure type in
    `nerva_ai.errors` explicitly rather than falling through to `str(error)`."""
    if isinstance(error, AIAuthError):
        return "The AI provider rejected our credentials."
    if isinstance(error, AIRateLimitError):
        return "The AI provider is rate-limiting requests."
    if isinstance(error, AIOutputValidationError):
        return "The AI provider's output did not match the expected schema."
    if isinstance(error, AIProviderError):
        return "Could not reach the AI provider."
    return "The AI analysis failed."


def _clear_result_fields(analysis: ConversationAnalysis) -> None:
    """Used only for a conversation's first-ever failed attempt, where there is
    no prior result to preserve: a fresh row must never claim a result it does
    not have."""
    analysis.category = None
    analysis.priority = None
    analysis.sentiment = None
    analysis.requires_response = None
    analysis.commercial_signal = None
    analysis.confidence = None
    analysis.estimated_value_cents = None
    analysis.value_currency = None
    analysis.value_is_explicit = None
    analysis.deadline_at = None
    analysis.deadline_is_explicit = None
    analysis.summary = None
    analysis.intent = None
    analysis.suggested_next_step = None
    analysis.mentioned_people = []
    analysis.mentioned_companies = []
    analysis.input_tokens = None
    analysis.output_tokens = None
    analysis.latency_ms = None
    analysis.analyzed_at = None


class AnalysisService:
    def __init__(
        self, session: Session, ctx: WorkspaceContext, *, ai_client: AIAnalysisClient
    ) -> None:
        self.session = session
        self.ctx = ctx
        self.ai_client = ai_client
        self.conversations = ConversationRepository(session, ctx.workspace_id)
        self.messages = MessageRepository(session, ctx.workspace_id)
        self.analyses = ConversationAnalysisRepository(session, ctx.workspace_id)
        self.activity = ActivityService(session, ctx.workspace_id)

    def analyze(self, conversation_id: uuid.UUID, *, force: bool = False) -> ConversationAnalysis:
        """Analyzes one conversation, or returns the existing analysis unchanged
        if nothing relevant has changed since it last completed. Raises
        `NotFoundError` (404) if the conversation is not in this workspace,
        `AnalysisAuthError` if the provider rejected our credentials, or
        `AnalysisProviderError` for any other provider failure — in the last two
        cases a `failed` `ConversationAnalysis` has already been committed."""
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.")

        messages = self.messages.list_for_conversation(conversation_id)
        analysis_input = build_analysis_input(conversation, messages)
        fingerprint = compute_fingerprint(
            analysis_input,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model_id=self.ai_client.model_id,
        )

        # Row-locked: two concurrent calls for the same conversation cannot both
        # decide there is nothing cached and both call the provider, and cannot
        # both attempt to insert a first row (the unique constraint from Block 2
        # is the second line of defense if they somehow did).
        existing = self.analyses.get_for_conversation(conversation_id, for_update=True)
        if (
            not force
            and existing is not None
            and existing.status == AnalysisStatus.COMPLETED
            and existing.input_fingerprint == fingerprint
        ):
            return existing  # cache hit: zero provider calls

        attempts = (existing.attempts if existing is not None else 0) + 1
        try:
            outcome = self.ai_client.analyze(analysis_input)
        except AIError as exc:
            self._persist_failure(existing, conversation, fingerprint, attempts, exc)
            if isinstance(exc, AIAuthError):
                raise AnalysisAuthError(str(exc)) from exc
            if isinstance(exc, AIRateLimitError):
                raise AnalysisRateLimitError(str(exc)) from exc
            if isinstance(exc, AIOutputValidationError):
                raise AnalysisOutputError(str(exc)) from exc
            raise AnalysisProviderError(str(exc)) from exc

        return self._persist_success(existing, conversation, fingerprint, attempts, outcome)

    def _persist_success(
        self,
        existing: ConversationAnalysis | None,
        conversation: Conversation,
        fingerprint: str,
        attempts: int,
        outcome: AnalysisOutcome,
    ) -> ConversationAnalysis:
        result = outcome.result
        now: datetime = utcnow()
        analysis = existing or ConversationAnalysis(conversation_id=conversation.id)

        analysis.status = AnalysisStatus.COMPLETED
        analysis.attempts = attempts
        analysis.last_attempted_at = now
        analysis.last_error = None
        analysis.schema_version = outcome.schema_version
        analysis.prompt_version = outcome.prompt_version
        analysis.provider = self.ai_client.provider
        analysis.model_id = outcome.model_id
        analysis.input_fingerprint = fingerprint
        analysis.category = result.category
        analysis.priority = result.priority
        analysis.sentiment = result.sentiment
        analysis.requires_response = result.requires_response
        analysis.commercial_signal = result.commercial_signal
        analysis.confidence = result.confidence
        analysis.estimated_value_cents = (
            result.estimated_value.amount_cents if result.estimated_value else None
        )
        analysis.value_currency = (
            result.estimated_value.currency if result.estimated_value else None
        )
        analysis.value_is_explicit = (
            result.estimated_value.is_explicit if result.estimated_value else None
        )
        analysis.deadline_at = result.deadline.date if result.deadline else None
        analysis.deadline_is_explicit = result.deadline.is_explicit if result.deadline else None
        analysis.summary = result.summary
        analysis.intent = result.intent
        analysis.suggested_next_step = result.suggested_next_step
        analysis.mentioned_people = list(result.mentioned_people)
        analysis.mentioned_companies = list(result.mentioned_companies)
        analysis.input_tokens = outcome.input_tokens
        analysis.output_tokens = outcome.output_tokens
        analysis.latency_ms = outcome.latency_ms
        analysis.analyzed_at = now

        if existing is None:
            self.analyses.add(analysis)

        self.activity.record(
            actor=Actor.nerva(),
            event_type=ActivityEventType.CONVERSATION_ANALYZED,
            entity_type="conversation",
            entity_id=conversation.id,
            summary=f'Conversation "{conversation.subject or "(no subject)"}" was analyzed.',
            data={
                "category": result.category.value,
                "priority": result.priority.value,
                "requires_response": result.requires_response,
                "commercial_signal": result.commercial_signal,
                "confidence": result.confidence,
                "schema_version": outcome.schema_version,
                "prompt_version": outcome.prompt_version,
                "model_id": outcome.model_id,
            },
        )
        self.session.commit()
        return analysis

    def _persist_failure(
        self,
        existing: ConversationAnalysis | None,
        conversation: Conversation,
        fingerprint: str,
        attempts: int,
        error: AIError,
    ) -> None:
        now: datetime = utcnow()
        is_new = existing is None
        analysis = existing or ConversationAnalysis(conversation_id=conversation.id)
        safe_error = _safe_error_message(error)

        analysis.status = AnalysisStatus.FAILED
        analysis.attempts = attempts
        analysis.last_attempted_at = now
        analysis.last_error = safe_error
        analysis.schema_version = SCHEMA_VERSION
        analysis.prompt_version = PROMPT_VERSION
        analysis.provider = self.ai_client.provider
        analysis.model_id = self.ai_client.model_id
        analysis.input_fingerprint = fingerprint
        if is_new:
            _clear_result_fields(analysis)
            self.analyses.add(analysis)
        # else: keep whatever the last successful analysis found — stale but
        # real is better than silently discarded, mirroring how a failed Gmail
        # sync leaves an integration's last-known-good fields untouched.

        self.activity.record(
            actor=Actor.nerva(),
            event_type=ActivityEventType.CONVERSATION_ANALYSIS_FAILED,
            entity_type="conversation",
            entity_id=conversation.id,
            summary=(
                f'Conversation "{conversation.subject or "(no subject)"}" could not be analyzed.'
            ),
            data={"error": safe_error, "attempts": attempts},
        )
        self.session.commit()
