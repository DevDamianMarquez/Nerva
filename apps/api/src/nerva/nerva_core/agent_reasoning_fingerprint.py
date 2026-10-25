"""M7.1d — the deterministic identity of one *reasoning state*: everything a
reasoner was actually shown, reduced to a single stable digest.

    AgentReasoningInput            (exactly the object handed to
            v                       `AgentReasoningClient.reason`)
    compute_agent_reasoning_fingerprint
            v
    a stable SHA-256 hex digest — "this exact conversation + this exact
    analysis content + this exact memory context + this exact contact
    identity + these exact capabilities, under these exact contract
    versions" — supplied to `nerva_core.proposal_fingerprint.
    ProposalProvenance.source_fingerprint`

Pure — no database, no HTTP, no provider call — mirroring `nerva.services.
analysis.compute_fingerprint` and `nerva_core.proposal_fingerprint.
compute_proposal_fingerprint` exactly: same canonicalization technique
(`json.dumps(..., sort_keys=True, separators=(",", ":"))` then SHA-256), same
"no timestamp of 'now', no random value, ever" guarantee.

## Why this exists rather than reusing `ConversationAnalysis.input_fingerprint`

M7.1c anticipated that a proposal's `source_fingerprint` would simply be
`ConversationAnalysis.input_fingerprint`, copied at reasoning time. That is
not honest once an `AgentReasoningInput` exists: it carries strictly *more*
than the analysis did — the memory context selected **now** (a separate,
later `MemoryContextService` call, not the one the stored analysis was
produced from — see `ConversationAnalysis.memory_ids_used`'s own docstring
for why that column is an audit snapshot and not a current selection), the
contact display name, and the capability set declared for this run. Two
reasoning runs over the same unchanged analysis but a changed memory context
would share one `input_fingerprint` and therefore collide into a single
proposal identity, silently suppressing the second as a duplicate. This
function hashes the real input instead, so the digest changes exactly when
something the reasoner saw changed.

## What participates, and why

Everything in `AgentReasoningInput`, field for field — that is the whole
point: the digest must identify *what the reasoner was shown*, so an
omission here is a way for two genuinely different reasoning states to
collide. Concretely: `subject`, `contact_display_name`, every `messages[i]`
(direction/sender label/`sent_at`/body text), every field of `analysis`
(`ReasoningAnalysisContext`, including the nested `MonetaryEstimate`/
`DeadlineEstimate`), every `memory_context[i]` (statement/kind/origin/
confidence/`observed_at`/scope), and `capabilities`.

`sent_at`/`observed_at`/`deadline.date` are content — *which* message this
is, *when* a belief was true — never a "now"; they do not change between two
calls over the same stored rows. `None` is encoded as JSON `null`, never
coerced to `""`/`0`: a contact with no display name and a contact literally
named `""` are different states, and `nerva.services.analysis.
compute_fingerprint`'s `subject or ""` conflation is deliberately not copied
here.

`capabilities` is canonicalized as a sorted, de-duplicated list of
`ActionType.value`s. It is conceptually a *set* of what was on offer (see
`AgentReasoningInput.capabilities`' own docstring), so neither the order a
caller listed them in nor an accidental repeat is part of the reasoning
state — but *which* types were offered very much is.

Both contract versions are folded in as plain fields:
`AGENT_REASONING_INPUT_VERSION` (the shape of the input, and this module's
own canonicalization of it) and `nerva_ai.agent_client.
REASONING_SCHEMA_VERSION` (the shape of the decision a reasoner may return,
which bounds what it could have decided). A change to either should make the
same conversation eligible for a fresh proposal rather than collide with an
old one — the same "stamps that should also invalidate" reasoning
`nerva.services.analysis.compute_fingerprint` already documents.

## Deliberately excluded: provider and model identity

`AgentReasoningClient.provider`/`.model_id` are **not** included, unlike
`nerva.services.analysis.compute_fingerprint`'s `model_id`. Two reasons, and
one acknowledged consequence:

1. This function is specified as, and documented as, the identity of *the
   input the reasoner receives*. Neither value is part of
   `AgentReasoningInput` — a provider is not something the reasoning state
   knows about itself.
2. The digest feeds `ProposalProvenance.source_fingerprint`, whose job is
   deduplicating *proposals*. Two models that independently reason to the
   exact same email, from the exact same state, for the same reason, should
   produce one proposal for a human to approve — not two identical ones
   differing only by which model wrote it.

The consequence, stated plainly: switching provider or model does **not**
make an already-proposed action re-proposable, the way switching the
*analysis* model does make a conversation re-analyzable. That asymmetry is
intentional (a cached analysis is a derived read; a proposal is a pending
request for a human decision), and it is the reason `AnalysisOutcome.
model_id`-style metadata is reported on
`nerva.services.agent_reasoning.AgentReasoningResult` for observability
instead of being hashed in.
"""

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from nerva.core.enums import ActionType
from nerva.nerva_ai.agent_client import (
    REASONING_SCHEMA_VERSION,
    AgentReasoningInput,
    ReasoningAnalysisContext,
)
from nerva.nerva_ai.client import AnalysisMemoryContextItem, AnalysisMessage

