"""M7.1a — pure reasoning contracts: the provider-agnostic boundary a future
agent-reasoning provider sits behind, mirroring `nerva_ai.client`'s own
boundary for conversation analysis exactly.

    Conversation / ConversationAnalysis / Memory
                    v
            AgentReasoningInput
                    v
            AgentReasoningClient            (Protocol — no real implementation yet)
                    v
            AgentReasoningOutcome
                    v
      NoActionDecision | ActionSuggestion    (AgentReasoningDecision)
                    v
      [FUTURE — not this block] ProposalPolicy (nerva_core, pure, no DB/LLM)
                    v
      [FUTURE — not this block] ActionService.propose(actor=Actor.nerva())

This module imports nothing from `nerva.services`, `nerva.repositories`,
`nerva.routes`, SQLAlchemy, or `nerva_connect` — and never will, by design: it
describes what a reasoning provider receives and returns, not how a decision
gets turned into a persisted `Action`. That boundary (`ProposalPolicy`) is
explicitly future work (M7.1b+), as is any real provider, prompt, or
orchestration (M7.1c+). Nothing here calls OpenAI, Gmail, or a database.

## Why this lives in `nerva_ai`, not `nerva_core`

`nerva_core` holds *pure decision rules* that never touch a database or an
LLM (`rules.py`, `memory_rules.py`, `memory_context.py`). This module is the
*provider contract* a future LLM call is shaped by/against — the same role
`nerva_ai.client.AIAnalysisClient`/`AnalysisInput`/`AnalysisOutcome` already
play for conversation analysis. `ProposalPolicy` (M7.1b), which *is* a pure
decision rule over an already-received `AgentReasoningDecision`, belongs in
`nerva_core` instead — mirroring exactly how `AnalysisService` (not
`nerva_core`) builds `AnalysisInput`, while `nerva_core.rules.
decide_materialization` (not `nerva_ai`) decides what a *result* implies.

## Reuse, not duplication, of existing safe DTOs

`AgentReasoningInput` reuses `AnalysisMessage` and `AnalysisMemoryContextItem`
(`nerva_ai.client`) verbatim for conversation/memory content — the same
id-free, already-reduced shapes `AnalysisInput` already uses, so a reasoner
sees exactly the same untrusted-data discipline conversation analysis already
enforces (see those classes' own docstrings). Memory content reaching this
module must already be Block 1/2's final, selected, ranked sequence — this
module never re-ranks, re-filters, or re-queries memory, and never will:
that is `MemoryContextService`/`select_context_memories`'s job, done once,
upstream, by whatever future caller builds an `AgentReasoningInput`.

## Evidence labels: an application-controlled, opaque namespace

A reasoner must be able to point at *why* it suggests something without ever
inventing or receiving a database id. The labels it may cite are fixed by
*this module's own convention*, never chosen by the model and never carried
as a field on `AnalysisMessage`/`AnalysisMemoryContextItem` themselves:

- `messages[i]` (0-indexed in `AgentReasoningInput.messages`) is citable as
  the label `"message_{i+1}"`.
- `memory_context[i]` (0-indexed in `AgentReasoningInput.memory_context`) is
  citable as the label `"memory_{i+1}"`.
- The single `analysis` field is citable as the fixed label `"analysis"`.

`ActionSuggestion.evidence` is restricted to strings matching exactly this
shape (`_EVIDENCE_LABEL_PATTERN`) — a bounded, deterministic check this pure
module *can* make without any context. What it deliberately cannot and does
not check is *groundedness*: whether `"message_7"` actually exists in the
particular `AgentReasoningInput` that produced a given suggestion requires
knowing that input's actual `len(messages)`, which is context this DTO does
not carry about itself. That cross-check belongs to a future `ProposalPolicy`
or orchestration service (M7.1b+), never to this module — see each class's
own docstring.

## Structured output vs. everything downstream — three different questions

`ActionSuggestion`/`NoActionDecision` are `extra="forbid"`, bounded Pydantic
models — the same discipline `nerva_ai.schemas.ConversationAnalysisResult`
already applies to a structured LLM response. Validating *against this
schema* only answers "is this shaped like a suggestion" — it answers neither
"is `action_type` one this workspace is actually allowed to propose right
now" (a future `ProposalPolicy` question, checked against `AgentReasoningInput
.capabilities`) nor "does `payload` actually satisfy `SendEmailPayload`/
`EXECUTABLE_PAYLOADS`" (re-checked independently at proposal time, and again,
unchanged, at execution time — see `nerva_actions.payloads`). These are three
separate validations against three separate contracts, deliberately never
collapsed into one: a payload that merely parses as *some* JSON object is not
assumed valid just because the structured-output call accepted it.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

from nerva.core.enums import (
    ActionType,
    ConversationCategory,
    ConversationPriority,
    SentimentLabel,
)
from nerva.nerva_ai.client import AnalysisMemoryContextItem, AnalysisMessage
from nerva.nerva_ai.schemas import DeadlineEstimate, MonetaryEstimate

# M7.1a only — see the module docstring's "versioning" note: this tracks the
# shape of *this block's* structured output contract (`ActionSuggestion`/
# `NoActionDecision`), independent of any one provider's prompt. M7.1e adds a
# real `AGENT_REASONING_PROMPT_VERSION` (`nerva_ai.agent_reasoning_prompts`),
# but that constant stays there, not here: it versions one provider's prompt
# text, while this one versions the shape every provider's output must
# satisfy regardless of how its prompt is worded. See `AgentReasoningOutcome.
# prompt_version` for where a provider reports its own.
REASONING_SCHEMA_VERSION = 1

_Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
_Summary = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
_Reason = Annotated[str, StringConstraints(min_length=1, max_length=1000, strip_whitespace=True)]
# See the module docstring's "Evidence labels" section for exactly what each
# alternative means and who assigns it. Deliberately no UUID alternative:
# the model is never given, and can never cite, a database id.
_EVIDENCE_LABEL_PATTERN = r"^(message_[1-9][0-9]*|memory_[1-9][0-9]*|analysis)$"
_EvidenceLabel = Annotated[str, StringConstraints(pattern=_EVIDENCE_LABEL_PATTERN)]


@dataclass(frozen=True, slots=True)
class ReasoningAnalysisContext:
    """The minimal, already-safe slice of a conversation's current completed
    `ConversationAnalysis` a reasoner needs — never the ORM row, never its
    id, never `input_fingerprint` (a future `ProposalPolicy`/orchestration
    concern, not something a reasoning provider needs to see). Mirrors
    `nerva_core.rules.AnalysisSummary`'s own "minimal slice" discipline for a
    different pure consumer; reuses `MonetaryEstimate`/`DeadlineEstimate`
    (`nerva_ai.schemas`) verbatim rather than redefining them."""

    category: ConversationCategory
    priority: ConversationPriority
    sentiment: SentimentLabel | None
    requires_response: bool
    commercial_signal: bool
    confidence: float
    summary: str
    intent: str | None
    suggested_next_step: str | None
    estimated_value: MonetaryEstimate | None
    deadline: DeadlineEstimate | None


@dataclass(frozen=True, slots=True)
class AgentReasoningInput:
    """Everything one `reason()` call needs to decide whether proposing an
    action is warranted, and nothing more: no `workspace_id`, no `Memory.id`,
    no `Contact.id`, no `Conversation.id`, no `Action.id`, no integration
    secret, no OAuth token, no authorization data. Mirrors `AnalysisInput`'s
    own "the client cannot leak or misuse an identifier it is never given"
    principle, one level up the pipeline.

    `contact_display_name` is a display name only — never a raw email
    address — the same restraint `AnalysisMessage.sender_label` already
    applies, and for the same reason: a future proposal's real recipient is
    resolved from `Conversation`/`Contact` rows directly by whatever
    materializes an `Action`, never trusted from anything this module's
    consumer (the model) stated.

    `capabilities` is the closed set of `ActionType`s the system — never the
    model — currently allows a suggestion to name; empty means none are
    available right now (a safe default: nothing can be validly suggested).
    The model does not decide which `ActionType`s exist; it may only choose
    among what it is told is available here. See the module docstring's
    "Evidence labels" section for how `messages`/`memory_context` map to the
    labels `ActionSuggestion.evidence` may cite."""

    subject: str | None
    contact_display_name: str | None
    messages: Sequence[AnalysisMessage]
    analysis: ReasoningAnalysisContext
    memory_context: Sequence[AnalysisMemoryContextItem] = field(default_factory=tuple)
    capabilities: Sequence[ActionType] = field(default_factory=tuple)


class NoActionDecision(BaseModel):
    """The reasoner explicitly decided nothing should be proposed right now.
    Never a database row, never persisted by this module or any of its
    callers (see the module docstring) — a plain, bounded, honest "nothing
    to do" outcome, mirroring `nerva_core.rules.MaterializationDecision.NOOP`/
    `nerva_core.memory_rules.MemoryFormationDecision.NOOP`'s own role: a
    legitimate result, not an error and not an empty `ActionSuggestion`."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["no_action"] = "no_action"
    reason: _Reason
    confidence: _Confidence


