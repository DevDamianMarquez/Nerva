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

## M6.4 Block 3: memory as analysis context

`analyze()` now also asks `MemoryContextService.get_context_for_conversation`
(Block 1/2) for the already-selected, already-ranked memory relevant to this
conversation, maps it through `build_memory_context_items` into
`AnalysisInput.memory_context`, and folds it into `compute_fingerprint` — a
changed memory context now makes a conversation eligible for re-analysis even
if no message changed, the same way a schema/prompt/model version change
already does. This module never re-ranks or re-filters memory: Block 1
(`nerva_core.memory_context.select_context_memories`) remains the sole
authority on *which* memories and in *what order*; `build_memory_context_items`
below only relabels an already-final sequence into the id-free shape a
provider may see.

**Failure behavior, deliberately with no degradation path.** `self.memory_
context.get_context_for_conversation` is called with no `try`/`except` around
it. `MemoryContextService` (Block 2) already fails closed — an unknown/cross-
workspace conversation raises `NotFoundError`, a corrupt `Memory.content`
raises `MemoryContentError` — and this module does not add a second,
competing policy on top: it was deliberately considered and rejected. Today's
architecture has no failure mode out of `get_context_for_conversation` that is
*both* (a) something this module could prove is memory-retrieval-specific
rather than a wider authorization/tenant-isolation/data-integrity problem, and
(b) safe to paper over with an empty context rather than surface. Absent that
proof, catching anything here — even narrowly — would risk exactly what this
block was told not to do: turning an authorization or corruption failure into
a silent empty context. A `conversation_id` already resolved by this very
method (just above, via `self.conversations.get`) in the same workspace
context cannot legitimately turn up `NotFoundError` from
`MemoryContextService` either, so there is no routine case this omission
affects — if one of these does ever fire here, that is a genuine bug or a
genuine security-relevant condition, and `analyze()` failing outright (the
existing behavior for any other read failure in this method) is the correct
response, not a quieter one.

## M6.4 Block 4: audit snapshot

`_persist_success` records `ConversationAnalysis.memory_ids_used` — the exact
ids of `selected_memories` (the same list `build_memory_context_items` just
turned into `analysis_input.memory_context`), in the same order, with no
second query and no re-running the selector. This is the one and only writer
of that column. A cache hit (`return existing` above, before either persist
method ever runs) therefore never touches it — the stored snapshot keeps
describing whatever context actually produced the result still being
returned. `_persist_failure` only ever clears it (via `_clear_result_fields`,
and only for a brand-new row with no prior result to protect); an existing
row's failed retry leaves it alone, for the same reason it leaves every other
result field alone — see that method's own comment.
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
from nerva.nerva_ai.client import (
    AIAnalysisClient,
    AnalysisInput,
    AnalysisMemoryContextItem,
    AnalysisMessage,
    AnalysisOutcome,
)
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
from nerva.nerva_core.memory_context import MemoryCandidate, MemoryScope
from nerva.repositories.workspace_data import (
    ConversationAnalysisRepository,
    ConversationRepository,
    MessageRepository,
)
from nerva.services.activity import ActivityService
from nerva.services.memory_context import MemoryContextService

# Bounds on what one analysis request may send to a provider, so a single call's
# cost and latency stay predictable regardless of how long a thread is or how
# large any one message is. Chosen for v1, not derived from a provider's actual
# context window: comfortably small relative to any current model, and easy to
# raise later without a migration. Kept as module constants (not buried in a
# function body) so tests exercise the real values, never a stand-in for them.
MAX_MESSAGES_PER_ANALYSIS = 20
MAX_CHARS_PER_MESSAGE = 4_000
MAX_TOTAL_CHARS = 20_000

# The *only* place these are defined — every caller (today, just `analyze()`
# below) must read them from here rather than copying a number. This bounds
# what `MemoryContextService.get_context_for_conversation` (Block 1/2) is
# asked for; it is not the same ceiling as `nerva_ai.prompts.
# MAX_RENDERED_MEMORY_CONTEXT_CHARS` (that one bounds the *rendered* prompt
# text, including metadata this module's items do not carry — see that
# module's docstring for why the two must stay distinct).
ANALYSIS_MEMORY_MAX_ITEMS = 10
ANALYSIS_MEMORY_MAX_CHARS = 4_000

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


