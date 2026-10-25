"""`nerva_core.agent_reasoning_fingerprint`: M7.1d's identity of one reasoning
state. Pure — no database, no LLM, no provider, no orchestration. Mirrors
`test_proposal_fingerprint.py`'s structure for the same kind of question one
level up the pipeline: what must, and must not, change a digest.

Every "changes the fingerprint" test below is the real guarantee the loop
depends on: a reasoning state the reasoner would plausibly decide differently
on must not collide with an earlier one, or M7.1c's duplicate suppression
would silently discard the second, genuinely-different proposal."""

import ast
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import nerva.nerva_core.agent_reasoning_fingerprint as fingerprint_module
from nerva.core.enums import (
    ActionType,
    ConversationCategory,
    ConversationPriority,
    MemoryKind,
    MemoryOrigin,
    MessageDirection,
    SentimentLabel,
)
from nerva.nerva_ai.agent_client import AgentReasoningInput, ReasoningAnalysisContext
from nerva.nerva_ai.client import AnalysisMemoryContextItem, AnalysisMessage
from nerva.nerva_ai.schemas import DeadlineEstimate, MonetaryEstimate
from nerva.nerva_core.agent_reasoning_fingerprint import (
    AGENT_REASONING_INPUT_VERSION,
    compute_agent_reasoning_fingerprint,
)
from nerva.nerva_core.memory_context import MemoryScope

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)


def _analysis(**overrides: object) -> ReasoningAnalysisContext:
    defaults: dict[str, object] = {
        "category": ConversationCategory.SALES_LEAD,
        "priority": ConversationPriority.MEDIUM,
        "sentiment": None,
        "requires_response": True,
        "commercial_signal": True,
        "confidence": 0.8,
        "summary": "Prospect asked about pricing for 3 seats.",
        "intent": "request_quote",
        "suggested_next_step": "Send pricing information.",
        "estimated_value": None,
        "deadline": None,
    }
    defaults.update(overrides)
    return ReasoningAnalysisContext(**defaults)  # type: ignore[arg-type]


def _message(**overrides: object) -> AnalysisMessage:
    defaults: dict[str, object] = {
        "direction": MessageDirection.INBOUND,
        "sender_label": "Priya Nair",
        "sent_at": NOW,
        "body_text": "Can you send pricing for 3 seats?",
    }
    defaults.update(overrides)
    return AnalysisMessage(**defaults)  # type: ignore[arg-type]


def _memory(**overrides: object) -> AnalysisMemoryContextItem:
    defaults: dict[str, object] = {
        "statement": "Prefers email over calls.",
        "kind": MemoryKind.PREFERENCE,
        "origin": MemoryOrigin.OBSERVED,
        "confidence": None,
        "observed_at": NOW,
        "scope": MemoryScope.CONTACT,
    }
    defaults.update(overrides)
    return AnalysisMemoryContextItem(**defaults)  # type: ignore[arg-type]


def _request(**overrides: object) -> AgentReasoningInput:
    defaults: dict[str, object] = {
        "subject": "Pricing question",
        "contact_display_name": "Priya Nair",
        "messages": (_message(),),
        "analysis": _analysis(),
        "memory_context": (_memory(),),
        "capabilities": (ActionType.SEND_EMAIL,),
    }
    defaults.update(overrides)
    return AgentReasoningInput(**defaults)  # type: ignore[arg-type]


def _fp(**overrides: object) -> str:
    return compute_agent_reasoning_fingerprint(_request(**overrides))


# ---------------------------------------------------------------------------
# A. Determinism and shape
# ---------------------------------------------------------------------------


def test_the_same_input_always_produces_the_same_fingerprint() -> None:
    assert _fp() == _fp()
    assert len(_fp()) == 64


def test_the_fingerprint_is_a_stable_hex_digest_not_pythons_builtin_hash() -> None:
    """`hash()` is salted per process (`PYTHONHASHSEED`) and could never
    produce this shape — the same check `test_proposal_fingerprint.py` makes
    for the same reason."""
    fingerprint = _fp()

    assert len(fingerprint) == 64
    assert all(character in "0123456789abcdef" for character in fingerprint)


def test_two_independently_built_but_identical_inputs_agree() -> None:
    """Nothing object-identity-based (an `id()`, a `dataclass` instance's
    address) participates: two separately-constructed but equal inputs must
    be the same reasoning state."""
    assert compute_agent_reasoning_fingerprint(_request()) == compute_agent_reasoning_fingerprint(
        _request()
    )