# The version of the *reasoning input contract* — the shape of what a
# reasoner is handed, plus this module's canonicalization of it. Deliberately
# **not** called a prompt version: no prompt exists to version (see
# `nerva_ai.agent_client`'s own `REASONING_SCHEMA_VERSION` note, and
# `nerva_core.proposal_fingerprint`'s note on the `reasoning_version` rename
# this constant now fills). A real provider that renders a prompt should add
# its own prompt version *alongside* this one, never instead of it: a prompt
# change and an input-contract change are independent reasons for the same
# state to reason differently.
AGENT_REASONING_INPUT_VERSION = "v1"

# Bumped only if this module's own canonicalization rules change in a way
# that should invalidate every previously-computed fingerprint.
AGENT_REASONING_FINGERPRINT_SCHEMA_VERSION = 1


def compute_agent_reasoning_fingerprint(request: AgentReasoningInput) -> str:
    """A stable SHA-256 hex digest of exactly what `request` shows a
    reasoner — see the module docstring for precisely what is, and is not,
    included. No timestamp of "now", no random value, no database id (an
    `AgentReasoningInput` carries none to begin with — that is M7.1a's own
    guarantee, not something re-checked here)."""
    normalized: dict[str, Any] = {
        "schema_version": AGENT_REASONING_FINGERPRINT_SCHEMA_VERSION,
        "input_version": AGENT_REASONING_INPUT_VERSION,
        "reasoning_schema_version": REASONING_SCHEMA_VERSION,
        "subject": request.subject,
        "contact_display_name": request.contact_display_name,
        "messages": [_canonical_message(message) for message in request.messages],
        "analysis": _canonical_analysis(request.analysis),
        "memory_context": [_canonical_memory_item(item) for item in request.memory_context],
        "capabilities": _canonical_capabilities(request.capabilities),
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_message(message: AnalysisMessage) -> dict[str, Any]:
    """Exactly the four fields `AnalysisMessage` carries — the same four
    `nerva.services.analysis.compute_fingerprint` already normalizes, in the
    same way, so a message's contribution to either digest is derived
    identically."""
    return {
        "direction": message.direction.value,
        "sender_label": message.sender_label,
        "sent_at": message.sent_at.isoformat(),
        "body_text": message.body_text,
    }


def _canonical_memory_item(item: AnalysisMemoryContextItem) -> dict[str, Any]:
    """`scope.name` (not `.value`) for the reason `nerva.services.analysis.
    compute_fingerprint` already documents: `MemoryScope` is a plain `Enum`
    with `auto()` values, so `.value` is an arbitrary int that reordering the
    enum's members would silently change."""
    return {
        "statement": item.statement,
        "kind": item.kind.value,
        "origin": item.origin.value,
        "confidence": item.confidence,
        "observed_at": item.observed_at.isoformat(),
        "scope": item.scope.name,
    }


def _canonical_analysis(analysis: ReasoningAnalysisContext) -> dict[str, Any]:
    """Every field of `ReasoningAnalysisContext`, including the free-text ones.

    Unlike `nerva_core.proposal_fingerprint`, prose is **not** excluded here:
    there, `summary`/`reason` are the model's own explanation of an action
    whose identity is already fully pinned by its type and payload; here,
    `summary`/`intent`/`suggested_next_step` are *input* the reasoner reads
    and could plausibly decide differently on. A changed analysis summary is
    a changed reasoning state."""
    estimated_value = analysis.estimated_value
    deadline = analysis.deadline
    return {
        "category": analysis.category.value,
        "priority": analysis.priority.value,
        "sentiment": analysis.sentiment.value if analysis.sentiment is not None else None,
        "requires_response": analysis.requires_response,
        "commercial_signal": analysis.commercial_signal,
        "confidence": analysis.confidence,
        "summary": analysis.summary,
        "intent": analysis.intent,
        "suggested_next_step": analysis.suggested_next_step,
        "estimated_value": (
            None
            if estimated_value is None
            else {
                "amount_cents": estimated_value.amount_cents,
                "currency": estimated_value.currency,
                "is_explicit": estimated_value.is_explicit,
            }
        ),
        "deadline": (
            None
            if deadline is None
            else {"date": deadline.date.isoformat(), "is_explicit": deadline.is_explicit}
        ),
    }


def _canonical_capabilities(capabilities: Sequence[ActionType]) -> list[str]:
    return sorted({capability.value for capability in capabilities})
