"""`nerva_core.send_email_assembly`: M7.1l.2's pure, deterministic recipient
assembler. Pure — no database, no HTTP, no provider, no network. Nothing
here wires this module into `AgentReasoningService` (M7.1l.3, not this
block); every test constructs an `ActionSuggestion` by hand, the same way
`test_proposal_policy.py` does."""

import uuid

import pytest

from nerva.core.enums import ActionType
from nerva.nerva_actions.payloads import SendEmailPayload
from nerva.nerva_ai.agent_client import ActionSuggestion
from nerva.nerva_core.proposal_fingerprint import ProposalProvenance, compute_proposal_fingerprint
from nerva.nerva_core.proposal_policy import (
    AllowedProposal,
    ProposalPolicyInput,
    ProposalPolicyReason,
    ProposalRejection,
    decide_proposal,
)
from nerva.nerva_core.send_email_assembly import (
    EmptyTrustedRecipientsError,
    assemble_trusted_recipients,
)

TRUSTED_RECIPIENT = "trusted@halcyon.example"
TRUSTED_RECIPIENTS = frozenset({TRUSTED_RECIPIENT})


def _suggestion(**overrides: object) -> ActionSuggestion:
    """Defaults match the post-M7.1l.1 model-facing contract: a `SEND_EMAIL`
    payload with `subject`/`body` only, no `recipients` at all."""
    defaults: dict[str, object] = {
        "action_type": ActionType.SEND_EMAIL,
        "summary": "Reply with pricing for 3 seats.",
        "payload": {
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        },
        "confidence": 0.8,
        "reason": "The prospect explicitly asked for pricing and has not received it yet.",
        "evidence": ["message_1"],
    }
    defaults.update(overrides)
    return ActionSuggestion.model_validate(defaults)


# ---------------------------------------------------------------------------
# 1-7. SEND_EMAIL: trusted recipient injected, everything else preserved
# ---------------------------------------------------------------------------


def test_the_trusted_recipient_is_injected_into_the_payload() -> None:
    suggestion = _suggestion()

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.payload["recipients"] == [TRUSTED_RECIPIENT]


def test_subject_is_preserved() -> None:
    suggestion = _suggestion()

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.payload["subject"] == suggestion.payload["subject"]


def test_body_is_preserved() -> None:
    suggestion = _suggestion()

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.payload["body"] == suggestion.payload["body"]


def test_summary_is_preserved() -> None:
    suggestion = _suggestion(summary="A distinct summary for this test.")

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.summary == "A distinct summary for this test."


def test_reason_is_preserved() -> None:
    suggestion = _suggestion(reason="A distinct reason for this test, long enough to pass.")

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.reason == "A distinct reason for this test, long enough to pass."


def test_confidence_is_preserved() -> None:
    suggestion = _suggestion(confidence=0.42)

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.confidence == 0.42


def test_evidence_is_preserved() -> None:
    suggestion = _suggestion(evidence=["message_1", "message_2", "analysis"])

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.evidence == ["message_1", "message_2", "analysis"]


def test_action_type_is_preserved() -> None:
    suggestion = _suggestion()

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.action_type is ActionType.SEND_EMAIL


# ---------------------------------------------------------------------------
# 8/9. Defensive overwrite: the model/attacker never gets a vote
# ---------------------------------------------------------------------------


def test_an_attacker_supplied_recipient_is_overwritten() -> None:
    suggestion = _suggestion(
        payload={
            "recipients": ["attacker@example.com"],
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        }
    )

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.payload["recipients"] == [TRUSTED_RECIPIENT]


def test_an_attacker_supplied_recipient_is_never_merged_in() -> None:
    suggestion = _suggestion(
        payload={
            "recipients": ["attacker@example.com"],
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        }
    )

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert "attacker@example.com" not in result.payload["recipients"]
    assert len(result.payload["recipients"]) == 1


# ---------------------------------------------------------------------------
# 10. Determinism: sorted, independent of frozenset iteration order
# ---------------------------------------------------------------------------