# ---------------------------------------------------------------------------
# B. Conversation content (requirement 14)
# ---------------------------------------------------------------------------


def test_a_different_subject_changes_the_fingerprint() -> None:
    assert _fp(subject="Pricing question") != _fp(subject="Renewal question")


def test_a_subject_of_none_is_not_the_same_as_an_empty_subject() -> None:
    """`nerva.services.analysis.compute_fingerprint` collapses these two with
    `subject or ""`; this module deliberately does not — see its docstring."""
    assert _fp(subject=None) != _fp(subject="")


def test_a_different_message_body_changes_the_fingerprint() -> None:
    assert _fp(messages=(_message(body_text="a"),)) != _fp(messages=(_message(body_text="b"),))


def test_a_different_sender_label_changes_the_fingerprint() -> None:
    assert _fp(messages=(_message(sender_label="Priya"),)) != _fp(
        messages=(_message(sender_label="You"),)
    )


def test_a_different_message_direction_changes_the_fingerprint() -> None:
    assert _fp(messages=(_message(direction=MessageDirection.INBOUND),)) != _fp(
        messages=(_message(direction=MessageDirection.OUTBOUND),)
    )


def test_a_different_sent_at_changes_the_fingerprint() -> None:
    """`sent_at` is content — *which* message this is — never a "now"."""
    assert _fp(messages=(_message(sent_at=NOW),)) != _fp(
        messages=(_message(sent_at=datetime(2026, 1, 16, 12, 0, tzinfo=UTC)),)
    )


def test_an_added_message_changes_the_fingerprint() -> None:
    assert _fp(messages=(_message(),)) != _fp(messages=(_message(), _message(body_text="More.")))


def test_message_order_changes_the_fingerprint() -> None:
    """Messages are a sequence, not a set: a thread read in a different order
    is a different thing to reason about."""
    first, second = _message(body_text="a"), _message(body_text="b")

    assert _fp(messages=(first, second)) != _fp(messages=(second, first))


def test_no_messages_at_all_still_fingerprints() -> None:
    assert len(_fp(messages=())) == 64


# ---------------------------------------------------------------------------
# C. Analysis content (requirement 15)
# ---------------------------------------------------------------------------


def test_a_different_analysis_category_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(category=ConversationCategory.SALES_LEAD)) != _fp(
        analysis=_analysis(category=ConversationCategory.BILLING)
    )


def test_a_different_analysis_priority_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(priority=ConversationPriority.LOW)) != _fp(
        analysis=_analysis(priority=ConversationPriority.URGENT)
    )


def test_a_different_analysis_sentiment_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(sentiment=None)) != _fp(
        analysis=_analysis(sentiment=SentimentLabel.NEGATIVE)
    )


def test_a_different_requires_response_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(requires_response=True)) != _fp(
        analysis=_analysis(requires_response=False)
    )


def test_a_different_commercial_signal_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(commercial_signal=True)) != _fp(
        analysis=_analysis(commercial_signal=False)
    )


def test_a_different_analysis_confidence_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(confidence=0.8)) != _fp(analysis=_analysis(confidence=0.4))


def test_a_different_analysis_summary_changes_the_fingerprint() -> None:
    """Unlike `nerva_core.proposal_fingerprint`, prose is included here: an
    analysis summary is *input* a reasoner reads, not an explanation of an
    already-identified action."""
    assert _fp(analysis=_analysis(summary="Asked for pricing.")) != _fp(
        analysis=_analysis(summary="Asked to cancel.")
    )


def test_a_different_analysis_intent_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(intent="request_quote")) != _fp(
        analysis=_analysis(intent="cancel_subscription")
    )


def test_a_different_suggested_next_step_changes_the_fingerprint() -> None:
    assert _fp(analysis=_analysis(suggested_next_step="Send pricing.")) != _fp(
        analysis=_analysis(suggested_next_step="Escalate to support.")
    )


def test_an_added_estimated_value_changes_the_fingerprint() -> None:
    estimate = MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True)

    assert _fp(analysis=_analysis(estimated_value=None)) != _fp(
        analysis=_analysis(estimated_value=estimate)
    )


