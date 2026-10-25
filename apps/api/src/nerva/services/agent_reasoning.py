"""M7.1d — the first end-to-end wiring of agent reasoning: the one service
that turns a stored conversation into, at most, one `Action(PROPOSED)`.

    Conversation + current ConversationAnalysis + selected Memory context
            v
    AgentReasoningInput                    (nerva_ai.agent_client, M7.1a)
            v
    AgentReasoningClient.reason            (Protocol — a fake today)
            v
    AgentReasoningDecision
            v
    assemble_trusted_recipients            (nerva_core.send_email_assembly, M7.1l.2/M7.1l.3 —
                                             SEND_EMAIL only; NoActionDecision passes through)
            v
    decide_proposal                        (nerva_core.proposal_policy, M7.1b)
            v
    NO_ACTION / POLICY_REJECTED            or AllowedProposal
                                                   v
                               ProposalMaterializationService.materialize
                                                   v
                               ACTION_CREATED / ACTION_DUPLICATE   (M7.1c)

Nothing here approves an action, executes one, renders a prompt, calls
OpenAI, or touches Gmail. There is no HTTP route for this block either (see
"Explicit invocation only" below) and no new database table: the only thing
persisted by a run of this service is whatever M7.1c already persists — one
`Action(PROPOSED)` plus the single `ACTION_PROPOSED` `ActivityEvent`
`ActionService.propose` already writes. A `NO_ACTION` or `POLICY_REJECTED`
run persists nothing at all and records no `ActivityEvent`.

## Provider-neutral by construction

`__init__` takes an `AgentReasoningClient` — the Protocol (M7.1a), never
`tests.fakes.agent_reasoning.FakeAgentReasoningClient` and never a concrete
provider class. This module imports no provider implementation, exactly like
`AnalysisService` depends only on `AIAnalysisClient`; tests inject the fake,
and whatever real provider a later block adds is injected the same way with
no change here. This module does not know, and must never branch on, which
provider it was given — `provider`/`model_id` are read for observability
only (reported on `AgentReasoningResult`, never used in a decision and
deliberately never hashed — see `nerva_core.agent_reasoning_fingerprint`'s
"Deliberately excluded" section).

## Explicit invocation only

`reason_and_propose(conversation_id)` is called by a caller that decided to
reason — nothing in this block calls it automatically. It is deliberately
*not* wired into `AnalysisService.analyze`, `routes/analysis.py`, the
analysis backfill, or Gmail sync, and no public endpoint exposes it: the
whole loop is exercisable from the service layer and from tests, which is
enough for this block and avoids introducing automatic proposal creation in
production before a real provider, a real prompt, and a human-facing review
surface exist. Adding a trigger later is a wiring decision for the block
that needs it, not something this module anticipates or depends on.

## Preconditions: fail closed before the reasoner, never after

Everything the reasoner needs is resolved — and every reason to refuse is
checked — *before* `reason()` is called, so a run that cannot be grounded
costs zero provider calls:

- An unknown or cross-workspace `conversation_id` raises `NotFoundError`,
  the same way `AnalysisService.analyze`/`MemoryContextService.
  get_context_for_conversation` already do for one.
- No `ConversationAnalysis` for the conversation, one whose `status` is not
  `COMPLETED` (`PENDING`/`FAILED`), or a `COMPLETED` one missing any result
  field `ReasoningAnalysisContext` requires, raises
  `AnalysisNotReadyError`. The last case is defense in depth: those columns
  are nullable at the database level (a `FAILED` first attempt writes a row
  with every result field cleared — see `nerva.services.analysis.
  _clear_result_fields`), so a `COMPLETED` row with a `NULL` `category` is
  an inconsistency this service refuses to paper over with a guessed
  default rather than one it can prove impossible.
- No resolvable recipient raises `RecipientNotResolvableError` — see
  "Recipient authority" below.

`MemoryContextService` failures are deliberately not caught, for exactly the
reasons `AnalysisService`'s own module docstring already spells out: there is
no failure mode out of `get_context_for_conversation` that is both provably
memory-specific and safe to degrade into an empty context, so an
authorization/tenant/corruption failure must fail this call outright rather
than quietly reason over less than it should.

## Recipient authority: `Contact.email`, and nothing else

The recipient a `SEND_EMAIL` proposal is allowed to name comes from exactly
one place: the `Contact` row named by `Conversation.contact_id`, read
through the workspace-scoped `ContactRepository`. It is never taken from the
`ActionSuggestion`'s payload, from text inside a message body, from a
`Memory` statement, or from anything else the model produced.

**M7.1l.3: assembled, not merely compared.** Through M7.1l.2, the only thing
enforcing this was `decide_proposal`'s `RECIPIENT_MISMATCH` gate *comparing*
a model-supplied candidate against `allowed_recipients` — a model-produced
`recipients` value could reach `ProposalPolicy` at all, it would just be
rejected if wrong. As of this milestone, `reason_and_propose` calls
`nerva_core.send_email_assembly.assemble_trusted_recipients` on every
`ActionSuggestion` immediately after `reason()` returns and *before*
`ProposalPolicyInput` is ever constructed — passing `prepared.
allowed_recipients` (this same `Contact.email`-derived set, already resolved
by `_resolve_contact`/`_prepare`, never re-queried) as `trusted_recipients`.
A model/attacker-supplied `payload["recipients"]` is therefore unconditionally
overwritten before `decide_proposal` ever sees the decision — `RECIPIENT_
MISMATCH` remains as defense in depth (and is still the only thing that can
reject a *direct* call to `decide_proposal`, e.g. from a future caller that
does not route through this service), but the automatic orchestration path
no longer depends on the model ever supplying the right value, or indeed on
it supplying one at all: see `nerva_ai.providers._send_email_wire`'s own
docstring for why a fresh `SEND_EMAIL` suggestion's payload is `{"subject":
..., "body": ...}` with no `recipients` key at all. `NoActionDecision` is
never passed to the assembler — see `reason_and_propose`'s own docstring.

Why this source is unambiguous: `Conversation.contact_id` is a plain,
single-valued tenant foreign key, and `Contact.email` is a single column
with a `CHECK (email = lower(email))` constraint — so "the recipient for
this conversation" is exactly one already-lower-cased address, or nothing.
There is no tie-break rule to invent. `Message.recipients` is deliberately
never consulted: it is an unstructured, comma-separated display string (see
`nerva.models.conversations.Message`), not a joinable relationship, and
parsing addresses out of it would be precisely the silent invented rule this
block was told not to add.

**Known limitations, all failing closed or deferred rather than guessed:**

- A conversation with `contact_id IS NULL` (nothing identified a
  counterpart — e.g. a Gmail thread whose only message is outbound to the
  connected account itself, see `IntegrationService._counterpart_email`)
  cannot be reasoned about at all. It raises, rather than reasoning with an
  empty `allowed_recipients` that M7.1b would then reject anyway: calling a
  provider for a run that cannot possibly produce a valid `SEND_EMAIL` is
  waste, not safety.
- Multi-participant threads are **not** supported as multi-recipient
  proposals. Gmail sync already collapses a thread to one `contact_id` by
  its own heuristic; this service reasons about replying to that one
  counterpart and nothing else. Proposing to email several participants
  would need a product decision about who is authoritative, plus a widened
  `allowed_recipients` set — neither exists.
- Aliases are not resolved. If a counterpart writes from two addresses, each
  is its own `Contact`, and only the one on `Conversation.contact_id` is
  authorized.
- A self-addressed conversation (the connected mailbox's own address as the
  counterpart) is not detected. Doing so would mean treating
  `Integration.external_account_email` as an identity key, which that
  model's own docstring explicitly says it is not ("Display only; never used
  as an identity key"). The worst case is a proposal to email oneself, which
  still requires a human approval to do anything at all — a quality gap, not
  a security one. Closing it needs a real account-identity field, not a
  comparison this service invents against a display value.

## One memory selection, one evidence namespace

`MemoryContextService.get_context_for_conversation` is called exactly once
per run. That single selection becomes `AgentReasoningInput.memory_context`,
and `build_evidence_labels` derives the `memory_N` labels from that same
built object — so the labels shown to the reasoner and the labels
`decide_proposal` accepts are, structurally, the same set: there is no
second query, no re-selection, and no independent label construction that
could drift. `ConversationAnalysis.memory_ids_used` is deliberately *not*
used as a memory source: it is the audit snapshot of what produced the
stored analysis (see that column's own docstring), not current context.

Memory content stays untrusted data throughout. It is bounded and
id-stripped by the same `build_memory_context_items` the analysis path uses,
and nothing in a statement can widen `capabilities`, add an evidence label,
or change a recipient — all three come from this module and the database,
never from reasoner-visible content.

## Reuse of the analysis input builder

`build_analysis_input` (`nerva.services.analysis`) is called to produce the
conversation half of the reasoning input, and its `subject`/`messages`/
`memory_context` are lifted onto `AgentReasoningInput` unchanged. That is
deliberate reuse, not convenience: quote-stripping, per-message truncation,
the total-character budget, the newest-messages-win ordering rule and the
`sender_label` ("You" for outbound, display name or email for inbound)
convention are then provably identical to what analysis already sends a
provider, with no second copy of those bounds to drift. The `message_N`
labels therefore also count exactly the messages that survived those bounds
— never the raw row count.
"""