def test_multiple_trusted_recipients_are_sorted_deterministically() -> None:
    suggestion = _suggestion()
    trusted = frozenset({"b@example.com", "a@example.com", "c@example.com"})

    result = assemble_trusted_recipients(suggestion, trusted_recipients=trusted)

    assert result.payload["recipients"] == ["a@example.com", "b@example.com", "c@example.com"]


# ---------------------------------------------------------------------------
# 11/12. Empty trusted recipients: fail closed, never a fallback
# ---------------------------------------------------------------------------


def test_empty_trusted_recipients_fails_closed() -> None:
    suggestion = _suggestion()

    with pytest.raises(EmptyTrustedRecipientsError):
        assemble_trusted_recipients(suggestion, trusted_recipients=frozenset())


def test_empty_trusted_recipients_never_falls_back_to_the_model_payload() -> None:
    """Even if the (malformed, hand-built) input payload already names a
    recipient, an empty trusted set must still fail closed rather than use
    it."""
    suggestion = _suggestion(
        payload={
            "recipients": ["attacker@example.com"],
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        }
    )

    with pytest.raises(EmptyTrustedRecipientsError):
        assemble_trusted_recipients(suggestion, trusted_recipients=frozenset())


# ---------------------------------------------------------------------------
# 13/14. Immutability
# ---------------------------------------------------------------------------


def test_the_input_suggestion_is_not_mutated() -> None:
    suggestion = _suggestion()
    original_payload = dict(suggestion.payload)

    assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert suggestion.payload == original_payload
    assert "recipients" not in suggestion.payload


def test_the_output_is_a_distinct_object() -> None:
    suggestion = _suggestion()

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result is not suggestion
    assert result.payload is not suggestion.payload


# ---------------------------------------------------------------------------
# 15. Non-SEND_EMAIL: unchanged, no recipient injection
# ---------------------------------------------------------------------------


def test_a_non_send_email_suggestion_is_returned_unchanged() -> None:
    suggestion = _suggestion(action_type=ActionType.UPDATE_OPPORTUNITY_STAGE, payload={})

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result is suggestion
    assert "recipients" not in result.payload


def test_a_non_send_email_suggestion_with_empty_trusted_recipients_does_not_raise() -> None:
    """Recipient authority is a `SEND_EMAIL`-specific concept — an empty
    trusted set must never fail-close an action type that has no recipient
    at all."""
    suggestion = _suggestion(action_type=ActionType.UPDATE_OPPORTUNITY_STAGE, payload={})

    result = assemble_trusted_recipients(suggestion, trusted_recipients=frozenset())

    assert result is suggestion


# ---------------------------------------------------------------------------
# 16/17. Validation boundary: malformed input survives assembly unchecked
# ---------------------------------------------------------------------------


def test_a_malformed_subject_and_body_survive_assembly_unchecked() -> None:
    """`subject`/`body` are not validated here at all — any contract they
    must satisfy (length, non-empty) is `SendEmailPayload`'s job, downstream,
    never this module's."""
    suggestion = _suggestion(payload={"subject": "", "body": ""})

    result = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert result.payload["subject"] == ""
    assert result.payload["body"] == ""


def test_a_malformed_trusted_recipient_survives_assembly_unchecked() -> None:
    """A syntactically invalid address is not this module's concern either —
    `SendEmailPayload.recipients` (an `EmailStr`) is what must eventually
    reject it."""
    suggestion = _suggestion()
    malformed_trusted = frozenset({"not-an-email-address"})

    result = assemble_trusted_recipients(suggestion, trusted_recipients=malformed_trusted)

    assert result.payload["recipients"] == ["not-an-email-address"]


# ---------------------------------------------------------------------------
# 18. Repeated calls with the same input produce equal output
# ---------------------------------------------------------------------------


def test_repeated_calls_with_the_same_input_produce_equal_output() -> None:
    suggestion = _suggestion()

    first = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)
    second = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    assert first == second
    assert first is not second


# ---------------------------------------------------------------------------
# ProposalPolicy compatibility (§12): assembler -> decide_proposal
# ---------------------------------------------------------------------------