def test_a_different_estimated_value_amount_changes_the_fingerprint() -> None:
    low = MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True)
    high = MonetaryEstimate(amount_cents=900_000, currency="USD", is_explicit=True)

    assert _fp(analysis=_analysis(estimated_value=low)) != _fp(
        analysis=_analysis(estimated_value=high)
    )


def test_a_different_estimated_value_currency_changes_the_fingerprint() -> None:
    usd = MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True)
    eur = MonetaryEstimate(amount_cents=150_000, currency="EUR", is_explicit=True)

    assert _fp(analysis=_analysis(estimated_value=usd)) != _fp(
        analysis=_analysis(estimated_value=eur)
    )


def test_a_different_estimated_value_explicitness_changes_the_fingerprint() -> None:
    explicit = MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True)
    inferred = MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=False)

    assert _fp(analysis=_analysis(estimated_value=explicit)) != _fp(
        analysis=_analysis(estimated_value=inferred)
    )


def test_an_added_deadline_changes_the_fingerprint() -> None:
    deadline = DeadlineEstimate(date=date(2026, 2, 1), is_explicit=True)

    assert _fp(analysis=_analysis(deadline=None)) != _fp(analysis=_analysis(deadline=deadline))


def test_a_different_deadline_date_changes_the_fingerprint() -> None:
    early = DeadlineEstimate(date=date(2026, 2, 1), is_explicit=True)
    late = DeadlineEstimate(date=date(2026, 3, 1), is_explicit=True)

    assert _fp(analysis=_analysis(deadline=early)) != _fp(analysis=_analysis(deadline=late))


def test_a_different_deadline_explicitness_changes_the_fingerprint() -> None:
    explicit = DeadlineEstimate(date=date(2026, 2, 1), is_explicit=True)
    inferred = DeadlineEstimate(date=date(2026, 2, 1), is_explicit=False)

    assert _fp(analysis=_analysis(deadline=explicit)) != _fp(analysis=_analysis(deadline=inferred))


# ---------------------------------------------------------------------------
# D. Memory context (requirement 13)
# ---------------------------------------------------------------------------


def test_a_different_memory_statement_changes_the_fingerprint() -> None:
    assert _fp(memory_context=(_memory(statement="a"),)) != _fp(
        memory_context=(_memory(statement="b"),)
    )


def test_an_added_memory_changes_the_fingerprint() -> None:
    """The whole reason this module exists rather than reusing
    `ConversationAnalysis.input_fingerprint`: the same analysis with new
    memory is a different reasoning state."""
    assert _fp(memory_context=()) != _fp(memory_context=(_memory(),))


def test_a_removed_memory_changes_the_fingerprint() -> None:
    assert _fp(memory_context=(_memory(), _memory(statement="Second."))) != _fp(
        memory_context=(_memory(),)
    )


def test_memory_order_changes_the_fingerprint() -> None:
    """Memory reaches a reasoner already ranked (Block 1's selector is the
    sole authority on order); a different order is a different context."""
    first, second = _memory(statement="a"), _memory(statement="b")

    assert _fp(memory_context=(first, second)) != _fp(memory_context=(second, first))


def test_a_different_memory_kind_changes_the_fingerprint() -> None:
    assert _fp(memory_context=(_memory(kind=MemoryKind.PREFERENCE),)) != _fp(
        memory_context=(_memory(kind=MemoryKind.FACT),)
    )


def test_a_different_memory_origin_changes_the_fingerprint() -> None:
    assert _fp(memory_context=(_memory(origin=MemoryOrigin.OBSERVED),)) != _fp(
        memory_context=(_memory(origin=MemoryOrigin.INFERRED, confidence=0.7),)
    )


def test_a_different_memory_confidence_changes_the_fingerprint() -> None:
    assert _fp(memory_context=(_memory(origin=MemoryOrigin.INFERRED, confidence=0.7),)) != _fp(
        memory_context=(_memory(origin=MemoryOrigin.INFERRED, confidence=0.3),)
    )


def test_a_different_memory_observed_at_changes_the_fingerprint() -> None:
    assert _fp(memory_context=(_memory(observed_at=NOW),)) != _fp(
        memory_context=(_memory(observed_at=datetime(2025, 6, 1, tzinfo=UTC)),)
    )


def test_a_different_memory_scope_changes_the_fingerprint() -> None:
    assert _fp(memory_context=(_memory(scope=MemoryScope.CONTACT),)) != _fp(
        memory_context=(_memory(scope=MemoryScope.WORKSPACE),)
    )


