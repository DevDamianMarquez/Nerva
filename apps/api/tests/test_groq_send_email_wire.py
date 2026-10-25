"""`nerva_ai.providers._groq_send_email_wire` — the Groq-specific flat wire
contract (M7.1i.1). No network call, no Groq credentials: this module tests
the Pydantic contract and its deterministic mapper directly, mirroring
`test_agent_client.py`'s own discipline for the provider-neutral domain
types.

Scope: this module proves the wire contract's own validation rules and its
mapping onto `AgentReasoningDecision` — it says nothing about
`GroqAgentReasoningProvider`'s actual request/response plumbing (see
`test_groq_agent_reasoning_provider.py`) or the schema sent to Groq (see
`test_groq_agent_reasoning_schema.py`)."""

import pydantic
import pytest

from nerva.core.enums import ActionType
from nerva.nerva_ai.agent_client import ActionSuggestion, NoActionDecision
from nerva.nerva_ai.providers._groq_send_email_wire import (
    _GroqWireReasoningResponse,
    _GroqWireSendEmailPayload,
    _map_groq_wire_decision,
)


def _no_action(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "decision": "no_action",
        "reason": "Nothing warrants a reply yet.",
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return defaults


def _send_email(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "decision": "suggest_send_email",
        "summary": "Reply with pricing for 3 seats.",
        "payload": {
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        },
        "confidence": 0.8,
        "reason": "The prospect explicitly asked for pricing.",
        "evidence": ["message_1"],
    }
    defaults.update(overrides)
    return defaults


# ---------------------------------------------------------------------------
# A. valid shapes
# ---------------------------------------------------------------------------


def test_no_action_is_valid_and_flat() -> None:
    wire = _GroqWireReasoningResponse.model_validate(_no_action())

    assert wire.decision == "no_action"
    assert wire.summary is None
    assert wire.payload is None
    assert wire.evidence is None


def test_send_email_is_valid_and_flat() -> None:
    wire = _GroqWireReasoningResponse.model_validate(_send_email())

    assert wire.decision == "suggest_send_email"
    assert wire.summary == "Reply with pricing for 3 seats."
    assert isinstance(wire.payload, _GroqWireSendEmailPayload)
    assert wire.evidence == ["message_1"]


# ---------------------------------------------------------------------------
# B. closed set: no extra fields, no unknown discriminator, no other action
# ---------------------------------------------------------------------------


def test_extra_root_field_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(extra="sneaky"))


def test_extra_payload_field_is_rejected() -> None:
    payload = {
        "subject": "s",
        "body": "b",
        "extra": "sneaky",
    }
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(payload=payload))


def test_payload_rejects_a_recipients_field() -> None:
    """M7.1l.1: `recipients` does not exist in this payload at all — any
    attempt to supply one fails the same `extra="forbid"` check any other
    unknown field would, not merely a bounds check on a value."""
    payload = {"recipients": ["a@example.com"], "subject": "s", "body": "b"}
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(payload=payload))


def test_payload_has_only_subject_and_body() -> None:
    assert set(_GroqWireSendEmailPayload.model_fields) == {"subject", "body"}


def test_unknown_decision_discriminator_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(decision="suggest_create_task"))


def test_create_task_cannot_be_expressed() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(
            {"decision": "create_task", "reason": "r", "confidence": 0.5}
        )


def test_update_opportunity_stage_cannot_be_expressed() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(
            {"decision": "update_opportunity_stage", "reason": "r", "confidence": 0.5}
        )


# ---------------------------------------------------------------------------
# C. variant-consistency validator — the thing a flat merged schema cannot
#    express structurally, so this module enforces it itself
# ---------------------------------------------------------------------------


def test_no_action_with_a_payload_attached_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(payload={"subject": "s", "body": "b"}))


def test_no_action_with_evidence_attached_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(evidence=["message_1"]))


def test_no_action_with_a_summary_attached_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(summary="Reply now."))


def test_send_email_missing_payload_is_rejected() -> None:
    data = _send_email()
    del data["payload"]
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(data)


def test_send_email_missing_evidence_is_rejected() -> None:
    data = _send_email()
    del data["evidence"]
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(data)


def test_send_email_missing_summary_is_rejected() -> None:
    data = _send_email()
    del data["summary"]
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(data)


# ---------------------------------------------------------------------------
# D. bounds, preserved from the shared wire contract
# ---------------------------------------------------------------------------


def test_confidence_above_one_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(confidence=1.1))


def test_confidence_below_zero_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(confidence=-0.1))


def test_invalid_evidence_label_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(evidence=["not_a_real_label"]))


def test_empty_evidence_list_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(evidence=[]))


def test_more_than_ten_evidence_labels_is_rejected() -> None:
    labels = [f"message_{i}" for i in range(1, 12)]
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(evidence=labels))


def test_subject_over_200_chars_is_rejected() -> None:
    payload = {"subject": "x" * 201, "body": "b"}
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(payload=payload))


def test_body_over_20000_chars_is_rejected() -> None:
    payload = {"subject": "s", "body": "x" * 20_001}
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(payload=payload))


def test_reason_over_1000_chars_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_no_action(reason="x" * 1001))


def test_summary_over_500_chars_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _GroqWireReasoningResponse.model_validate(_send_email(summary="x" * 501))


# ---------------------------------------------------------------------------
# E. deterministic mapping onto the shared domain contract
# ---------------------------------------------------------------------------


def test_mapping_no_action_produces_no_action_decision() -> None:
    wire = _GroqWireReasoningResponse.model_validate(
        _no_action(reason="Already answered.", confidence=0.7)
    )

    decision = _map_groq_wire_decision(wire)

    assert isinstance(decision, NoActionDecision)
    assert decision.reason == "Already answered."
    assert decision.confidence == 0.7


def test_mapping_send_email_produces_action_suggestion() -> None:
    wire = _GroqWireReasoningResponse.model_validate(_send_email())

    decision = _map_groq_wire_decision(wire)

    assert isinstance(decision, ActionSuggestion)
    assert decision.action_type == ActionType.SEND_EMAIL
    assert decision.summary == "Reply with pricing for 3 seats."
    assert decision.payload == {
        "subject": "Pricing for 3 seats",
        "body": "Hi Priya, here is the pricing you asked about.",
    }
    assert "recipients" not in decision.payload
    assert decision.evidence == ["message_1"]
    assert decision.confidence == 0.8
    assert decision.reason == "The prospect explicitly asked for pricing."


def test_mapping_a_suggestion_always_produces_send_email_action_type() -> None:
    """There is no `action_type` field anywhere in this wire contract for a
    model to set — the mapper itself is the only place `ActionType.SEND_EMAIL`
    is ever assigned for a Groq-produced suggestion."""
    wire = _GroqWireReasoningResponse.model_validate(
        _send_email(summary="Different summary entirely.")
    )

    decision = _map_groq_wire_decision(wire)

    assert isinstance(decision, ActionSuggestion)
    assert decision.action_type is ActionType.SEND_EMAIL