def test_assembled_suggestion_with_the_trusted_recipient_is_allowed_by_policy() -> None:
    suggestion = _suggestion()
    assembled = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    result = decide_proposal(
        ProposalPolicyInput(
            decision=assembled,
            declared_capabilities=frozenset({ActionType.SEND_EMAIL}),
            available_evidence_labels=frozenset({"message_1"}),
            allowed_recipients=TRUSTED_RECIPIENTS,
        )
    )

    assert isinstance(result, AllowedProposal)
    assert result.policy_reason is ProposalPolicyReason.ALLOWED
    assert isinstance(result.validated_payload, SendEmailPayload)
    assert result.validated_payload.recipients == [TRUSTED_RECIPIENT]


def test_an_attacker_recipient_never_survives_through_to_policy() -> None:
    """A fake/model suggestion naming `attacker@example.com` is overwritten
    by the assembler before `ProposalPolicy` ever sees it — the attacker
    address never reaches, and is never evaluated by, `decide_proposal`."""
    suggestion = _suggestion(
        payload={
            "recipients": ["attacker@example.com"],
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        }
    )
    assembled = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    result = decide_proposal(
        ProposalPolicyInput(
            decision=assembled,
            declared_capabilities=frozenset({ActionType.SEND_EMAIL}),
            available_evidence_labels=frozenset({"message_1"}),
            allowed_recipients=TRUSTED_RECIPIENTS,
        )
    )

    assert isinstance(result, AllowedProposal)
    assert result.validated_payload.recipients == [TRUSTED_RECIPIENT]
    assert "attacker@example.com" not in result.validated_payload.recipients


def test_an_attacker_recipient_still_cannot_force_a_mismatch_bypass() -> None:
    """Even if `allowed_recipients` (the policy's own trusted set, resolved
    independently) differs from what the assembler used, the attacker
    address still never appears anywhere `ProposalPolicy` evaluates — the
    mismatch is against the assembled, trusted value, not the discarded one."""
    suggestion = _suggestion(
        payload={
            "recipients": ["attacker@example.com"],
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        }
    )
    assembled = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    result = decide_proposal(
        ProposalPolicyInput(
            decision=assembled,
            declared_capabilities=frozenset({ActionType.SEND_EMAIL}),
            available_evidence_labels=frozenset({"message_1"}),
            allowed_recipients=frozenset({"someone-else@halcyon.example"}),
        )
    )

    assert isinstance(result, ProposalRejection)
    assert result.reason is ProposalPolicyReason.RECIPIENT_MISMATCH


# ---------------------------------------------------------------------------
# Fingerprint compatibility (§13): assembler -> policy -> fingerprint
# ---------------------------------------------------------------------------


def test_the_assembled_trusted_recipient_reaches_the_proposal_fingerprint() -> None:
    suggestion = _suggestion()
    assembled = assemble_trusted_recipients(suggestion, trusted_recipients=TRUSTED_RECIPIENTS)

    allowed = decide_proposal(
        ProposalPolicyInput(
            decision=assembled,
            declared_capabilities=frozenset({ActionType.SEND_EMAIL}),
            available_evidence_labels=frozenset({"message_1"}),
            allowed_recipients=TRUSTED_RECIPIENTS,
        )
    )
    assert isinstance(allowed, AllowedProposal)

    provenance = ProposalProvenance(
        conversation_id=uuid.uuid4(),
        analysis_id=uuid.uuid4(),
        source_fingerprint="fp-reasoning-state-1",
        reasoning_version="test-reasoning-v0",
        proposal_policy_version="v1",
    )
    fingerprint_with_trusted = compute_proposal_fingerprint(provenance=provenance, allowed=allowed)

    other_assembled = assemble_trusted_recipients(
        suggestion, trusted_recipients=frozenset({"someone-else@halcyon.example"})
    )
    other_allowed = decide_proposal(
        ProposalPolicyInput(
            decision=other_assembled,
            declared_capabilities=frozenset({ActionType.SEND_EMAIL}),
            available_evidence_labels=frozenset({"message_1"}),
            allowed_recipients=frozenset({"someone-else@halcyon.example"}),
        )
    )
    assert isinstance(other_allowed, AllowedProposal)
    fingerprint_with_other = compute_proposal_fingerprint(
        provenance=provenance, allowed=other_allowed
    )

    assert fingerprint_with_trusted != fingerprint_with_other
