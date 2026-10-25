"""M7.1b — the deterministic gate between an untrusted `AgentReasoningDecision`
(M7.1a) and anything that could ever become a persisted `Action`.

    AgentReasoningDecision (model output, not trusted)
            v
    decide_proposal(ProposalPolicyInput)
            v
    ProposalRejection | AllowedProposal       (ProposalPolicyDecision)
            v
    [FUTURE, not this block — M7.1c] fingerprint + dedup + ActionService.propose

Pure — no database, no HTTP, no provider call, no `ActionService`/
`ActionRepository`, no SQLAlchemy, no Gmail — mirroring `nerva_core.rules`
(`decide_materialization`) and `nerva_core.memory_rules`
(`decide_memory_formation`) exactly: every rule here is exhaustively
unit-testable, and this is the *only* code allowed to decide whether a
suggestion is safe/valid enough to be worth materializing later. It never
creates an `Action`, never checks for an existing proposal, and never
computes a fingerprint — see "Not this block" below.

## Why this reuses `nerva_ai.agent_client` types directly, unlike
## `nerva_core.rules`/`nerva_core.memory_rules`

Both existing pure-rule modules define their *own* minimal, local input
shape (`nerva_core.rules.AnalysisSummary`, `nerva_core.memory_rules.
EstimatedValueObservation`) rather than importing `nerva_ai`'s own result
types — deliberately narrowing a much larger upstream object
(`ConversationAnalysisResult`/`ConversationAnalysis`) down to the few fields
a given rule actually uses, and keeping `nerva_core` with zero dependency on
`nerva_ai`. This module makes a different, deliberate choice: `ActionSuggestion`/
`NoActionDecision`/`AgentReasoningDecision` (`nerva_ai.agent_client`) are
imported and used *as-is*, not re-declared locally. There is no narrowing
opportunity to justify a local copy here — M7.1a already built those three
types to be the minimal, final output shape of a reasoning call (see that
module's own docstring); duplicating them field-for-field in `nerva_core`
would be pure ceremony, not a "minimal slice" the way `AnalysisSummary` is a
real narrowing of something bigger. This does introduce a new, one-directional
dependency (`nerva_core` -> `nerva_ai`, where previously only the reverse
existed via `nerva_ai.client` importing `nerva_core.memory_context.
MemoryScope`) — noted here explicitly as a deliberate departure from the
established precedent, not an oversight.

## Reasoner capabilities vs. policy allowlist — two independent checks

`AgentReasoningInput.capabilities` (M7.1a) is what the *system* told the
model was available for *this specific run* — never authority on its own.
This module checks an `ActionSuggestion.action_type` against two separate,
independent sets, in this order:

1. `_ALLOWED_ACTION_TYPES` — this policy's own fixed, hardcoded allowlist for
   M7.1 (`{SEND_EMAIL}` only). A hardcoded module constant, not a field on
   `ProposalPolicyInput`: accepting it as a per-call parameter would let a
   buggy or compromised caller widen it, defeating the point of re-checking
   independently of whatever the reasoner was told. `CREATE_TASK`/
   `UPDATE_OPPORTUNITY_STAGE` are rejected here regardless of what a future
   caller ever puts in `declared_capabilities` — see `ProposalPolicyReason.
   ACTION_TYPE_NOT_ALLOWED`.
2. `ProposalPolicyInput.declared_capabilities` — the exact capabilities
   actually declared for *this* reasoning run (the same set passed as
   `AgentReasoningInput.capabilities`). A type absent from *this* set but
   present in (1) is `ProposalPolicyReason.ACTION_TYPE_NOT_AVAILABLE` — a
   distinct code, because it means the model proposed something it was
   never told was on offer this time, which is itself suspicious
   independent of whether the type is ever allowed at all. Checking (1)
   before (2) is deliberate: a type `_ALLOWED_ACTION_TYPES` never contains
   is rejected as "not allowed," never as merely "not available this run,"
   even if a caller's bug put it in `declared_capabilities` too.

## Evidence grounding

`ProposalPolicyInput.available_evidence_labels` must be the exact, closed
set of labels that existed in the specific `AgentReasoningInput` the
decision being checked was reasoned over (see `nerva_ai.agent_client`'s own
docstring for the `message_N`/`memory_N`/`"analysis"` convention) — this
module does not, and cannot, derive that set itself; it never sees the
original `AgentReasoningInput`, only the label set a caller already computed
from it. `ActionSuggestion.evidence` already guarantees each label's *shape*
is one of the three valid forms (M7.1a); this module is what checks each
label's *existence* in that exact run — a label that merely looks valid
(e.g. `"message_999"` when only one message existed) is `ProposalPolicyReason.
INVALID_EVIDENCE`, exactly like a label that is outright malformed would have
already been. Even one invented label among otherwise-valid ones rejects the
whole suggestion — never a partial acceptance.

**`SEND_EMAIL` additionally requires at least one grounded `message_N`
label** (`_REQUIRES_MESSAGE_EVIDENCE`), not merely "at least one label" (M7.1a
already guarantees that much structurally). Rationale: proposing to send an
email is inherently about *this conversation*; grounding it only in `analysis`
or only in `memory_N` would let a suggestion exist with no citation of any
actual message content it is purportedly responding to. `analysis`/`memory_N`
labels may still accompany a `message_N` one; they just cannot substitute for
it. This is deliberately scoped to `SEND_EMAIL` as a type-specific
requirement, not a blanket rule — a future action type might have no
conversation to ground in at all, and would need its own evaluation, not
this one inherited by default.

## Payload validation: reused, never re-implemented

`ActionSuggestion.payload` is a raw, `extra`-unchecked-against-its-real-
contract JSON object (see that class's own docstring) — surviving M7.1a's
structured-output validation only proves it is *some* JSON object, not that
it satisfies `SendEmailPayload`. This module calls `nerva_actions.payloads.
parse_action_payload` — the exact same function `nerva_actions.eligibility.
ensure_executable` calls before execution — rather than re-implementing any
check (recipient count, email syntax, body length, unexpected fields) by
hand. A payload that fails this is `ProposalPolicyReason.INVALID_PAYLOAD`,
a safe, ordinary outcome of untrusted model output — never an exception this
module lets escape; the same discipline `nerva.services.analysis` already
applies to a provider's own output.

## Recipient authority

The model has no authority to choose a recipient. `ProposalPolicyInput.
allowed_recipients` is the exact, pre-resolved set of addresses this
conversation may be replied to, built by a future caller from `Conversation`/
`Contact` data — never by this module, never by the model, and never
derived from anything inside a message body. The check is **exact set
equality** between `validated_payload.recipients` and `allowed_recipients`
(both lower-cased before comparing — `SendEmailPayload` already lower-cases
the former via its own validator; this module lower-cases the latter too,
rather than trusting a caller to have done so), not a subset check: a
suggestion naming the right recipient *plus* an invented extra address is
rejected exactly like one naming a completely different address —
`ProposalPolicyReason.RECIPIENT_MISMATCH` either way. Duplicate entries in
`validated_payload.recipients` do not themselves cause a mismatch (set
comparison collapses them, and `SendEmailPayload` does not forbid them
either); neither does the order either list is given in. An empty
`allowed_recipients` (no resolvable recipient for this conversation at all)
can never match any non-empty candidate list (`SendEmailPayload.recipients`
requires at least one) — a safe rejection falls out of the same equality
check with no special case needed.

## Confidence is never a gate

`ActionSuggestion.confidence`/`reason` are carried through unchanged onto
`AllowedProposal` for future observability/audit — this module never
compares either against a threshold. A high-confidence suggestion with an
invalid payload or a mismatched recipient is rejected exactly as a
low-confidence one would be; a low-confidence suggestion that is otherwise
structurally valid is still `ALLOW`. Confidence is information the model
reported about itself, not authority this module grants or withholds
based on it — see M7.1 audit's §13 for why no arbitrary threshold is
introduced here either.

## Not this block (M7.1c+)

No proposal fingerprint, no duplicate-proposal lookup, no `ActionRepository`
query, no `ActionService.propose` call, no `ActivityEvent`. `AllowedProposal`
is the safe, validated product this module hands to a future caller — what
that caller does with it (compute a fingerprint, check for an existing live
proposal, finally call `ActionService.propose(actor=Actor.nerva())`) is
explicitly out of scope here, and nothing in this module assumes or depends
on any of it happening in any particular way.
"""