import uuid
from dataclasses import dataclass
from enum import Enum, auto

from sqlalchemy.orm import Session

from nerva.core.context import WorkspaceContext
from nerva.core.enums import ActionType, AnalysisStatus
from nerva.core.errors import NotFoundError
from nerva.models import Action, Contact, Conversation, ConversationAnalysis
from nerva.nerva_ai.agent_client import (
    ActionSuggestion,
    AgentReasoningClient,
    AgentReasoningInput,
    ReasoningAnalysisContext,
)
from nerva.nerva_ai.schemas import DeadlineEstimate, MonetaryEstimate
from nerva.nerva_core.agent_reasoning_fingerprint import (
    AGENT_REASONING_INPUT_VERSION,
    compute_agent_reasoning_fingerprint,
)
from nerva.nerva_core.proposal_fingerprint import ProposalProvenance
from nerva.nerva_core.proposal_policy import (
    PROPOSAL_POLICY_VERSION,
    AllowedProposal,
    ProposalPolicyInput,
    ProposalPolicyReason,
    ProposalRejection,
    decide_proposal,
)
from nerva.nerva_core.send_email_assembly import assemble_trusted_recipients
from nerva.repositories.workspace_data import (
    ContactRepository,
    ConversationAnalysisRepository,
    ConversationRepository,
    MessageRepository,
)
from nerva.services.analysis import build_analysis_input, build_memory_context_items
from nerva.services.base import ScopedService
from nerva.services.memory_context import MemoryContextService
from nerva.services.proposal_materialization import (
    MaterializedProposal,
    ProposalMaterializationService,
)

