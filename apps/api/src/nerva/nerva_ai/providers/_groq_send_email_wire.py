"""Groq-specific flat `send_email` wire contract (M7.1i.1).

`nerva_ai.providers._send_email_wire` (the shared OpenAI/Groq contract until
now) wraps its discriminated union in one object field: `{"decision": {...}}`,
where the nested object is itself one of two whole-object `$defs` variants
selected by `anyOf`. That shape is exactly what OpenAI's own Structured
Outputs implementation handles correctly (verified in the M7.1i audit:
`_strict_schema(_WireReasoningResponse)` and the `openai` SDK's own
`type_to_response_format_param` converter produce byte-identical schemas for
the same Pydantic model) — but a real M7.1h smoke test against Groq's
`openai/gpt-oss-20b` got back a flat object (`{"decision": "no_action",
"reason": ..., "confidence": ..., "summary": ...}`) instead of the required
nested envelope, and Groq's own API rejected it with `json_validate_failed`
before this codebase ever saw it. The M7.1i audit ruled out a malformed
schema (Groq's docs explicitly support `anyOf` + `$ref` + nested `$defs`
unions, and `openai/gpt-oss-20b` is a supported Structured Outputs model) —
the schema sent was valid, but Groq's own generation for this provider/model
combination did not reliably honor the nesting.

This module is the fix the M7.1i audit recommended (Option A): give Groq its
own *flat* wire contract, provider-specific, where the discriminator and
every field the model fills in are direct siblings on one top-level object —
there is no `{"decision": {...}}` envelope to get wrong. `OpenAIAgentReasoningProvider`
is entirely unaffected — it keeps using `_send_email_wire._WireReasoningResponse`
unchanged (see that module's own docstring); this module is imported by
`nerva_ai.providers.groq_agent_reasoning` alone.

## Why one merged object, not a root-level `anyOf` of two flat objects

The obvious-looking alternative — keep the discriminated union, just make
each of its two branches flat instead of wrapped — does not work here:
Groq's own Structured Outputs documentation (consulted directly during the
M7.1i audit) states the root schema passed as `json_schema.schema` must
itself be a plain `{"type": "object", ...}`, never an `anyOf` at the root.
`anyOf` is only documented as supported *inside* a property's own schema —
which, if used for a property whose two branches are themselves whole
object shapes, reproduces exactly the nested-object-selection problem this
module exists to avoid (the model would again have to correctly select and
populate one of two full object shapes, just under a different property
name instead of under `decision`).

So instead, `_GroqWireReasoningResponse` is **one** flat object whose
properties are the union of every field either variant could ever carry.
`decision` (a plain `Literal["no_action", "suggest_send_email"]`, never
nested) is the discriminator; `reason`/`confidence` are common to both
variants; `summary`/`payload`/`evidence` are `None` for `no_action` and
required (structurally enforced below, not merely by convention) for
`suggest_send_email`. Each of those three is rendered as a nullable field
(`T | None`) — JSON Schema `anyOf: [<T's schema>, {"type": "null"}]` — which
is Groq's own documented, supported pattern for an optional property; this
is a deliberately different, much narrower use of `anyOf` than the
whole-object-branch pattern that failed, and it asks the model to do the
easiest possible thing when a field does not apply: emit `null`, not
assemble a differently-shaped object.

`_check_variant_consistency` (a `model_validator`) is what actually keeps
this from being "just a dict of mostly-optional fields" — the one thing a
flat, merged schema cannot express structurally (unlike a true discriminated
union) is "exactly these fields together, never a partial mix." This
validator enforces that for both directions: a `no_action` response may not
also carry `summary`/`payload`/`evidence`, and a `suggest_send_email`
response must carry all three. A model response that is schema-valid but
mixes the two (e.g. `decision: "no_action"` with a `payload` attached) is
rejected here, before `_map_groq_wire_decision` ever runs — the same
fail-closed discipline `_send_email_wire._map_wire_decision` already applies
to its own (structurally impossible, there) mixed case.

## Still never an authorization

Exactly like `_send_email_wire._map_wire_decision`, `_map_groq_wire_decision`
only produces a `NoActionDecision`/`ActionSuggestion` — the same domain types
`OpenAIAgentReasoningProvider` produces from its own wire shape.
`nerva_core.proposal_policy.decide_proposal` remains the only authority on
whether either is ever safe to act on; nothing here, or in
`GroqAgentReasoningProvider`, checks a recipient, checks a capability, or
touches a database.

## Bounds, duplicated not imported

`_Confidence`/`_Reason`/`_Summary`/`_EvidenceLabel`/`_Subject`/`_Body` below
duplicate `_send_email_wire`'s own private constrained types field-for-field,
exactly as `_send_email_wire` itself already duplicates (rather than
imports) `nerva_ai.agent_client`'s private bounds — see that module's own
comment for why: these are each small, stable, and independently covered by
this module's own tests, so reaching across a leading-underscore module
boundary is deliberately avoided. `_GroqWireSendEmailPayload` likewise
duplicates `_WireSendEmailPayload`'s exact shape (`subject`/`body`, same
bounds) rather than importing it, for the same reason.

## No `recipients` field (M7.1l.1)

Neither this payload nor `_send_email_wire._WireSendEmailPayload` carries a
`recipients` field any more — see that module's own docstring for why
asking the model for a recipient it has no legitimate way to produce was
responsibility confusion, not a security control. A model drafts
`subject`/`body` only here too; `_map_groq_wire_decision` below produces an
`ActionSuggestion.payload` of exactly `{"subject": ..., "body": ...}`, and
a future milestone assembles the trusted recipient onto it afterward,
entirely outside this module."""

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from nerva.core.enums import ActionType
from nerva.nerva_ai.agent_client import ActionSuggestion, AgentReasoningDecision, NoActionDecision

