"""`nerva_core.proposal_fingerprint`: M7.1c's deterministic proposal
identity. Pure — no database, no HTTP. Mirrors `test_analysis_input.py`'s
`compute_fingerprint` tests for the same reasons."""

import uuid

import pytest

from nerva.core.enums import ActionType
from nerva.nerva_actions.payloads import SendEmailPayload
from nerva.nerva_core.proposal_fingerprint import ProposalProvenance, compute_proposal_fingerprint
from nerva.nerva_core.proposal_policy import AllowedProposal

CONVERSATION_ID = uuid.uuid4()
ANALYSIS_ID = uuid.uuid4()


def _payload(**overrides: object) -> SendEmailPayload:
    defaults: dict[str, object] = {
        "recipients": ["priya@halcyon.example"],
        "subject": "Pricing for 3 seats",
        "body": "Hi Priya, here is the pricing you asked about.",
    }
    defaults.update(overrides)
    return SendEmailPayload.model_validate(defaults)


def _allowed(**overrides: object) -> AllowedProposal:
    defaults: dict[str, object] = {
        "action_type": ActionType.SEND_EMAIL,
        "summary": "Reply with pricing for 3 seats.",
        "validated_payload": _payload(),
        "reason": "The prospect asked for pricing.",
        "confidence": 0.8,
        "grounded_evidence": ("message_1",),
    }
    defaults.update(overrides)
    return AllowedProposal(**defaults)  # type: ignore[arg-type]


def _provenance(**overrides: object) -> ProposalProvenance:
    defaults: dict[str, object] = {
        "conversation_id": CONVERSATION_ID,
        "analysis_id": ANALYSIS_ID,
        "source_fingerprint": "fp-reasoning-state-1",
        "reasoning_version": "test-reasoning-v0",
        "proposal_policy_version": "v1",
    }
    defaults.update(overrides)
    return ProposalProvenance(**defaults)  # type: ignore[arg-type]


def _fp(
    allowed: AllowedProposal | None = None, provenance: ProposalProvenance | None = None
) -> str:
    return compute_proposal_fingerprint(
        provenance=provenance or _provenance(), allowed=allowed or _allowed()
    )


# ---------------------------------------------------------------------------
# 1/15. Determinism, no Python hash()/randomness
# ---------------------------------------------------------------------------


def test_the_same_input_always_produces_the_same_fingerprint() -> None:
    first = _fp()
    second = _fp()

    assert first == second
    assert len(first) == 64  # sha256 hex digest, same shape as other fingerprints in this repo


def test_the_fingerprint_is_not_pythons_builtin_hash() -> None:
    """`hash()` is salted per-process by default (`PYTHONHASHSEED`) and would
    make this fingerprint non-reproducible across runs — confirmed here by
    simply checking the result is a stable 64-char hex digest, the shape
    `hash()` (a signed machine int) could never produce."""
    fingerprint = _fp()

    assert len(fingerprint) == 64
    assert all(ch in "0123456789abcdef" for ch in fingerprint)


# ---------------------------------------------------------------------------
# 2. Dict key order never matters (json.dumps(sort_keys=True))
# ---------------------------------------------------------------------------


def test_dict_key_construction_order_does_not_change_the_fingerprint() -> None:
    a = ProposalProvenance(
        conversation_id=CONVERSATION_ID,
        analysis_id=ANALYSIS_ID,
        source_fingerprint="fp-1",
        reasoning_version="v0",
        proposal_policy_version="v1",
    )
    b = ProposalProvenance(
        proposal_policy_version="v1",
        reasoning_version="v0",
        source_fingerprint="fp-1",
        analysis_id=ANALYSIS_ID,
        conversation_id=CONVERSATION_ID,
    )

    assert _fp(provenance=a) == _fp(provenance=b)


# ---------------------------------------------------------------------------
# 3/4. Recipient case and order equivalence
# ---------------------------------------------------------------------------