def test_an_empty_memory_context_still_fingerprints() -> None:
    assert len(_fp(memory_context=())) == 64


# ---------------------------------------------------------------------------
# E. Contact identity (requirement 17)
# ---------------------------------------------------------------------------


def test_a_different_contact_display_name_changes_the_fingerprint() -> None:
    assert _fp(contact_display_name="Priya Nair") != _fp(contact_display_name="Priya N.")


def test_a_contact_display_name_of_none_is_not_the_same_as_an_empty_one() -> None:
    assert _fp(contact_display_name=None) != _fp(contact_display_name="")


# ---------------------------------------------------------------------------
# F. Capabilities (requirement 16)
# ---------------------------------------------------------------------------


def test_different_capabilities_change_the_fingerprint() -> None:
    assert _fp(capabilities=(ActionType.SEND_EMAIL,)) != _fp(capabilities=(ActionType.CREATE_TASK,))


def test_an_added_capability_changes_the_fingerprint() -> None:
    assert _fp(capabilities=(ActionType.SEND_EMAIL,)) != _fp(
        capabilities=(ActionType.SEND_EMAIL, ActionType.CREATE_TASK)
    )


def test_no_capabilities_at_all_changes_the_fingerprint() -> None:
    assert _fp(capabilities=()) != _fp(capabilities=(ActionType.SEND_EMAIL,))


def test_capability_order_does_not_change_the_fingerprint() -> None:
    """`capabilities` is conceptually the *set* on offer (see
    `AgentReasoningInput.capabilities`' own docstring), so the order a caller
    happened to list them in is not part of the reasoning state."""
    assert _fp(capabilities=(ActionType.SEND_EMAIL, ActionType.CREATE_TASK)) == _fp(
        capabilities=(ActionType.CREATE_TASK, ActionType.SEND_EMAIL)
    )


def test_a_repeated_capability_does_not_change_the_fingerprint() -> None:
    assert _fp(capabilities=(ActionType.SEND_EMAIL, ActionType.SEND_EMAIL)) == _fp(
        capabilities=(ActionType.SEND_EMAIL,)
    )


# ---------------------------------------------------------------------------
# G. Versioning
# ---------------------------------------------------------------------------


def test_the_input_version_is_not_called_a_prompt_version() -> None:
    """The whole point of the M7.1c `reasoning_prompt_version` ->
    `reasoning_version` correction: nothing in this codebase claims to
    version a prompt that does not exist."""
    assert AGENT_REASONING_INPUT_VERSION == "v1"
    assert not any(
        "prompt" in name.lower() for name in vars(fingerprint_module) if not name.startswith("_")
    )


def test_a_changed_input_version_changes_every_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Folded into the hash as a plain field, so old and new digests cannot
    collide silently if this contract is ever revised."""
    before = _fp()
    monkeypatch.setattr(fingerprint_module, "AGENT_REASONING_INPUT_VERSION", "v2")

    assert _fp() != before


def test_a_changed_reasoning_schema_version_changes_every_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _fp()
    monkeypatch.setattr(fingerprint_module, "REASONING_SCHEMA_VERSION", 99)

    assert _fp() != before


def test_a_changed_fingerprint_schema_version_changes_every_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = _fp()
    monkeypatch.setattr(fingerprint_module, "AGENT_REASONING_FINGERPRINT_SCHEMA_VERSION", 2)

    assert _fp() != before


# ---------------------------------------------------------------------------
# H. Purity boundary
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORT_PREFIXES = (
    "nerva.services",
    "nerva.repositories",
    "nerva.routes",
    "nerva.api",
    "nerva.db",
    "nerva.models",
    "nerva.nerva_connect",
    "sqlalchemy",
    "fastapi",
    "httpx",
    "openai",
    "requests",
    "google",
)


def _imported_module_names(source_path: Path) -> set[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_the_fingerprint_module_never_imports_services_db_or_transport() -> None:
    source_path = Path(fingerprint_module.__file__)
    imported = _imported_module_names(source_path)

    for forbidden in FORBIDDEN_IMPORT_PREFIXES:
        offenders = {
            name for name in imported if name == forbidden or name.startswith(forbidden + ".")
        }
        assert not offenders, f"{source_path.name} imports {offenders}"
