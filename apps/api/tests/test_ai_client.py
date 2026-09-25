"""`FakeAIClient` itself: the substitute every other test in this suite relies on
to prove no real LLM call is ever made. If this fixture doesn't behave like a real
`AIAnalysisClient` would (recording calls, raising scripted errors, stamping the
current schema/prompt versions), every test built on top of it is unreliable."""

from datetime import UTC, datetime

import pytest

from nerva.core.enums import ConversationCategory, MessageDirection
from nerva.nerva_ai.client import AnalysisInput, AnalysisMessage
from nerva.nerva_ai.errors import AIProviderError
from nerva.nerva_ai.prompts import PROMPT_VERSION
from nerva.nerva_ai.schemas import SCHEMA_VERSION
from tests.fakes.ai import FakeAIClient, make_analysis_result

SOME_REQUEST = AnalysisInput(
    subject="Pricing for 3 seats",
    messages=[
        AnalysisMessage(
            direction=MessageDirection.INBOUND,
            sender_label="Priya Nair",
            sent_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            body_text="Can you send pricing for 3 seats?",
        )
    ],
)


def test_analyze_records_every_call() -> None:
    client = FakeAIClient()

    client.analyze(SOME_REQUEST)
    client.analyze(SOME_REQUEST)

    assert client.call_count == 2
    assert client.calls == [SOME_REQUEST, SOME_REQUEST]


def test_analyze_returns_the_scripted_result_stamped_with_current_versions() -> None:
    scripted = make_analysis_result(commercial_signal=True, confidence=0.9)
    client = FakeAIClient(result=scripted)

    outcome = client.analyze(SOME_REQUEST)

    assert outcome.result == scripted
    assert outcome.schema_version == SCHEMA_VERSION
    assert outcome.prompt_version == PROMPT_VERSION
    assert outcome.model_id == client.model_id
    assert outcome.latency_ms >= 0


def test_analyze_raises_the_scripted_failure_instead_of_returning() -> None:
    client = FakeAIClient()
    client.fail_with = AIProviderError("fake: provider unavailable")

    with pytest.raises(AIProviderError):
        client.analyze(SOME_REQUEST)

    # The failed attempt is still recorded: callers need to see it was tried.
    assert client.call_count == 1


def test_make_analysis_result_defaults_are_valid_and_overridable() -> None:
    default = make_analysis_result()
    assert default.category == ConversationCategory.OTHER
    assert default.commercial_signal is False

    overridden = make_analysis_result(category=ConversationCategory.SALES_LEAD, confidence=1.0)
    assert overridden.category == ConversationCategory.SALES_LEAD
    assert overridden.confidence == 1.0