def test_recipient_case_does_not_change_the_fingerprint() -> None:
    """`SendEmailPayload` already lower-cases recipients at validation time,
    so two payloads built from differently-cased raw input are already
    identical by the time they reach this module — this test pins that
    chain end to end, not just this module's own behavior."""
    lower = _allowed(validated_payload=_payload(recipients=["priya@halcyon.example"]))
    upper = _allowed(validated_payload=_payload(recipients=["PRIYA@HALCYON.EXAMPLE"]))

    assert _fp(lower) == _fp(upper)


def test_recipient_list_order_does_not_change_the_fingerprint() -> None:
    forward = _allowed(
        validated_payload=_payload(recipients=["a@halcyon.example", "b@halcyon.example"])
    )
    backward = _allowed(
        validated_payload=_payload(recipients=["b@halcyon.example", "a@halcyon.example"])
    )

    assert _fp(forward) == _fp(backward)


def test_duplicate_recipients_do_not_change_the_fingerprint() -> None:
    single = _allowed(validated_payload=_payload(recipients=["priya@halcyon.example"]))
    duplicated = _allowed(
        validated_payload=_payload(recipients=["priya@halcyon.example", "priya@halcyon.example"])
    )

    assert _fp(single) == _fp(duplicated)


# ---------------------------------------------------------------------------
# 5. Reasoning-state identity changes the fingerprint
# ---------------------------------------------------------------------------


def test_a_different_source_fingerprint_changes_the_fingerprint() -> None:
    a = _provenance(source_fingerprint="fp-1")
    b = _provenance(source_fingerprint="fp-2")

    assert _fp(provenance=a) != _fp(provenance=b)


def test_a_different_conversation_id_changes_the_fingerprint() -> None:
    """Included even though `source_fingerprint` alone is, in practice,
    already specific enough — see the module docstring for why two
    different conversations with a coincidentally-identical
    `source_fingerprint` must still never collide."""
    a = _provenance(conversation_id=uuid.uuid4())
    b = _provenance(conversation_id=uuid.uuid4())

    assert _fp(provenance=a) != _fp(provenance=b)


def test_a_different_analysis_id_changes_the_fingerprint() -> None:
    a = _provenance(analysis_id=uuid.uuid4())
    b = _provenance(analysis_id=uuid.uuid4())

    assert _fp(provenance=a) != _fp(provenance=b)


# ---------------------------------------------------------------------------
# 6/7. The effective action changes the fingerprint
# ---------------------------------------------------------------------------


def test_a_different_body_changes_the_fingerprint() -> None:
    a = _allowed(validated_payload=_payload(body="Hello"))
    b = _allowed(validated_payload=_payload(body="Hello!"))

    assert _fp(a) != _fp(b)


def test_a_different_subject_changes_the_fingerprint() -> None:
    a = _allowed(validated_payload=_payload(subject="Pricing"))
    b = _allowed(validated_payload=_payload(subject="Pricing information"))

    assert _fp(a) != _fp(b)


def test_a_different_recipient_set_changes_the_fingerprint() -> None:
    a = _allowed(validated_payload=_payload(recipients=["priya@halcyon.example"]))
    b = _allowed(validated_payload=_payload(recipients=["someone-else@halcyon.example"]))

    assert _fp(a) != _fp(b)


# ---------------------------------------------------------------------------
# 8. ActionType changes the fingerprint (where the helper supports it)
# ---------------------------------------------------------------------------


def test_a_different_action_type_changes_the_fingerprint() -> None:
    """`ProposalPolicy` only ever allows `SEND_EMAIL` through today, so this
    exercises `compute_proposal_fingerprint` directly with a value it would
    never actually see end to end — only to confirm `action_type` itself
    genuinely participates in the hash input, independent of the payload."""
    email_type = _allowed(action_type=ActionType.SEND_EMAIL)
    different_type = _allowed(action_type=ActionType.CREATE_TASK)

    assert _fp(email_type) != _fp(different_type)


