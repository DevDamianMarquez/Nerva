"""`nerva_ai.agent_client`: M7.1a's pure reasoning contracts. No database, no
LLM, no OpenAI — `FakeAgentReasoningClient` (`tests.fakes.agent_reasoning`)
stands in for the Protocol, the same way `FakeAIClient` already does for
`AIAnalysisClient`. Nothing here exercises `ProposalPolicy`, `ActionService`,
or any orchestration — none of that exists yet (M7.1b+)."""

import ast
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import pydantic
import pytest

import nerva.nerva_ai.agent_client as agent_client_module
from nerva.core.enums import (
    ActionType,
    ConversationCategory,
    ConversationPriority,
    MemoryKind,
    MemoryOrigin,
    MessageDirection,
)
from nerva.nerva_ai.agent_client import (
    REASONING_SCHEMA_VERSION,
    ActionSuggestion,
    AgentReasoningClient,
    AgentReasoningDecision,
    AgentReasoningInput,
    AgentReasoningOutcome,
    NoActionDecision,
    ReasoningAnalysisContext,
)
from nerva.nerva_ai.client import AnalysisMemoryContextItem, AnalysisMessage
from nerva.nerva_ai.errors import AIProviderError
from nerva.nerva_core.memory_context import MemoryScope
from tests.fakes.agent_reasoning import FakeAgentReasoningClient, make_no_action_decision

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)

SOME_ANALYSIS = ReasoningAnalysisContext(
    category=ConversationCategory.SALES_LEAD,
    priority=ConversationPriority.MEDIUM,
    sentiment=None,
    requires_response=True,
    commercial_signal=True,
    confidence=0.8,
    summary="Prospect asked about pricing for 3 seats.",
    intent="request_quote",
    suggested_next_step="Send pricing information.",
    estimated_value=None,
    deadline=None,
)

SOME_MESSAGE = AnalysisMessage(
    direction=MessageDirection.INBOUND,
    sender_label="Priya Nair",
    sent_at=NOW,
    body_text="Can we meet next week?",
)

SOME_MEMORY_ITEM = AnalysisMemoryContextItem(
    statement="Client prefers afternoon meetings.",
    kind=MemoryKind.PREFERENCE,
    origin=MemoryOrigin.OBSERVED,
    confidence=None,
    observed_at=NOW,
    scope=MemoryScope.CONTACT,
)


def _input(**overrides: object) -> AgentReasoningInput:
    defaults: dict[str, object] = {
        "subject": "Pricing for 3 seats",
        "contact_display_name": "Priya Nair",
        "messages": (SOME_MESSAGE,),
        "analysis": SOME_ANALYSIS,
    }
    defaults.update(overrides)
    return AgentReasoningInput(**defaults)  # type: ignore[arg-type]