# The closed set of `ActionType`s a reasoner may be told is available in this
# milestone — the *system's* declaration, never the model's. `CREATE_TASK`
# has no executable payload contract at all (see `nerva_actions.payloads`),
# and `UPDATE_OPPORTUNITY_STAGE` would need an authoritative, unambiguous
# `Action.opportunity_id` for this conversation, which `MemoryContextService`'s
# own "Known limitation" already shows is not always resolvable. Both are
# additionally rejected by `nerva_core.proposal_policy`'s independent
# hardcoded allowlist regardless of what this tuple ever says — the two
# checks are deliberately not the same list (see that module's "Reasoner
# capabilities vs. policy allowlist").
REASONING_CAPABILITIES: tuple[ActionType, ...] = (ActionType.SEND_EMAIL,)

# Bounds on the memory context *this* pipeline asks for. Deliberately its own
# constants rather than reusing `nerva.services.analysis.
# ANALYSIS_MEMORY_MAX_ITEMS`/`ANALYSIS_MEMORY_MAX_CHARS`: those bound a
# different provider call made at a different time, and sharing one constant
# would couple two pipelines that have no reason to move together. Same values
# today, by choice, not by dependency.
REASONING_MEMORY_MAX_ITEMS = 10
REASONING_MEMORY_MAX_CHARS = 4_000

# The fixed label for `AgentReasoningInput.analysis` — M7.1a's convention,
# not a name this module invents (see `nerva_ai.agent_client`'s "Evidence
# labels" section, and `_EVIDENCE_LABEL_PATTERN`, which is what actually
# constrains an `ActionSuggestion.evidence` entry's shape).
ANALYSIS_EVIDENCE_LABEL = "analysis"