# ---------------------------------------------------------------------------
# 9/10. Versions change the fingerprint
# ---------------------------------------------------------------------------


def test_a_different_reasoning_version_changes_the_fingerprint() -> None:
    a = _provenance(reasoning_version="test-reasoning-v0")
    b = _provenance(reasoning_version="test-reasoning-v1")

    assert _fp(provenance=a) != _fp(provenance=b)


def test_a_different_proposal_policy_version_changes_the_fingerprint() -> None:
    a = _provenance(proposal_policy_version="v1")
    b = _provenance(proposal_policy_version="v2")

    assert _fp(provenance=a) != _fp(provenance=b)


def test_a_different_provider_prompt_version_does_not_change_the_fingerprint() -> None:
    """M7.1e: unlike `reasoning_version`/`proposal_policy_version` just
    above, `provider_prompt_version` is provenance/audit metadata only — see
    `nerva_core.proposal_fingerprint`'s own "provider_prompt_version" section
    for why two runs that reason to the exact same email, under the exact
    same policy, must collide into one proposal regardless of which wording
    of a provider's prompt produced it."""
    a = _provenance(provider_prompt_version="v1")
    b = _provenance(provider_prompt_version="v2")

    assert _fp(provenance=a) == _fp(provenance=b)


def test_a_none_provider_prompt_version_does_not_change_the_fingerprint_either() -> None:
    """A fake/unconfigured provider (`provider_prompt_version=None`, the
    dataclass default) and a real one must fingerprint identically for the
    same reasoning state — this field never participates in the hash at
    all, present or absent."""
    a = _provenance(provider_prompt_version=None)
    b = _provenance(provider_prompt_version="v1")

    assert _fp(provenance=a) == _fp(provenance=b)


# ---------------------------------------------------------------------------
# 11-14. Prose/explanatory fields never change the fingerprint
# ---------------------------------------------------------------------------


def test_a_different_confidence_does_not_change_the_fingerprint() -> None:
    a = _allowed(confidence=0.9)
    b = _allowed(confidence=0.1)

    assert _fp(a) == _fp(b)


def test_a_different_reason_does_not_change_the_fingerprint() -> None:
    a = _allowed(reason="Because the prospect asked for pricing.")
    b = _allowed(reason="Because it seemed like a good idea.")

    assert _fp(a) == _fp(b)


def test_a_different_summary_does_not_change_the_fingerprint() -> None:
    a = _allowed(summary="Reply with pricing.")
    b = _allowed(summary="Send the pricing information requested.")

    assert _fp(a) == _fp(b)


def test_evidence_ordering_does_not_change_the_fingerprint() -> None:
    a = _allowed(grounded_evidence=("message_1", "memory_1"))
    b = _allowed(grounded_evidence=("memory_1", "message_1"))

    assert _fp(a) == _fp(b)


def test_evidence_content_is_entirely_excluded_from_the_fingerprint() -> None:
    """Deliberately broader than the one explicit instruction (only ordering
    must not matter): the *content* of grounded_evidence is excluded
    entirely, because citing a different message in support of the exact
    same effective action does not make it a different action — see the
    module docstring."""
    a = _allowed(grounded_evidence=("message_1",))
    b = _allowed(grounded_evidence=("message_2", "analysis"))

    assert _fp(a) == _fp(b)


# ---------------------------------------------------------------------------
# Payload type exhaustiveness
# ---------------------------------------------------------------------------


def test_an_unsupported_payload_type_raises_rather_than_silently_hashing_something() -> None:
    from nerva.core.enums import OpportunityStage
    from nerva.nerva_actions.payloads import UpdateOpportunityStagePayload

    unsupported = _allowed(
        action_type=ActionType.UPDATE_OPPORTUNITY_STAGE,
        validated_payload=UpdateOpportunityStagePayload(target_stage=OpportunityStage.QUALIFIED),
    )

    with pytest.raises(TypeError):
        _fp(unsupported)
