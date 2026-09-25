"""`nerva_core.rules`: pure, no database, no LLM. `evaluate_potential_opportunity`
is the only code allowed to decide a business signal should exist;
`decide_materialization` is the only code allowed to decide what a
materialization service should do about one that may already exist."""

import dataclasses

from nerva.core.enums import BusinessSignalStatus, ConversationCategory
from nerva.nerva_core.rules import (
    POTENTIAL_OPPORTUNITY_CONFIDENCE_THRESHOLD,
    AnalysisSummary,
    ExistingSignalState,
    MaterializationDecision,
    decide_materialization,
    evaluate_potential_opportunity,
)

QUALIFYING = AnalysisSummary(
    category=ConversationCategory.SALES_LEAD,
    commercial_signal=True,
    confidence=0.9,
    summary="Asked about pricing for 3 seats.",
    estimated_value_cents=None,
    value_currency=None,
    value_is_explicit=None,
)


def _replace(**overrides: object) -> AnalysisSummary:
    return dataclasses.replace(QUALIFYING, **overrides)  # type: ignore[arg-type]


# --- evaluate_potential_opportunity ---------------------------------------------


def test_a_qualifying_sales_analysis_produces_a_signal() -> None:
    signal = evaluate_potential_opportunity(QUALIFYING)

    assert signal is not None
    assert signal.title == QUALIFYING.summary
    assert signal.summary == QUALIFYING.summary


def test_commercial_signal_false_never_qualifies_regardless_of_everything_else() -> None:
    analysis = _replace(commercial_signal=False, confidence=1.0)

    assert evaluate_potential_opportunity(analysis) is None


def test_a_non_sales_lead_category_does_not_qualify() -> None:
    for category in ConversationCategory:
        if category == ConversationCategory.SALES_LEAD:
            continue
        analysis = _replace(category=category)

        assert evaluate_potential_opportunity(analysis) is None, category


def test_confidence_below_the_threshold_does_not_qualify() -> None:
    below = _replace(confidence=POTENTIAL_OPPORTUNITY_CONFIDENCE_THRESHOLD - 0.01)

    assert evaluate_potential_opportunity(below) is None


def test_confidence_at_or_above_the_threshold_qualifies() -> None:
    at_threshold = _replace(confidence=POTENTIAL_OPPORTUNITY_CONFIDENCE_THRESHOLD)

    assert evaluate_potential_opportunity(at_threshold) is not None


def test_sentiment_alone_is_never_part_of_the_decision() -> None:
    """The rule takes no `sentiment` field at all — this test documents that
    fact structurally: `AnalysisSummary` has no sentiment, so a "happy" or
    "angry" email cannot influence qualification through this rule."""
    assert "sentiment" not in AnalysisSummary.__dataclass_fields__


def test_an_explicit_monetary_value_is_propagated_unchanged() -> None:
    analysis = _replace(estimated_value_cents=150_000, value_currency="USD", value_is_explicit=True)

    signal = evaluate_potential_opportunity(analysis)

    assert signal is not None
    assert signal.estimated_value_cents == 150_000
    assert signal.currency == "USD"
    assert signal.value_is_explicit is True


def test_an_inferred_monetary_value_is_propagated_with_its_flag_preserved() -> None:
    analysis = _replace(estimated_value_cents=50_000, value_currency="EUR", value_is_explicit=False)

    signal = evaluate_potential_opportunity(analysis)

    assert signal is not None
    assert signal.estimated_value_cents == 50_000
    assert signal.value_is_explicit is False


def test_a_missing_monetary_value_stays_missing_never_fabricated() -> None:
    analysis = _replace(estimated_value_cents=None, value_currency=None, value_is_explicit=None)

    signal = evaluate_potential_opportunity(analysis)

    assert signal is not None
    assert signal.estimated_value_cents is None
    assert signal.currency is None
    assert signal.value_is_explicit is None


def test_the_title_is_truncated_to_the_opportunity_title_limit() -> None:
    analysis = _replace(summary="x" * 500)

    signal = evaluate_potential_opportunity(analysis)

    assert signal is not None
    assert len(signal.title) == 200
    assert signal.summary == "x" * 500  # the full summary is preserved, only title is truncated


# --- decide_materialization ------------------------------------------------------


def test_no_qualifying_signal_never_creates_anything() -> None:
    decision = decide_materialization(
        has_qualifying_signal=False, existing=None, current_fingerprint="fp-1"
    )

    assert decision == MaterializationDecision.NOOP


def test_a_qualifying_signal_with_no_existing_row_is_created() -> None:
    decision = decide_materialization(
        has_qualifying_signal=True, existing=None, current_fingerprint="fp-1"
    )

    assert decision == MaterializationDecision.CREATE


def test_a_pending_signal_is_never_touched_by_reevaluation() -> None:
    pending = ExistingSignalState(status=BusinessSignalStatus.PENDING, source_fingerprint="fp-1")

    same_fp = decide_materialization(
        has_qualifying_signal=True, existing=pending, current_fingerprint="fp-1"
    )
    different_fp = decide_materialization(
        has_qualifying_signal=True, existing=pending, current_fingerprint="fp-2"
    )
    no_longer_qualifies = decide_materialization(
        has_qualifying_signal=False, existing=pending, current_fingerprint="fp-2"
    )

    assert same_fp == different_fp == no_longer_qualifies == MaterializationDecision.NOOP


def _state(status: BusinessSignalStatus, fingerprint: str = "fp-1") -> ExistingSignalState:
    return ExistingSignalState(status=status, source_fingerprint=fingerprint)


def test_a_confirmed_signal_is_never_touched_regardless_of_reevaluation() -> None:
    confirmed = _state(BusinessSignalStatus.CONFIRMED)

    for qualifies in (True, False):
        for fingerprint in ("fp-1", "fp-2"):
            decision = decide_materialization(
                has_qualifying_signal=qualifies, existing=confirmed, current_fingerprint=fingerprint
            )
            assert decision == MaterializationDecision.NOOP


def test_a_dismissed_signal_with_the_same_fingerprint_is_not_recreated() -> None:
    dismissed = _state(BusinessSignalStatus.DISMISSED)

    decision = decide_materialization(
        has_qualifying_signal=True, existing=dismissed, current_fingerprint="fp-1"
    )

    assert decision == MaterializationDecision.NOOP


def test_a_dismissed_signal_with_a_new_fingerprint_that_still_qualifies_is_replaced() -> None:
    dismissed = _state(BusinessSignalStatus.DISMISSED)

    decision = decide_materialization(
        has_qualifying_signal=True, existing=dismissed, current_fingerprint="fp-2"
    )

    assert decision == MaterializationDecision.REPLACE


def test_a_dismissed_signal_with_a_new_fingerprint_that_no_longer_qualifies_stays_noop() -> None:
    dismissed = _state(BusinessSignalStatus.DISMISSED)

    decision = decide_materialization(
        has_qualifying_signal=False, existing=dismissed, current_fingerprint="fp-2"
    )

    assert decision == MaterializationDecision.NOOP
