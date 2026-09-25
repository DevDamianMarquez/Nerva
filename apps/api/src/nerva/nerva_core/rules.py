"""Deterministic business rules: turns a completed `ConversationAnalysis` into
typed, inert business-signal effects.

Pure — no database, no LLM, no provider imports — so every rule here is
exhaustively unit-testable, and, crucially, this is the *only* code allowed to
decide a business signal should exist. The LLM's structured output
(`nerva_ai.schemas.ConversationAnalysisResult`) never reaches this module
directly: `nerva.services.signals` reads the already-persisted, already-
validated `ConversationAnalysis` row and hands this module only the minimal
slice (`AnalysisSummary`) a rule actually needs. Nothing here ever writes to
the database, calls a provider, or creates an `Opportunity` — it only answers
"does this analysis qualify for a signal, and what should happen to whatever
signal already exists."
"""

from dataclasses import dataclass
from enum import Enum, auto

from nerva.core.enums import BusinessSignalStatus, ConversationCategory

# Conservative by design: a low-confidence guess must never alone queue a
# decision for a human. Kept as a module constant (not buried in a condition)
# so it is easy to find, tune, and reference from tests — the same treatment as
# `nerva.services.integrations`'s `SYNC_WINDOW_DAYS` / `MAX_MESSAGES_PER_SYNC`.
POTENTIAL_OPPORTUNITY_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True, slots=True)
class AnalysisSummary:
    """The minimal slice of a completed analysis a rule can see — deliberately
    not the full `ConversationAnalysisResult` (which also carries fields no
    rule here uses, like `intent` or `mentioned_people`) and never the
    SQLAlchemy row itself (rules must not depend on the ORM). Built by
    `nerva.services.signals` from a `ConversationAnalysis` row whose
    `status == AnalysisStatus.COMPLETED` — every field below is guaranteed
    non-`None` on such a row (see `nerva.services.analysis`'s persistence)."""

    category: ConversationCategory
    commercial_signal: bool
    confidence: float
    summary: str
    estimated_value_cents: int | None
    value_currency: str | None
    value_is_explicit: bool | None


@dataclass(frozen=True, slots=True)
class PotentialOpportunitySignal:
    """An inert, deterministic effect: apply it, don't act on it. Every field
    traces back to a value that already passed `ConversationAnalysisResult`'s
    validation — nothing here is invented by this rule."""

    title: str
    summary: str
    estimated_value_cents: int | None
    currency: str | None
    value_is_explicit: bool | None
    confidence: float


_TITLE_MAX_LENGTH = 200  # matches nerva.schemas.crm.OpportunityCreate's ShortText


def evaluate_potential_opportunity(analysis: AnalysisSummary) -> PotentialOpportunitySignal | None:
    """Returns a signal iff the analysis conservatively qualifies as a sales
    lead worth a human's attention, or `None` otherwise.

    Deliberately narrow for v1: sentiment is never part of this decision (a
    "happy" email is not a lead, and an "angry" one is not necessarily not
    one) — only `commercial_signal`, `category` and `confidence` do. A
    monetary value is propagated only when the analysis itself already found
    one (explicit or inferred, with that distinction preserved); this rule
    never invents one of its own.
    """
    if not analysis.commercial_signal:
        return None
    if analysis.category != ConversationCategory.SALES_LEAD:
        return None
    if analysis.confidence < POTENTIAL_OPPORTUNITY_CONFIDENCE_THRESHOLD:
        return None

    return PotentialOpportunitySignal(
        title=analysis.summary[:_TITLE_MAX_LENGTH],
        summary=analysis.summary,
        estimated_value_cents=analysis.estimated_value_cents,
        currency=analysis.value_currency,
        value_is_explicit=analysis.value_is_explicit,
        confidence=analysis.confidence,
    )


@dataclass(frozen=True, slots=True)
class ExistingSignalState:
    """The minimal slice of an already-persisted `BusinessSignal` a
    materialization decision needs — not the ORM row itself, for the same
    reason `AnalysisSummary` is not a `ConversationAnalysis` row."""

    status: BusinessSignalStatus
    source_fingerprint: str


class MaterializationDecision(Enum):
    """What `nerva.services.signals.SignalService` should do about the current
    (rule result, existing signal) pair. Not a database-backed enum — this
    never leaves this pure decision, so it does not belong in `core.enums`."""

    NOOP = auto()
    CREATE = auto()
    REPLACE = auto()


def decide_materialization(
    *,
    has_qualifying_signal: bool,
    existing: ExistingSignalState | None,
    current_fingerprint: str,
) -> MaterializationDecision:
    """The re-analysis behavior for business signals, as an explicit decision
    table (mirrors `nerva_actions.lifecycle`'s spirit: pure code, exhaustively
    testable, so the policy is legible in one place rather than scattered
    through a service):

    | existing signal    | qualifies? | decision | why |
    |---------------------|------------|----------|-----|
    | none                | yes        | CREATE   | |
    | none                | no         | NOOP     | |
    | pending             | yes/no     | NOOP     | never churn a decision a human has not made |
    | confirmed           | yes/no     | NOOP     | permanently frozen, never silently undone |
    | dismissed, same fp  | yes/no     | NOOP     | don't recreate what was just declined |
    | dismissed, new fp   | yes        | REPLACE  | new evidence — give the human another chance |
    | dismissed, new fp   | no         | NOOP     | |

    `REPLACE` reuses the existing row (there is at most one signal per
    conversation+type — see `nerva.models.signals`), rather than creating a
    second one, so a conversation never accumulates a history of superseded
    signal rows.
    """
    if not has_qualifying_signal:
        return MaterializationDecision.NOOP
    if existing is None:
        return MaterializationDecision.CREATE
    if existing.status in (BusinessSignalStatus.PENDING, BusinessSignalStatus.CONFIRMED):
        return MaterializationDecision.NOOP
    # DISMISSED
    if existing.source_fingerprint == current_fingerprint:
        return MaterializationDecision.NOOP
    return MaterializationDecision.REPLACE