class AgentReasoningPreconditionError(Exception):
    """A run could not even be attempted: something the reasoner must be
    given could not be resolved from trusted application data. Always raised
    *before* any provider call, so no `reason()` happened and nothing was
    persisted. Never used for a reasoner or policy *outcome* — those are
    ordinary `AgentReasoningResult`s, not errors."""


class AnalysisNotReadyError(AgentReasoningPreconditionError):
    """There is no current, completed `ConversationAnalysis` to reason over —
    none exists, it is `PENDING`/`FAILED`, or a `COMPLETED` row is missing a
    result field `ReasoningAnalysisContext` requires. See the module
    docstring's "Preconditions"."""


class RecipientNotResolvableError(AgentReasoningPreconditionError):
    """No single authoritative recipient could be resolved for this
    conversation from trusted application data. See the module docstring's
    "Recipient authority" for the one source consulted and why the
    alternatives are refused rather than guessed."""


class ReasoningOutcomeStatus(Enum):
    """What one `reason_and_propose` run actually did. Four distinct states,
    never collapsed into a boolean or an `Action | None`: a reasoner that
    declined, a policy that refused, a new proposal and an already-existing
    identical proposal are four different things a caller (and an operator
    reading a log) must be able to tell apart."""

    NO_ACTION = auto()
    POLICY_REJECTED = auto()
    ACTION_CREATED = auto()
    ACTION_DUPLICATE = auto()


@dataclass(frozen=True, slots=True)
class AgentReasoningResult:
    """The outcome of one run. `action` is set only for `ACTION_CREATED` (the
    new row) and `ACTION_DUPLICATE` (the pre-existing one), and is `None`
    for `NO_ACTION`/`POLICY_REJECTED` — nothing was persisted in those cases.

    `policy_reason`/`policy_detail` come straight from `decide_proposal`;
    `policy_detail` is this codebase's own fixed explanation (never
    model-authored text) and is for logging only — branch on `status` or
    `policy_reason`, never on a string. `reasoning_fingerprint` is the digest
    of the exact `AgentReasoningInput` that was sent, which is also what
    reached `ProposalProvenance.source_fingerprint` when an `Action` was
    created. `provider`/`model_id` are observability only (see the module
    docstring's "Provider-neutral by construction")."""

    status: ReasoningOutcomeStatus
    policy_reason: ProposalPolicyReason
    policy_detail: str | None
    reasoning_fingerprint: str
    provider: str
    model_id: str
    action: Action | None

    @property
    def action_id(self) -> uuid.UUID | None:
        return self.action.id if self.action is not None else None


@dataclass(frozen=True, slots=True)
class ReasoningRequest:
    """One run's complete, internally-consistent reasoning state: the exact
    `AgentReasoningInput` a provider is handed, plus the two trusted facts
    `decide_proposal` must check that input's *output* against.

    Bundled into one object on purpose. `evidence_labels` is derived from
    `request` itself (`build_evidence_labels`), and `allowed_recipients` from
    the database — so the only way to build a `ProposalPolicyInput` is from
    the same object that was reasoned over, and "the labels the reasoner saw"
    cannot drift from "the labels the policy accepts" (see the module
    docstring's "One memory selection, one evidence namespace")."""

    request: AgentReasoningInput
    evidence_labels: frozenset[str]
    allowed_recipients: frozenset[str]
    # Internal ids kept here, deliberately never on `request`: M7.1a's
    # `AgentReasoningInput` carries none, and these two are needed only by
    # `ProposalProvenance` — this workspace's own audit trail, which a
    # provider never sees.
    conversation_id: uuid.UUID
    analysis_id: uuid.UUID