from dataclasses import dataclass
from enum import Enum, auto

from nerva.core.enums import ActionType
from nerva.nerva_actions.errors import ActionExecutorUnavailableError, ActionNotExecutableError
from nerva.nerva_actions.payloads import ExecutablePayload, SendEmailPayload, parse_action_payload
from nerva.nerva_ai.agent_client import ActionSuggestion, AgentReasoningDecision, NoActionDecision

# M7.1c addition (purely additive — the decision logic below is unchanged
# from M7.1b): identifies *this exact ruleset* for provenance/fingerprinting
# purposes, the same role `nerva_ai.prompts.PROMPT_VERSION` plays for the
# analysis prompt. Bump this whenever `decide_proposal`'s rules change in a
# way that should make a previously-computed `AllowedProposal` for the same
# reasoning state distinguishable from one produced under a different
# ruleset — see `nerva_core.proposal_fingerprint`, the only current reader.
PROPOSAL_POLICY_VERSION = "v1"

# M7.1's own fixed allowlist — see the module docstring's "Reasoner
# capabilities vs. policy allowlist". Hardcoded here, not a field on
# `ProposalPolicyInput`, so no caller can widen it.
_ALLOWED_ACTION_TYPES: frozenset[ActionType] = frozenset({ActionType.SEND_EMAIL})