_Confidence = Annotated[float, Field(ge=0.0, le=1.0)]
_Reason = Annotated[str, StringConstraints(min_length=1, max_length=1000, strip_whitespace=True)]
_Summary = Annotated[str, StringConstraints(min_length=1, max_length=500, strip_whitespace=True)]
_EVIDENCE_LABEL_PATTERN = r"^(message_[1-9][0-9]*|memory_[1-9][0-9]*|analysis)$"
_EvidenceLabel = Annotated[str, StringConstraints(pattern=_EVIDENCE_LABEL_PATTERN)]
_Subject = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]
_Body = Annotated[str, StringConstraints(min_length=1, max_length=20_000, strip_whitespace=True)]


class _GroqWireSendEmailPayload(BaseModel):
    """Identical shape and bounds to `_send_email_wire._WireSendEmailPayload`
    — see the module docstring's "Bounds, duplicated not imported"."""

    model_config = ConfigDict(extra="forbid")

    subject: _Subject
    body: _Body


class _GroqWireReasoningResponse(BaseModel):
    """The flat, single-object `response_format` target for Groq — see the
    module docstring's "Why one merged object" for exactly why this is not a
    discriminated union at the schema level, and `_check_variant_consistency`
    for how it still behaves like one at the validation level.

    There is deliberately no `action_type` field, exactly like
    `_send_email_wire._WireSendEmailSuggestion` — `decision ==
    "suggest_send_email"` is the only way this schema can ever express a
    candidate action, and it can only ever be `send_email`."""

    model_config = ConfigDict(extra="forbid")

    decision: Literal["no_action", "suggest_send_email"]
    reason: _Reason
    confidence: _Confidence
    summary: _Summary | None = None
    payload: _GroqWireSendEmailPayload | None = None
    evidence: Annotated[list[_EvidenceLabel], Field(min_length=1, max_length=10)] | None = None

    @model_validator(mode="after")
    def _check_variant_consistency(self) -> "_GroqWireReasoningResponse":
        if self.decision == "no_action":
            if self.summary is not None or self.payload is not None or self.evidence is not None:
                raise ValueError(
                    "A 'no_action' response must not carry summary, payload, or evidence."
                )
        elif self.summary is None or self.payload is None or self.evidence is None:
            raise ValueError(
                "A 'suggest_send_email' response must carry summary, payload, and evidence."
            )
        return self


def _map_groq_wire_decision(wire: _GroqWireReasoningResponse) -> AgentReasoningDecision:
    """The one, deterministic mapping from a validated flat Groq wire
    response to the real `AgentReasoningDecision` — mirrors
    `_send_email_wire._map_wire_decision` exactly, producing the identical
    domain types regardless of which provider's wire shape fed it. Never
    trusted as already-authorized: `nerva_core.proposal_policy.decide_proposal`
    is still unconditionally required downstream."""
    if wire.decision == "no_action":
        return NoActionDecision(reason=wire.reason, confidence=wire.confidence)
    assert wire.summary is not None
    assert wire.payload is not None
    assert wire.evidence is not None
    return ActionSuggestion(
        action_type=ActionType.SEND_EMAIL,
        summary=wire.summary,
        payload=wire.payload.model_dump(mode="json"),
        confidence=wire.confidence,
        reason=wire.reason,
        evidence=list(wire.evidence),
    )