class ActionSuggestion(BaseModel):
    """The reasoner's candidate for one proposed `Action` — never the
    `Action` itself, and never trusted as one. See the module docstring's
    "Structured output vs. everything downstream" for the three separate
    validations this schema alone does *not* satisfy: whether `action_type`
    is actually allowed right now, and whether `payload` actually satisfies
    its type's real contract, are both left to a future `ProposalPolicy` and
    to `nerva_actions.payloads.parse_action_payload` respectively — neither
    is re-implemented or assumed here.

    `payload` is deliberately a raw, unvalidated JSON object — the same
    shape `nerva.schemas.actions.ActionCreate.payload` already has at the
    API boundary — not `SendEmailPayload`/`ExecutablePayload`: accepting it
    *as* a validated payload here would let "the structured call parsed"
    stand in for "the payload is valid," which the module docstring
    explicitly rejects as two different questions.

    `evidence` is a non-empty, bounded list of labels from
    `AgentReasoningInput`'s own opaque namespace (see the module docstring)
    — never a raw id, never free text, never empty: a suggestion with
    nothing supporting it is not distinguishable from one the model
    fabricated outright, so none is accepted without at least one citation.
    This module validates each label's *shape* only; whether a given label
    actually exists in the specific input that produced this suggestion is
    a groundedness check left to a future `ProposalPolicy` (see the module
    docstring)."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["suggest_action"] = "suggest_action"
    action_type: ActionType
    summary: _Summary
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    confidence: _Confidence
    reason: _Reason
    evidence: Annotated[list[_EvidenceLabel], Field(min_length=1, max_length=10)]


# A discriminated union, not `ActionSuggestion | None`: `NoActionDecision` is
# a distinct, equally-first-class shape (its own `reason`/`confidence`, no
# `action_type`/`payload`/`evidence` at all) — not a degenerate "empty"
# suggestion. `decision` is each variant's own discriminator field, should a
# future real provider need to parse one of the two from a tagged JSON
# response rather than from Python objects directly.
AgentReasoningDecision = NoActionDecision | ActionSuggestion


@dataclass(frozen=True, slots=True)
class AgentReasoningOutcome:
    """A successful `reason()` call: the validated decision plus the
    operational metadata an orchestration layer wants for observability —
    mirrors `AnalysisOutcome` exactly, `prompt_version` included. Never
    carries a raw provider response — only what has already been reduced to
    a validated `AgentReasoningDecision`.

    `prompt_version` identifies *this provider's own* rendered prompt (e.g.
    `nerva_ai.agent_reasoning_prompts.AGENT_REASONING_PROMPT_VERSION` for
    `OpenAIAgentReasoningProvider`, M7.1e) — never confused with
    `REASONING_SCHEMA_VERSION` above (the shape of the decision every
    provider must satisfy) or `nerva_core.agent_reasoning_fingerprint.
    AGENT_REASONING_INPUT_VERSION` (the shape of what a provider is given).
    A provider with no real prompt to version (`tests.fakes.agent_reasoning.
    FakeAgentReasoningClient`) reports a plain, honest sentinel here — never
    a value implying a prompt contract it does not have."""

    decision: AgentReasoningDecision
    schema_version: int
    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    latency_ms: int
    prompt_version: str


class AgentReasoningClient(Protocol):
    """The provider-neutral contract a future real reasoning provider
    implements, and a fake stands in for in every test — mirrors
    `AIAnalysisClient` exactly: `provider`/`model_id` are known before any
    call is made (not something only a response reveals), and a failure is
    always raised, never returned as a partially-valid or best-guess
    decision. No implementation exists yet in this block — no prompt, no
    OpenAI, no orchestration — only this Protocol and the contracts it is
    shaped by/against.

    Raises an `nerva_ai.errors.AIError` subclass on failure — the same
    provider-boundary error hierarchy `AIAnalysisClient.analyze` already
    uses (that module's own docstring already frames it as "errors raised
    across the AI analysis boundary," not something specific to one call);
    no parallel error hierarchy is introduced for a Protocol with no real
    implementation yet."""

    provider: str
    model_id: str

    def reason(self, request: AgentReasoningInput) -> AgentReasoningOutcome: ...