# Per-action-type evidence grounding requirement — see the module
# docstring's "Evidence grounding". Scoped deliberately to the one type
# that exists today, not applied blindly to any future one.
_REQUIRES_MESSAGE_EVIDENCE: frozenset[ActionType] = frozenset({ActionType.SEND_EMAIL})


class ProposalPolicyReason(Enum):
    """Why `decide_proposal` reached the outcome it did — a closed, checkable
    code, never a string used for branching logic. `ALLOWED` is included so
    any caller reading this enum off either half of `ProposalPolicyDecision`
    never needs an `isinstance` check first (see `AllowedProposal.
    policy_reason`)."""

    REASONER_NO_ACTION = auto()
    ACTION_TYPE_NOT_ALLOWED = auto()
    ACTION_TYPE_NOT_AVAILABLE = auto()
    INVALID_PAYLOAD = auto()
    INVALID_EVIDENCE = auto()
    RECIPIENT_MISMATCH = auto()
    ALLOWED = auto()


@dataclass(frozen=True, slots=True)
class ProposalPolicyInput:
    """Everything `decide_proposal` needs, and nothing it can go fetch on its
    own — this module never queries anything. A future caller (M7.1c) builds
    this from the exact `AgentReasoningInput` that produced `decision`, plus
    whatever already-resolved, trusted facts the policy must check against.
    See the module docstring's "Evidence grounding"/"Recipient authority"
    for exactly what `available_evidence_labels`/`allowed_recipients` must
    represent and who is responsible for computing them correctly."""

    decision: AgentReasoningDecision
    declared_capabilities: frozenset[ActionType]
    available_evidence_labels: frozenset[str]
    allowed_recipients: frozenset[str]


@dataclass(frozen=True, slots=True)
class ProposalRejection:
    """A `NO_ACTION` outcome — deterministic, and never a database row (see
    the module docstring). `reason` is always one of the six rejection codes;
    never `ALLOWED`. `detail` is a short, fixed explanation this module
    itself generates, for logging only — never model-authored text, and
    never used for branching logic (that is exactly what `reason` is for)."""

    reason: ProposalPolicyReason
    detail: str


@dataclass(frozen=True, slots=True)
class AllowedProposal:
    """An `ALLOW` outcome: the validated, normalized data a future M7.1c
    should actually use — never the raw `ActionSuggestion`, and never the
    unvalidated `dict` it carried as `payload`. `reason`/`confidence` are the
    *model's own* stated justification/confidence
    (`ActionSuggestion.reason`/`.confidence`), carried through unchanged for
    future observability/audit — never re-derived, and never a gate (see the
    module docstring's "Confidence is never a gate"). `policy_reason` is
    always `ALLOWED` here; see `ProposalPolicyReason`'s own docstring for
    why it exists on this side too."""

    action_type: ActionType
    summary: str
    validated_payload: ExecutablePayload
    reason: str
    confidence: float
    grounded_evidence: tuple[str, ...]
    policy_reason: ProposalPolicyReason = ProposalPolicyReason.ALLOWED


