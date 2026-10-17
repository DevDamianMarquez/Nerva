"""`nerva_core.memory_rules`: pure, no database, no LLM. `decide_memory_formation`
is the only code allowed to decide whether a new `estimated_value` memory
should be created, an existing one superseded, or nothing done at all."""

from nerva.nerva_core.memory_rules import (
    EstimatedValueObservation,
    ExistingEstimatedValueMemory,
    MemoryFormationDecision,
    decide_memory_formation,
)

VALID_OBSERVATION = EstimatedValueObservation(
    estimated_value_cents=150_000, value_currency="USD", value_is_explicit=True
)
NO_ESTIMATE = EstimatedValueObservation(
    estimated_value_cents=None, value_currency=None, value_is_explicit=None
)


def _existing(
    cents: int = 150_000, currency: str = "USD", fingerprint: str = "fp-1"
) -> ExistingEstimatedValueMemory:
    return ExistingEstimatedValueMemory(
        estimated_value_cents=cents, value_currency=currency, source_fingerprint=fingerprint
    )


# --- EstimatedValueObservation.has_valid_estimate -------------------------------


def test_a_value_with_a_currency_is_a_valid_estimate() -> None:
    assert VALID_OBSERVATION.has_valid_estimate is True


def test_no_value_and_no_currency_is_not_a_valid_estimate() -> None:
    assert NO_ESTIMATE.has_valid_estimate is False


def test_a_value_with_no_currency_is_not_a_valid_estimate() -> None:
    observation = EstimatedValueObservation(
        estimated_value_cents=100, value_currency=None, value_is_explicit=True
    )
    assert observation.has_valid_estimate is False


def test_a_currency_with_no_value_is_not_a_valid_estimate() -> None:
    observation = EstimatedValueObservation(
        estimated_value_cents=None, value_currency="USD", value_is_explicit=True
    )
    assert observation.has_valid_estimate is False


# --- decide_memory_formation: no existing active memory --------------------------


def test_no_existing_memory_and_a_valid_estimate_creates() -> None:
    decision = decide_memory_formation(
        observation=VALID_OBSERVATION, existing=None, current_fingerprint="fp-1"
    )

    assert decision == MemoryFormationDecision.CREATE


def test_no_existing_memory_and_no_valid_estimate_is_a_noop() -> None:
    decision = decide_memory_formation(
        observation=NO_ESTIMATE, existing=None, current_fingerprint="fp-1"
    )

    assert decision == MemoryFormationDecision.NOOP


# --- decide_memory_formation: idempotency against retries ------------------------


def test_the_same_fingerprint_is_always_a_noop_even_with_a_valid_estimate() -> None:
    decision = decide_memory_formation(
        observation=VALID_OBSERVATION,
        existing=_existing(fingerprint="fp-1"),
        current_fingerprint="fp-1",
    )

    assert decision == MemoryFormationDecision.NOOP


def test_the_same_fingerprint_is_always_a_noop_even_with_no_valid_estimate() -> None:
    decision = decide_memory_formation(
        observation=NO_ESTIMATE, existing=_existing(fingerprint="fp-1"), current_fingerprint="fp-1"
    )

    assert decision == MemoryFormationDecision.NOOP


def test_the_same_fingerprint_is_a_noop_even_when_the_value_would_otherwise_differ() -> None:
    """Documents that the idempotency check is checked before the value
    comparison: a caller re-running the exact same analysis must never
    supersede, even if (hypothetically) the existing row's recorded value
    differs from what this observation carries."""
    decision = decide_memory_formation(
        observation=VALID_OBSERVATION,
        existing=_existing(cents=999_999, currency="EUR", fingerprint="fp-1"),
        current_fingerprint="fp-1",
    )

    assert decision == MemoryFormationDecision.NOOP


# --- decide_memory_formation: a new analysis run, no longer a valid estimate -----


def test_a_new_fingerprint_with_no_valid_estimate_never_discards() -> None:
    decision = decide_memory_formation(
        observation=NO_ESTIMATE, existing=_existing(fingerprint="fp-1"), current_fingerprint="fp-2"
    )

    assert decision == MemoryFormationDecision.NOOP


# --- decide_memory_formation: a new analysis run, with a valid estimate ---------


def test_a_new_fingerprint_with_the_same_value_and_currency_is_a_noop() -> None:
    decision = decide_memory_formation(
        observation=VALID_OBSERVATION,
        existing=_existing(cents=150_000, currency="USD", fingerprint="fp-1"),
        current_fingerprint="fp-2",
    )

    assert decision == MemoryFormationDecision.NOOP


def test_a_new_fingerprint_with_a_different_cents_amount_supersedes() -> None:
    decision = decide_memory_formation(
        observation=VALID_OBSERVATION,
        existing=_existing(cents=200_000, currency="USD", fingerprint="fp-1"),
        current_fingerprint="fp-2",
    )

    assert decision == MemoryFormationDecision.SUPERSEDE


def test_a_new_fingerprint_with_a_different_currency_supersedes_even_with_the_same_cents() -> None:
    decision = decide_memory_formation(
        observation=VALID_OBSERVATION,
        existing=_existing(cents=150_000, currency="EUR", fingerprint="fp-1"),
        current_fingerprint="fp-2",
    )

    assert decision == MemoryFormationDecision.SUPERSEDE


def test_value_is_explicit_never_influences_the_decision() -> None:
    """The rule branches only on presence/value/currency — `value_is_explicit`
    is carried through for a caller to set `MemoryOrigin`/`confidence`, never
    part of this decision (mirrors `nerva_core.rules`'s sentiment note)."""
    explicit = EstimatedValueObservation(
        estimated_value_cents=150_000, value_currency="USD", value_is_explicit=True
    )
    inferred = EstimatedValueObservation(
        estimated_value_cents=150_000, value_currency="USD", value_is_explicit=False
    )

    for observation in (explicit, inferred):
        decision = decide_memory_formation(
            observation=observation, existing=None, current_fingerprint="fp-1"
        )
        assert decision == MemoryFormationDecision.CREATE
