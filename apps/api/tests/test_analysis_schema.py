"""`ConversationAnalysisResult` and friends: the strict schema that is the entire
trust boundary between an LLM provider and the rest of Nerva. Untrusted output
must be rejected, not coerced or guessed into something plausible — see
`docs/ARCHITECTURE.md`'s content-safety section and the Milestone 4 design
decision on uncertainty (fields the model must be allowed to leave `null`)."""

from datetime import date

import pytest
from pydantic import ValidationError

from nerva.core.enums import ConversationCategory, ConversationPriority, SentimentLabel
from nerva.nerva_ai.schemas import ConversationAnalysisResult, DeadlineEstimate, MonetaryEstimate

MINIMAL = {
    "summary": "Asked about pricing for 3 seats.",
    "category": ConversationCategory.SALES_LEAD,
    "priority": ConversationPriority.MEDIUM,
    "requires_response": True,
    "commercial_signal": True,
    "confidence": 0.8,
}


def test_a_minimal_result_validates_with_every_optional_field_absent() -> None:
    result = ConversationAnalysisResult.model_validate(MINIMAL)

    assert result.intent is None
    assert result.sentiment is None
    assert result.estimated_value is None
    assert result.deadline is None
    assert result.suggested_next_step is None
    assert result.mentioned_people == []
    assert result.mentioned_companies == []


def test_required_fields_must_be_present() -> None:
    required_fields = (
        "summary",
        "category",
        "priority",
        "requires_response",
        "commercial_signal",
        "confidence",
    )
    for missing in required_fields:
        payload = {k: v for k, v in MINIMAL.items() if k != missing}
        with pytest.raises(ValidationError):
            ConversationAnalysisResult.model_validate(payload)


def test_unknown_fields_are_rejected_not_silently_dropped() -> None:
    payload = {**MINIMAL, "next_action_to_execute": "send_email"}

    with pytest.raises(ValidationError):
        ConversationAnalysisResult.model_validate(payload)


@pytest.mark.parametrize("bad_confidence", [-0.01, 1.01, 2.0])
def test_confidence_must_be_between_zero_and_one(bad_confidence: float) -> None:
    with pytest.raises(ValidationError):
        ConversationAnalysisResult.model_validate({**MINIMAL, "confidence": bad_confidence})


def test_category_must_be_one_of_the_closed_set() -> None:
    with pytest.raises(ValidationError):
        ConversationAnalysisResult.model_validate({**MINIMAL, "category": "not_a_real_category"})


def test_sentiment_may_be_present_or_explicitly_absent() -> None:
    with_sentiment = ConversationAnalysisResult.model_validate(
        {**MINIMAL, "sentiment": SentimentLabel.NEGATIVE}
    )
    assert with_sentiment.sentiment == SentimentLabel.NEGATIVE

    without_sentiment = ConversationAnalysisResult.model_validate({**MINIMAL, "sentiment": None})
    assert without_sentiment.sentiment is None


def test_intent_is_bounded_free_text_not_an_enum() -> None:
    result = ConversationAnalysisResult.model_validate({**MINIMAL, "intent": "request_quote"})
    assert result.intent == "request_quote"

    with pytest.raises(ValidationError):
        ConversationAnalysisResult.model_validate({**MINIMAL, "intent": "x" * 101})


def test_mentioned_entities_are_bounded_lists_that_default_empty() -> None:
    result = ConversationAnalysisResult.model_validate(
        {**MINIMAL, "mentioned_people": ["Priya Nair"], "mentioned_companies": ["Halcyon Freight"]}
    )
    assert result.mentioned_people == ["Priya Nair"]
    assert result.mentioned_companies == ["Halcyon Freight"]

    with pytest.raises(ValidationError):
        ConversationAnalysisResult.model_validate(
            {**MINIMAL, "mentioned_people": [f"Person {i}" for i in range(21)]}
        )


def test_monetary_estimate_distinguishes_explicit_from_inferred() -> None:
    explicit = MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=True)
    inferred = MonetaryEstimate(amount_cents=150_000, currency="USD", is_explicit=False)

    assert explicit.is_explicit is True
    assert inferred.is_explicit is False


def test_monetary_estimate_rejects_negative_amounts_and_bad_currency_codes() -> None:
    with pytest.raises(ValidationError):
        MonetaryEstimate(amount_cents=-1, currency="USD", is_explicit=True)
    with pytest.raises(ValidationError):
        MonetaryEstimate(amount_cents=100, currency="usd", is_explicit=True)
    with pytest.raises(ValidationError):
        MonetaryEstimate(amount_cents=100, currency="US", is_explicit=True)


def test_estimated_value_is_never_required_and_never_fabricated_by_the_schema() -> None:
    result = ConversationAnalysisResult.model_validate(MINIMAL)
    assert result.estimated_value is None


def test_deadline_is_a_date_not_a_datetime_to_avoid_inventing_a_time() -> None:
    deadline = DeadlineEstimate(date=date(2026, 6, 5), is_explicit=True)
    assert deadline.date == date(2026, 6, 5)

    result = ConversationAnalysisResult.model_validate(
        {**MINIMAL, "deadline": {"date": "2026-06-05", "is_explicit": False}}
    )
    assert result.deadline is not None
    assert result.deadline.date == date(2026, 6, 5)
    assert result.deadline.is_explicit is False