# A discriminated union, not `AllowedProposal | None`, for the same reason
# `nerva_ai.agent_client.AgentReasoningDecision` is not `ActionSuggestion |
# None`: a rejection is a distinct, equally first-class outcome with its own
# shape (a `ProposalPolicyReason` plus a `detail`), not a degenerate "empty"
# allowance.
ProposalPolicyDecision = ProposalRejection | AllowedProposal


def decide_proposal(policy_input: ProposalPolicyInput) -> ProposalPolicyDecision:
    """The one function allowed to turn an `AgentReasoningDecision` into a
    `ProposalPolicyDecision`. Checks fail fast, in a fixed order (fewest
    assumptions first), and return on the *first* violated gate rather than
    aggregating every problem — the same "fail fast, do not touch anything
    further" discipline `nerva_actions.eligibility.ensure_executable` already
    applies to a different pre-flight check. See the module docstring for
    the full reasoning behind each gate."""
    decision = policy_input.decision

    if isinstance(decision, NoActionDecision):
        return ProposalRejection(
            reason=ProposalPolicyReason.REASONER_NO_ACTION,
            detail="The reasoner itself decided no action was warranted.",
        )
    return _decide_suggestion(decision, policy_input)


def _decide_suggestion(
    suggestion: ActionSuggestion, policy_input: ProposalPolicyInput
) -> ProposalPolicyDecision:
    action_type = suggestion.action_type

    if action_type not in _ALLOWED_ACTION_TYPES:
        return ProposalRejection(
            reason=ProposalPolicyReason.ACTION_TYPE_NOT_ALLOWED,
            detail=f"'{action_type.value}' is not an action type this policy allows.",
        )

    if action_type not in policy_input.declared_capabilities:
        return ProposalRejection(
            reason=ProposalPolicyReason.ACTION_TYPE_NOT_AVAILABLE,
            detail=(
                f"'{action_type.value}' was not among the capabilities declared "
                "for this reasoning run."
            ),
        )

    available_labels = policy_input.available_evidence_labels
    invalid_labels = sorted(label for label in suggestion.evidence if label not in available_labels)
    if invalid_labels:
        return ProposalRejection(
            reason=ProposalPolicyReason.INVALID_EVIDENCE,
            detail=f"Evidence label(s) not available in this reasoning run: {invalid_labels}.",
        )

    if action_type in _REQUIRES_MESSAGE_EVIDENCE and not any(
        label.startswith("message_") for label in suggestion.evidence
    ):
        return ProposalRejection(
            reason=ProposalPolicyReason.INVALID_EVIDENCE,
            detail=(
                f"'{action_type.value}' requires at least one grounded message_N "
                "evidence label."
            ),
        )

    try:
        validated_payload = parse_action_payload(action_type, suggestion.payload)
    except (ActionNotExecutableError, ActionExecutorUnavailableError):
        return ProposalRejection(
            reason=ProposalPolicyReason.INVALID_PAYLOAD,
            detail=f"The candidate payload does not satisfy '{action_type.value}''s contract.",
        )

    recipient_rejection = _check_recipients(validated_payload, policy_input.allowed_recipients)
    if recipient_rejection is not None:
        return recipient_rejection

    return AllowedProposal(
        action_type=action_type,
        summary=suggestion.summary,
        validated_payload=validated_payload,
        reason=suggestion.reason,
        confidence=suggestion.confidence,
        grounded_evidence=tuple(suggestion.evidence),
    )


def _check_recipients(
    payload: ExecutablePayload, allowed_recipients: frozenset[str]
) -> ProposalRejection | None:
    """`None` means the recipients (if this payload type has any) are
    acceptable. Only `SendEmailPayload` carries recipients at all in M7.1;
    see the module docstring's "Recipient authority" for why the match must
    be exact, case-insensitive, and order/duplicate-insensitive."""
    if not isinstance(payload, SendEmailPayload):
        return None
    candidate = frozenset(payload.recipients)  # already lower-cased by SendEmailPayload itself
    expected = frozenset(address.lower() for address in allowed_recipients)
    if candidate != expected:
        return ProposalRejection(
            reason=ProposalPolicyReason.RECIPIENT_MISMATCH,
            detail=(
                "The candidate recipients do not exactly match the recipients "
                "allowed for this conversation."
            ),
        )
    return None
