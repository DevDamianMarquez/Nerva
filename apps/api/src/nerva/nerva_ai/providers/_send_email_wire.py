"""The provider-neutral `send_email` wire contract — extracted out of
`nerva_ai.providers.openai_agent_reasoning` (M7.1e) so a second provider
(`nerva_ai.providers.groq_agent_reasoning`, M7.1g) can share the exact same
validated shape without duplicating it.

Every name here is pure Pydantic: no `openai` import, no `groq` import, no
SDK-specific type. That purity is the whole reason this module is safe to
share — see `nerva_ai.providers.openai_agent_reasoning`'s own module
docstring ("Why a private wire schema, not `ActionSuggestion`/
`NoActionDecision`") for why this wire shape exists at all, and
`nerva_ai.agent_client`'s "Structured output vs. everything downstream" for
why mapping into `ActionSuggestion`/`NoActionDecision` here is still never an
authorization — `nerva_core.proposal_policy.decide_proposal` remains the only
authority regardless of which provider produced the wire response this
module maps.

This extraction is purely mechanical: every provider that imports from here
gets the exact same `_WireNoAction`/`_WireSendEmailPayload`/
`_WireSendEmailSuggestion`/`_WireReasoningResponse` shape and the exact same
`_map_wire_decision` mapping — no provider may fork this contract for itself.
A future capability needs a new wire variant here (and its own branch in
`_map_wire_decision`), exactly as `openai_agent_reasoning`'s own module
docstring already documents for `_WireSendEmailSuggestion`.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from nerva.core.enums import ActionType
from nerva.nerva_ai.agent_client import ActionSuggestion, AgentReasoningDecision, NoActionDecision

# Mirrors the bounds `nerva_ai.agent_client`'s own private constrained types
# apply to `ActionSuggestion`/`NoActionDecision` — duplicated here, not
# imported, because those names are that module's own implementation detail
# (leading underscore). Kept in sync by inspection (both are small, stable,
# and covered by the strict-schema tests of each provider that uses this
# module) rather than by reaching across a private boundary.
_Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
_Reason = Annotated[str, StringConstraints(min_length=1, max_length=1000, strip_whitespace=True)]
_Summary = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
# Same pattern as `nerva_ai.agent_client._EVIDENCE_LABEL_PATTERN` — not
# imported for the same reason as above.
_EVIDENCE_LABEL_PATTERN = r"^(message_[1-9][0-9]*|memory_[1-9][0-9]*|analysis)$"
_EvidenceLabel = Annotated[str, StringConstraints(pattern=_EVIDENCE_LABEL_PATTERN)]
# Mirrors `nerva_actions.payloads.SendEmailPayload`'s own `_Subject`/`_Body`
# bounds exactly, for the same "duplicated, not imported, both covered by a
# test" reason as above.
_Subject = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
_Body = Annotated[str, StringConstraints(min_length=1, max_length=20_000, strip_whitespace=True)]


class _WireSendEmailPayload(BaseModel):
    """The concretely-typed `send_email` candidate payload — see
    `openai_agent_reasoning`'s module docstring ("Why a private wire schema")
    for why this exists instead of `ActionSuggestion.payload`'s free-form
    `dict`.

    **Deliberately has no `recipients` field (M7.1l.1).** Through M7.1g/
    M7.1h/M7.1j the model was asked to supply `recipients` even though
    `AgentReasoningInput` never shows it any address to base one on —
    `AgentReasoningService._resolve_contact` (`nerva.services.
    agent_reasoning`) already resolves the one authoritative recipient from
    `Contact.email` *before* the reasoner is ever called (M7.1d), so asking
    the model for a value it cannot legitimately produce was pure
    responsibility confusion, not a security control: `nerva_core.
    proposal_policy`'s `RECIPIENT_MISMATCH` gate was the only thing actually
    enforcing it either way. A model drafts `subject`/`body` only; a future
    milestone (M7.1l.2/M7.1l.3) deterministically assembles the trusted
    recipient onto `ActionSuggestion.payload` *after* `reason()` returns and
    *before* `nerva_core.proposal_policy.decide_proposal` ever sees it —
    this module, and `OpenAIAgentReasoningProvider`, do not participate in
    that assembly and know nothing about it."""

    model_config = ConfigDict(extra="forbid")

    subject: _Subject
    body: _Body


class _WireNoAction(BaseModel):
    """Wire shape for "no action warranted" — maps 1:1 onto `nerva_ai.
    agent_client.NoActionDecision`."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["no_action"] = "no_action"
    reason: _Reason
    confidence: _Confidence


class _WireSendEmailSuggestion(BaseModel):
    """Wire shape for the one candidate action this contract can ever
    express. There is deliberately no `action_type` field at all — the
    discriminator value (`"suggest_send_email"`) already says which action
    type this is, so there is nothing for a model to set that could ever
    name `create_task`/`update_opportunity_stage`: those are not merely
    disallowed by instruction, they do not exist anywhere in this schema. A
    future capability needs its own sibling variant here (and its own branch
    in `_map_wire_decision`), the same way `nerva_core.proposal_policy.
    _ALLOWED_ACTION_TYPES` needs its own new entry."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["suggest_send_email"] = "suggest_send_email"
    summary: _Summary
    payload: _WireSendEmailPayload
    confidence: _Confidence
    reason: _Reason
    evidence: Annotated[list[_EvidenceLabel], Field(min_length=1, max_length=10)]


class _WireReasoningResponse(BaseModel):
    """The actual `response_format` schema target every provider using this
    module validates against. A bare `Union` is not a valid top-level JSON
    object schema for structured-output APIs (OpenAI's SDK raises `TypeError`
    for one directly; Groq's `json_schema` parameter expects a single object
    schema too) — wrapping it in one field of an object schema is the
    standard pattern, and the `decision` discriminator (`"no_action"`/
    `"suggest_send_email"`) lets Pydantic resolve the right variant on parse,
    with no custom logic needed beyond reading `.decision`."""

    model_config = ConfigDict(extra="forbid")

    decision: _WireNoAction | _WireSendEmailSuggestion


def _map_wire_decision(wire: _WireReasoningResponse) -> AgentReasoningDecision:
    """The one, deterministic mapping from a validated wire response to the
    real `AgentReasoningDecision` — never trusted as already-authorized:
    `nerva_core.proposal_policy.decide_proposal` is still unconditionally
    required downstream before anything this maps to can ever become a
    persisted `Action`, regardless of which provider produced `wire`."""
    decision = wire.decision
    if isinstance(decision, _WireNoAction):
        return NoActionDecision(reason=decision.reason, confidence=decision.confidence)
    return ActionSuggestion(
        action_type=ActionType.SEND_EMAIL,
        summary=decision.summary,
        payload=decision.payload.model_dump(mode="json"),
        confidence=decision.confidence,
        reason=decision.reason,
        evidence=list(decision.evidence),
    )