def build_evidence_labels(request: AgentReasoningInput) -> frozenset[str]:
    """The exact, closed set of evidence labels that exist for `request` —
    derived from `request` itself, never from a separate count of rows and
    never from anything the model returned.

    The convention (`message_{i+1}`, `memory_{i+1}`, `analysis`) is M7.1a's,
    defined in `nerva_ai.agent_client`'s module docstring and enforced in
    shape by its `_EVIDENCE_LABEL_PATTERN`; this function is what makes the
    *existence* half of that convention concrete for one particular input,
    which is exactly the set `ProposalPolicyInput.available_evidence_labels`
    is specified to carry. `analysis` is always present: `AgentReasoningInput.
    analysis` is a required field, so there is no input without it."""
    labels = {f"message_{index + 1}" for index in range(len(request.messages))}
    labels |= {f"memory_{index + 1}" for index in range(len(request.memory_context))}
    labels.add(ANALYSIS_EVIDENCE_LABEL)
    return frozenset(labels)


def build_reasoning_analysis_context(analysis: ConversationAnalysis) -> ReasoningAnalysisContext:
    """Maps a completed `ConversationAnalysis` row onto the minimal,
    already-safe slice M7.1a hands a reasoner — never the row, never its id,
    never `input_fingerprint`/`provider`/`model_id`/`memory_ids_used`.

    Raises `AnalysisNotReadyError` if any required result field is `NULL`:
    those columns are nullable at the database level even though a
    `COMPLETED` row written by `AnalysisService._persist_success` always
    fills them (`ConversationAnalysisResult` requires every one of them), so
    this is defense in depth against an inconsistent row — fail closed, never
    substitute a default the analysis never produced. The genuinely optional
    fields (`sentiment`, `intent`, `suggested_next_step`, and both
    estimates) pass through as `None` unchanged: `None` there is a real
    analysis outcome, not a missing value (see `nerva.models.analysis`'s own
    docstring)."""
    required = {
        "category": analysis.category,
        "priority": analysis.priority,
        "requires_response": analysis.requires_response,
        "commercial_signal": analysis.commercial_signal,
        "confidence": analysis.confidence,
        "summary": analysis.summary,
    }
    missing = sorted(name for name, value in required.items() if value is None)
    if missing:
        raise AnalysisNotReadyError(
            f"The completed analysis is missing required result field(s): {missing}."
        )
    # Narrowed by the `missing` check above; `assert` rather than `cast` so a
    # future refactor that drops a field from `required` fails loudly here.
    assert analysis.category is not None
    assert analysis.priority is not None
    assert analysis.requires_response is not None
    assert analysis.commercial_signal is not None
    assert analysis.confidence is not None
    assert analysis.summary is not None

    return ReasoningAnalysisContext(
        category=analysis.category,
        priority=analysis.priority,
        sentiment=analysis.sentiment,
        requires_response=analysis.requires_response,
        commercial_signal=analysis.commercial_signal,
        confidence=analysis.confidence,
        summary=analysis.summary,
        intent=analysis.intent,
        suggested_next_step=analysis.suggested_next_step,
        estimated_value=_estimated_value(analysis),
        deadline=_deadline(analysis),
    )


def _estimated_value(analysis: ConversationAnalysis) -> MonetaryEstimate | None:
    """Reassembles the three columns `AnalysisService._persist_success` flattened
    a `MonetaryEstimate` into. All three are written together or not at all, so a
    partially-set triple is an inconsistency, not a value to salvage — `None` is
    returned only when `estimated_value_cents` is `NULL`, and `MonetaryEstimate`'s
    own validation rejects anything else."""
    if analysis.estimated_value_cents is None:
        return None
    return MonetaryEstimate(
        amount_cents=analysis.estimated_value_cents,
        currency=analysis.value_currency or "",
        is_explicit=bool(analysis.value_is_explicit),
    )


def _deadline(analysis: ConversationAnalysis) -> DeadlineEstimate | None:
    if analysis.deadline_at is None:
        return None
    return DeadlineEstimate(
        date=analysis.deadline_at, is_explicit=bool(analysis.deadline_is_explicit)
    )