def _suggestion(**overrides: object) -> ActionSuggestion:
    defaults: dict[str, object] = {
        "action_type": ActionType.SEND_EMAIL,
        "summary": "Reply with pricing for 3 seats.",
        "payload": {
            "recipients": ["priya@halcyon.example"],
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
# 1. NO_ACTION
# ---------------------------------------------------------------------------


def test_a_valid_no_action_decision_is_accepted() -> None:
    decision = NoActionDecision.model_validate(
        {"reason": "Nothing actionable here.", "confidence": 0.95}
    )

    assert decision.decision == "no_action"
    assert decision.reason == "Nothing actionable here."
    assert decision.confidence == 0.95


def test_no_action_never_carries_a_fabricated_suggestion_shape() -> None:
    """`NoActionDecision` has no `action_type`/`payload`/`evidence` field at
    all — it cannot accidentally look like an empty `ActionSuggestion`."""
    field_names = set(NoActionDecision.model_fields)
    assert field_names == {"decision", "reason", "confidence"}


# ---------------------------------------------------------------------------
# 2. ActionSuggestion (send_email)
# ---------------------------------------------------------------------------


def test_a_valid_send_email_suggestion_is_accepted() -> None:
    suggestion = _suggestion()

    assert suggestion.decision == "suggest_action"
    assert suggestion.action_type is ActionType.SEND_EMAIL
    assert suggestion.evidence == ["message_1"]
    assert suggestion.payload["subject"] == "Pricing for 3 seats"


# ---------------------------------------------------------------------------
# 3. Discriminated union
# ---------------------------------------------------------------------------


def test_the_decision_union_discriminates_by_its_own_field() -> None:
    no_action: AgentReasoningDecision = make_no_action_decision()
    suggestion: AgentReasoningDecision = _suggestion()

    assert no_action.decision == "no_action"
    assert suggestion.decision == "suggest_action"
    assert isinstance(no_action, NoActionDecision)
    assert isinstance(suggestion, ActionSuggestion)
    assert not isinstance(no_action, ActionSuggestion)
    assert not isinstance(suggestion, NoActionDecision)


# ---------------------------------------------------------------------------
# 4/5. Evidence labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("label", ["message_1", "message_12", "memory_1", "analysis"])
def test_each_valid_evidence_label_shape_is_accepted(label: str) -> None:
    suggestion = _suggestion(evidence=[label])

    assert suggestion.evidence == [label]


@pytest.mark.parametrize(
    "label",
    [
        "message_0",  # 1-indexed by convention; 0 is never valid
        "contact_1",  # not one of the three allowed namespaces
        "message_1 ",  # trailing whitespace
        "MESSAGE_1",  # case-sensitive
        "550e8400-e29b-41d4-a716-446655440000",  # a raw UUID — never accepted
        "",
    ],
)
def test_an_invalid_evidence_label_shape_is_rejected(label: str) -> None:
    with pytest.raises(pydantic.ValidationError):
        _suggestion(evidence=[label])


def test_empty_evidence_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _suggestion(evidence=[])


def test_too_many_evidence_labels_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _suggestion(evidence=[f"message_{i}" for i in range(1, 12)])  # 11 > max 10


# ---------------------------------------------------------------------------
# 6. Confidence bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", [-0.01, 1.01, -1.0, 2.0])
def test_out_of_bounds_confidence_is_rejected_on_no_action(confidence: float) -> None:
    with pytest.raises(pydantic.ValidationError):
        NoActionDecision.model_validate({"reason": "x", "confidence": confidence})


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_out_of_bounds_confidence_is_rejected_on_a_suggestion(confidence: float) -> None:
    with pytest.raises(pydantic.ValidationError):
        _suggestion(confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 1.0, 0.5])
def test_boundary_confidence_values_are_accepted(confidence: float) -> None:
    decision = NoActionDecision.model_validate({"reason": "x", "confidence": confidence})
    assert decision.confidence == confidence


# ---------------------------------------------------------------------------
# 7. summary/reason bounds
# ---------------------------------------------------------------------------


def test_an_empty_reason_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        NoActionDecision.model_validate({"reason": "", "confidence": 0.5})


def test_an_empty_summary_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _suggestion(summary="")


def test_an_oversized_reason_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        NoActionDecision.model_validate({"reason": "x" * 1001, "confidence": 0.5})


def test_an_oversized_summary_is_rejected() -> None:
    with pytest.raises(pydantic.ValidationError):
        _suggestion(summary="x" * 501)


# ---------------------------------------------------------------------------
# 8. Memory context reuses AnalysisMemoryContextItem
# ---------------------------------------------------------------------------


def test_memory_context_reuses_the_existing_analysis_memory_context_item() -> None:
    request = _input(memory_context=(SOME_MEMORY_ITEM,))

    assert request.memory_context == (SOME_MEMORY_ITEM,)
    assert isinstance(request.memory_context[0], AnalysisMemoryContextItem)


# ---------------------------------------------------------------------------
# 9/10. No internal ids anywhere in the input or the output
# ---------------------------------------------------------------------------


def test_agent_reasoning_input_carries_no_internal_ids() -> None:
    field_names = {f.name for f in fields(AgentReasoningInput)}
    assert field_names == {
        "subject",
        "contact_display_name",
        "messages",
        "analysis",
        "memory_context",
        "capabilities",
    }
    forbidden_names = (
        "id",
        "workspace_id",
        "contact_id",
        "conversation_id",
        "memory_id",
        "action_id",
    )
    for forbidden in forbidden_names:
        assert forbidden not in field_names


def test_reasoning_analysis_context_carries_no_internal_ids() -> None:
    field_names = {f.name for f in fields(ReasoningAnalysisContext)}
    for forbidden in ("id", "workspace_id", "conversation_id", "analysis_id"):
        assert forbidden not in field_names


def test_no_decision_shape_requires_or_produces_a_uuid() -> None:
    for model in (NoActionDecision, ActionSuggestion):
        field_names = set(model.model_fields)
        assert not any(name == "id" or name.endswith("_id") for name in field_names)


def test_agent_reasoning_outcome_carries_no_internal_ids() -> None:
    field_names = {f.name for f in fields(AgentReasoningOutcome)}
    assert field_names == {
        "decision",
        "schema_version",
        "model_id",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "prompt_version",
    }


# ---------------------------------------------------------------------------
# 11/12/13. AgentReasoningInput construction
# ---------------------------------------------------------------------------


def test_capabilities_may_contain_send_email() -> None:
    request = _input(capabilities=(ActionType.SEND_EMAIL,))

    assert request.capabilities == (ActionType.SEND_EMAIL,)


def test_capabilities_default_to_empty() -> None:
    request = _input()

    assert request.capabilities == ()


def test_empty_memory_context_is_valid() -> None:
    request = _input(memory_context=())

    assert request.memory_context == ()


def test_a_minimal_single_message_transcript_is_valid() -> None:
    request = _input(messages=(SOME_MESSAGE,))

    assert len(request.messages) == 1
    assert request.messages[0] is SOME_MESSAGE


# ---------------------------------------------------------------------------
# 14. Unknown/extra structured fields are rejected
# ---------------------------------------------------------------------------


def test_an_unknown_field_is_rejected_on_no_action() -> None:
    with pytest.raises(pydantic.ValidationError):
        NoActionDecision.model_validate(
            {"reason": "x", "confidence": 0.5, "unexpected_field": "nope"}
        )


def test_an_unknown_field_is_rejected_on_a_suggestion() -> None:
    with pytest.raises(pydantic.ValidationError):
        _suggestion(unexpected_field="nope")


def test_a_decision_literal_mismatch_is_rejected() -> None:
    """The discriminator field itself is not an open string — only the one
    literal each model declares is accepted."""
    with pytest.raises(pydantic.ValidationError):
        NoActionDecision.model_validate(
            {"decision": "suggest_action", "reason": "x", "confidence": 0.5}
        )


# ---------------------------------------------------------------------------
# 15. FakeAgentReasoningClient implements the Protocol, no OpenAI involved
# ---------------------------------------------------------------------------


def test_fake_agent_reasoning_client_implements_the_protocol() -> None:
    fake = FakeAgentReasoningClient()
    protocol_typed: AgentReasoningClient = fake  # proves structural compliance
    request = _input()

    outcome = protocol_typed.reason(request)

    assert outcome.schema_version == REASONING_SCHEMA_VERSION
    assert outcome.decision.decision == "no_action"
    assert fake.call_count == 1
    assert fake.calls == [request]


def test_fake_agent_reasoning_client_can_be_scripted_with_a_suggestion() -> None:
    suggestion = _suggestion()
    client = FakeAgentReasoningClient(decision=suggestion)

    outcome = client.reason(_input())

    assert outcome.decision is suggestion


def test_fake_agent_reasoning_client_can_be_scripted_to_fail() -> None:
    client = FakeAgentReasoningClient()
    client.fail_with = AIProviderError("fake: provider unavailable")

    with pytest.raises(AIProviderError):
        client.reason(_input())

    assert client.call_count == 1  # the failed attempt is still recorded


# ---------------------------------------------------------------------------
# Bonus: the architecture boundary is enforced mechanically, not just by
# docstring — mirrors test_memory_context.py / test_gmail_send_boundary.py.
# ---------------------------------------------------------------------------

FORBIDDEN_IMPORT_PREFIXES = (
    "sqlalchemy",
    "nerva.models",
    "nerva.db",
    "nerva.repositories",
    "nerva.services",
    "nerva.routes",
    "nerva.nerva_connect",
    "nerva.nerva_actions",
    "openai",
    "httpx",
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


def test_agent_client_module_never_imports_services_db_or_transport() -> None:
    source_path = Path(agent_client_module.__file__)
    imported = _imported_module_names(source_path)

    for forbidden in FORBIDDEN_IMPORT_PREFIXES:
        offenders = {
            name for name in imported if name == forbidden or name.startswith(forbidden + ".")
        }
        assert not offenders, f"{source_path.name} imports {offenders}"