def build_analysis_input(
    conversation: Conversation,
    messages: Sequence[Message],
    memory_context: Sequence[AnalysisMemoryContextItem] = (),
) -> AnalysisInput:
    """Deterministic: the same `messages`/`memory_context` always produce the
    same `AnalysisInput`, which is what makes `compute_fingerprint` meaningful.
    Never mutates `messages` — quote-stripping and truncation only ever affect
    the copy returned here, never the stored `Message.body_text`.

    When a bound forces a choice, the newest messages are kept: they are the
    most likely to matter for an intent/priority/next-step read on a thread,
    and quoted history (already trimmed per-message by
    `nerva_ai.quoting.strip_quoted_reply`) means older context is usually
    already folded into what the newest message quotes anyway.

    `memory_context` is accepted, not built here — see `build_memory_context_
    items` and the module docstring's "memory as analysis context" section —
    and is passed through to `AnalysisInput` exactly as given, in exactly the
    order given, with no re-ranking. Defaults to empty so every existing
    caller that never mentions memory keeps working unchanged.
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
        memory_context=tuple(memory_context),
    )


def build_memory_context_items(
    candidates: Sequence[MemoryCandidate],
) -> Sequence[AnalysisMemoryContextItem]:
    """Maps Block 1/2's already-selected, already-ranked `MemoryCandidate`s
    (`MemoryContextService.get_context_for_conversation`'s return value,
    passed through unmodified and in the exact order given) into the minimal,
    id-free shape `AnalysisInput.memory_context` exposes to a provider. Never
    re-ranks, re-filters, or re-queries memory — `candidates` must already be
    exactly what that method returned; this only relabels and strips ids.

    `scope` is derived from each candidate's own subject fields (`_scope_of`),
    not by re-comparing against a `MemoryContextTarget` — a candidate reaching
    this function already matched `target` by construction (Block 1's
    `classify_scope` already decided that before this candidate was ever
    selected), so which single subject field is set
    (`Memory.at_most_one_subject`) already *is* its scope. Recomputing the
    comparison against `target` here would be the exact "duplicate ranking"
    this block was asked not to do, for no additional correctness.
    """
    return tuple(
        AnalysisMemoryContextItem(
            statement=candidate.statement,
            kind=candidate.kind,
            origin=candidate.origin,
            confidence=candidate.confidence,
            observed_at=candidate.observed_at,
            scope=_scope_of(candidate),
        )
        for candidate in candidates
    )


def _scope_of(candidate: MemoryCandidate) -> MemoryScope:
    if candidate.opportunity_id is not None:
        return MemoryScope.OPPORTUNITY
    if candidate.contact_id is not None:
        return MemoryScope.CONTACT
    if candidate.company_id is not None:
        return MemoryScope.COMPANY
    return MemoryScope.WORKSPACE


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

    `memory_context` (M6.4 Block 3) is folded in the same way, over exactly
    the fields a provider actually sees (`statement`, `kind`, `origin`,
    `confidence`, `observed_at`, `scope`) — deliberately never a `Memory.id`
    or any other database identifier, so two semantically identical contexts
    (same beliefs, same metadata) always fingerprint identically regardless
    of which physical row produced them. `scope.name` (not `.value`) is used
    because `MemoryScope` is a plain `Enum` with `auto()` values — `.name`
    ("OPPORTUNITY", …) is stable text; `.value` is an arbitrary int that would
    silently change if the enum's members were ever reordered, which must
    never silently change what a conversation fingerprints to. A memory the
    selector did not choose never reaches `analysis_input.memory_context` in
    the first place (that decision already happened in `MemoryContextService`/
    `select_context_memories`), so it already cannot affect this fingerprint
    without this function doing anything extra about it.
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
        "memory_context": [
            {
                "statement": item.statement,
                "kind": item.kind.value,
                "origin": item.origin.value,
                "confidence": item.confidence,
                "observed_at": item.observed_at.isoformat(),
                "scope": item.scope.name,
            }
            for item in analysis_input.memory_context
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
    not have. `memory_ids_used` is a result field by this same reasoning (M6.4
    Block 4): it describes what produced the *result*, so a row with no result
    must not claim one either — see `_persist_failure`'s own comment for why an
    *existing* row's failed retry leaves it untouched instead."""
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
    analysis.memory_ids_used = []


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
        self.memory_context = MemoryContextService(session, ctx)

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
        # No try/except here — see the module docstring's "Failure behavior"
        # for why a Block 2 failure (authorization, tenant isolation, data
        # integrity) must propagate and fail this call outright, never
        # degrade into an empty context.
        selected_memories = self.memory_context.get_context_for_conversation(
            conversation_id=conversation_id,
            max_items=ANALYSIS_MEMORY_MAX_ITEMS,
            max_chars=ANALYSIS_MEMORY_MAX_CHARS,
        )
        memory_context = build_memory_context_items(selected_memories)
        analysis_input = build_analysis_input(conversation, messages, memory_context)
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

        return self._persist_success(
            existing, conversation, fingerprint, attempts, outcome, selected_memories
        )

    def _persist_success(
        self,
        existing: ConversationAnalysis | None,
        conversation: Conversation,
        fingerprint: str,
        attempts: int,
        outcome: AnalysisOutcome,
        selected_memories: Sequence[MemoryCandidate],
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
        # M6.4 Block 4: the exact, already-selected candidates that produced
        # `analysis_input.memory_context` for *this* call — not a second query,
        # not the selector run again. See the module docstring and
        # `ConversationAnalysis.memory_ids_used`'s own docstring.
        analysis.memory_ids_used = [candidate.id for candidate in selected_memories]

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
        # sync leaves an integration's last-known-good fields untouched. This
        # includes `memory_ids_used` (M6.4 Block 4): the still-displayed result
        # was produced by the *previous* successful call's context, not this
        # failed attempt's — overwriting it here would make the audit snapshot
        # describe a context that never actually produced the stored result.

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