class AgentReasoningService(ScopedService):
    """Orchestration only: every decision this service reaches is reached by
    code that already owns it — `MemoryContextService`/`select_context_
    memories` for which memories, the injected `AgentReasoningClient` for
    whether to suggest anything, `decide_proposal` for whether a suggestion
    is safe, `ProposalMaterializationService` for whether it becomes a row.
    No eligibility, ranking, validation or deduplication rule is
    re-implemented here."""

    def __init__(
        self, session: Session, ctx: WorkspaceContext, *, reasoning_client: AgentReasoningClient
    ) -> None:
        super().__init__(session, ctx)
        self.reasoning_client = reasoning_client
        self.conversations = ConversationRepository(session, ctx.workspace_id)
        self.messages = MessageRepository(session, ctx.workspace_id)
        self.analyses = ConversationAnalysisRepository(session, ctx.workspace_id)
        self.contacts = ContactRepository(session, ctx.workspace_id)
        self.memory_context = MemoryContextService(session, ctx)
        self.materialization = ProposalMaterializationService(session, ctx)

    def reason_and_propose(self, conversation_id: uuid.UUID) -> AgentReasoningResult:
        """Reasons about one conversation and, at most, proposes one action.

        Calls the injected reasoning client exactly once, and only after every
        precondition has been satisfied from trusted application data. Raises
        `NotFoundError` if the conversation is not in this workspace,
        `AnalysisNotReadyError` if there is no completed analysis to reason
        over, or `RecipientNotResolvableError` if no authoritative recipient
        exists — in all three cases without any provider call. A provider
        failure propagates as the `nerva_ai.errors.AIError` subclass the
        client raised (see "Reasoner failures" on this method) and never
        creates an `Action`.

        Never approves and never executes: the only thing it can create is an
        `Action` in `PROPOSED`, attributed to `Actor.nerva()` by
        `ProposalMaterializationService` — which is also the only writer of
        the single `ACTION_PROPOSED` `ActivityEvent` involved.

        ## Reasoner failures

        An `AIError` from `reason()` is deliberately **not** wrapped in a
        reasoning-specific error hierarchy, unlike `AnalysisService.analyze`'s
        `AnalysisAuthError`/`AnalysisProviderError` family. Those exist
        because that method must first persist a `failed`
        `ConversationAnalysis` row and then hand its HTTP route a typed,
        already-recorded failure. Neither applies here: there is no reasoning
        row to persist (this block adds no table), and there is no route to
        map onto. Re-raising the provider's own typed error unchanged is both
        the least invention and the most information — the same reasoning
        `nerva_ai.agent_client`'s own docstring already gives for not
        introducing a parallel error hierarchy for a boundary with no real
        implementation yet. Nothing partial is left behind either way: the
        failure happens strictly before anything is written.

        ## Recipient assembly, never the model's (M7.1l.3)

        `outcome.decision` is passed through `nerva_core.send_email_assembly.
        assemble_trusted_recipients` — with `prepared.allowed_recipients` (the
        same trusted `Contact.email`-derived set `decide_proposal` is also
        given, resolved once by `_prepare`, never re-queried) as
        `trusted_recipients` — before `ProposalPolicyInput` is ever built.
        `NoActionDecision` is left untouched: the assembler is only ever
        called with an `ActionSuggestion`, and `decide_proposal` continues to
        see `ProposalPolicyReason.REASONER_NO_ACTION` exactly as before M7.1l.3
        for that case. See the module docstring's "Recipient authority" for
        why this makes any `recipients` value a model (or an attacker
        prompt-injecting one into it) ever supplied structurally irrelevant to
        the automatic path, not merely rejected by comparison.

        `assemble_trusted_recipients` raises `nerva_core.send_email_assembly.
        EmptyTrustedRecipientsError` if `trusted_recipients` is empty for a
        `SEND_EMAIL` suggestion — unreachable today, since `_resolve_contact`
        already fails closed with `RecipientNotResolvableError` before any
        memory selection or provider call if no non-empty `Contact.email` can
        be resolved (see `_prepare`). This method does not catch it: exactly
        like an `AIError` from `reason()` (see "Reasoner failures" above),
        re-raising it unchanged is the least invention and the most
        information, and it is raised strictly before `ProposalPolicyInput` is
        built, `decide_proposal` is called, or anything is persisted — so no
        `Action`, no `ActivityEvent`, no second provider call, and no
        fallback to anything the model supplied, ever happen on this path.
        """
        prepared = self._prepare(conversation_id)
        fingerprint = compute_agent_reasoning_fingerprint(prepared.request)

        # Exactly one provider call per valid invocation. No retry, no second
        # call on an unexpected decision shape: an invalid or hostile decision
        # is `decide_proposal`'s business, not a reason to ask again.
        outcome = self.reasoning_client.reason(prepared.request)

        # SEND_EMAIL only (see "Recipient assembly, never the model's" above);
        # `NoActionDecision` passes through untouched. Must happen before
        # `ProposalPolicyInput` is built — the model-produced, incomplete (or
        # attacker-tampered) payload must never reach `decide_proposal`.
        assembled_decision = (
            assemble_trusted_recipients(
                outcome.decision, trusted_recipients=prepared.allowed_recipients
            )
            if isinstance(outcome.decision, ActionSuggestion)
            else outcome.decision
        )

        decision = decide_proposal(
            ProposalPolicyInput(
                decision=assembled_decision,
                # All three from `prepared`, never re-derived from the model's
                # output or re-queried from the database — see
                # `ReasoningRequest`'s own docstring.
                declared_capabilities=frozenset(prepared.request.capabilities),
                available_evidence_labels=prepared.evidence_labels,
                allowed_recipients=prepared.allowed_recipients,
            )
        )

        if isinstance(decision, ProposalRejection):
            return self._rejected(decision, fingerprint)
        return self._materialized(decision, prepared, fingerprint, outcome.prompt_version)

    # ------------------------------------------------------------------
    # Input preparation (everything below runs before any provider call)
    # ------------------------------------------------------------------

    def _prepare(self, conversation_id: uuid.UUID) -> ReasoningRequest:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            raise NotFoundError("Conversation not found.")

        analysis = self.analyses.get_for_conversation(conversation_id)
        if analysis is None:
            raise AnalysisNotReadyError("This conversation has no analysis to reason over.")
        if analysis.status != AnalysisStatus.COMPLETED:
            raise AnalysisNotReadyError(
                f"This conversation's analysis is '{analysis.status.value}', not completed."
            )
        analysis_context = build_reasoning_analysis_context(analysis)

        # Resolved before the reasoner is called, and before any memory is
        # even selected: a conversation with no authoritative recipient can
        # never produce a valid `SEND_EMAIL`, so reasoning about it would be
        # a provider call spent on a guaranteed rejection.
        contact = self._resolve_contact(conversation)
        allowed_recipients = frozenset({contact.email.lower()})

        messages = self.messages.list_for_conversation(conversation_id)
        # Called exactly once. Not caught: see the module docstring's
        # "Preconditions" for why a Block 2 failure must fail this run.
        selected_memories = self.memory_context.get_context_for_conversation(
            conversation_id=conversation_id,
            max_items=REASONING_MEMORY_MAX_ITEMS,
            max_chars=REASONING_MEMORY_MAX_CHARS,
        )
        # The same bounded, id-free, quote-stripped shapes the analysis path
        # sends a provider — reused, not reimplemented (see the module
        # docstring's "Reuse of the analysis input builder").
        analysis_input = build_analysis_input(
            conversation, messages, build_memory_context_items(selected_memories)
        )

        request = AgentReasoningInput(
            subject=analysis_input.subject,
            # A display name only, never `Contact.email` — M7.1a's own
            # restraint. The authoritative address stays in
            # `allowed_recipients`, which the model never sees.
            contact_display_name=contact.name,
            messages=analysis_input.messages,
            analysis=analysis_context,
            memory_context=analysis_input.memory_context,
            capabilities=REASONING_CAPABILITIES,
        )
        return ReasoningRequest(
            request=request,
            evidence_labels=build_evidence_labels(request),
            allowed_recipients=allowed_recipients,
            conversation_id=conversation.id,
            analysis_id=analysis.id,
        )

    def _resolve_contact(self, conversation: Conversation) -> Contact:
        """The one authoritative source of a recipient for this conversation —
        see the module docstring's "Recipient authority" for why it is this
        and nothing else, and for the limitations that fail closed here."""
        if conversation.contact_id is None:
            raise RecipientNotResolvableError(
                "This conversation has no contact, so no recipient can be authorized."
            )
        # Workspace-scoped read: a `contact_id` that somehow named another
        # workspace's row could not resolve here (and the composite tenant
        # foreign key on `Conversation` makes such a row impossible to create
        # in the first place — see `docs/ARCHITECTURE.md` §4).
        contact = self.contacts.get(conversation.contact_id)
        if contact is None:
            raise RecipientNotResolvableError(
                "This conversation's contact could not be resolved in this workspace."
            )
        if not contact.email:
            raise RecipientNotResolvableError("This conversation's contact has no email address.")
        return contact

    # ------------------------------------------------------------------
    # Outcome mapping
    # ------------------------------------------------------------------

    def _rejected(self, decision: ProposalRejection, fingerprint: str) -> AgentReasoningResult:
        """`REASONER_NO_ACTION` is the reasoner declining — a legitimate,
        expected outcome, reported as `NO_ACTION`. Every other rejection code
        is the *policy* refusing something the model produced (an invalid
        payload, an invented evidence label, an attacker-supplied recipient,
        a capability it was never offered) and is reported as
        `POLICY_REJECTED`.

        Neither is an exception. `decide_proposal` already handled the unsafe
        output safely, and raising here would turn a successfully-defended
        malicious or malformed model response into a server error — exactly
        what M7.1b's "a safe, ordinary outcome of untrusted model output" is
        for. Neither writes anything: no `Action`, and no `ActivityEvent` (the
        only one in this pipeline is the `ACTION_PROPOSED` event
        `ActionService.propose` writes when a row is actually created)."""
        status = (
            ReasoningOutcomeStatus.NO_ACTION
            if decision.reason is ProposalPolicyReason.REASONER_NO_ACTION
            else ReasoningOutcomeStatus.POLICY_REJECTED
        )
        return self._result(
            status=status,
            policy_reason=decision.reason,
            policy_detail=decision.detail,
            fingerprint=fingerprint,
            action=None,
        )

    def _materialized(
        self,
        allowed: AllowedProposal,
        prepared: ReasoningRequest,
        fingerprint: str,
        provider_prompt_version: str,
    ) -> AgentReasoningResult:
        """`ProposalProvenance` is assembled here and nowhere else:
        `source_fingerprint` is the digest of the exact input just reasoned
        over (never recomputed from a possibly-changed row afterwards — see
        `nerva_core.proposal_fingerprint`'s "Reasoning state identity"),
        `analysis_id` the row that produced `ReasoningAnalysisContext`, and
        the version stamps that governed this run — `reasoning_version`/
        `proposal_policy_version` from this orchestration's own constants,
        and `provider_prompt_version` straight from `AgentReasoningOutcome.
        prompt_version` (M7.1e), never re-derived or guessed: this is the
        *only* place that value is read off an outcome, and the *only*
        place `ProposalProvenance.provider_prompt_version` is ever set.
        See `nerva_core.proposal_fingerprint`'s own docstring for why it is
        carried here for audit but excluded from the fingerprint's hash."""
        outcome = self.materialization.materialize(
            allowed,
            provenance=ProposalProvenance(
                conversation_id=prepared.conversation_id,
                analysis_id=prepared.analysis_id,
                source_fingerprint=fingerprint,
                reasoning_version=AGENT_REASONING_INPUT_VERSION,
                proposal_policy_version=PROPOSAL_POLICY_VERSION,
                provider_prompt_version=provider_prompt_version,
            ),
        )
        if isinstance(outcome, MaterializedProposal):
            return self._result(
                status=ReasoningOutcomeStatus.ACTION_CREATED,
                policy_reason=allowed.policy_reason,
                policy_detail=None,
                fingerprint=fingerprint,
                action=outcome.action,
            )
        return self._result(
            status=ReasoningOutcomeStatus.ACTION_DUPLICATE,
            policy_reason=allowed.policy_reason,
            policy_detail=None,
            fingerprint=fingerprint,
            action=outcome.existing_action,
        )

    def _result(
        self,
        *,
        status: ReasoningOutcomeStatus,
        policy_reason: ProposalPolicyReason,
        policy_detail: str | None,
        fingerprint: str,
        action: Action | None,
    ) -> AgentReasoningResult:
        return AgentReasoningResult(
            status=status,
            policy_reason=policy_reason,
            policy_detail=policy_detail,
            reasoning_fingerprint=fingerprint,
            provider=self.reasoning_client.provider,
            model_id=self.reasoning_client.model_id,
            action=action,
        )
