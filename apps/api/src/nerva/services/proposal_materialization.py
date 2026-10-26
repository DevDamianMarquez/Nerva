"""M7.1c — the one service allowed to turn M7.1b's `AllowedProposal` into a
persisted `Action(PROPOSED)`.

    AllowedProposal (M7.1b's safe, validated product) + ProposalProvenance
            v
    compute_proposal_fingerprint            (nerva_core.proposal_fingerprint)
            v
    ProposalMaterializationService.materialize
            v
    MaterializedProposal | DuplicateProposal   (ProposalMaterializationOutcome)

Never accepts a raw `ActionSuggestion` — only the already-policy-checked
`AllowedProposal` (M7.1b). Never approves, never executes, never calls an AI
provider or Gmail: those remain exactly where M5 (execution) and a future
M7.1d (orchestration, not built here) already put them. `Actor.nerva()` is
the only actor this service ever uses — see "Security boundaries" below.

## Atomic deduplication, not a blanket `IntegrityError` catch

`SELECT existing; if none: INSERT` has a real race: two concurrent calls can
both see "nothing yet" and both insert. The actual guarantee is the
database's own partial unique index on `(workspace_id, proposal_fingerprint)`
(`nerva.models.actions.PROPOSAL_FINGERPRINT_UNIQUE_INDEX`) — this service
*attempts* the insert unconditionally and lets PostgreSQL be the only thing
that ever decides "duplicate," rather than pre-checking and racing anyway.

Catching `IntegrityError` broadly (the way `ContactService.create`/
`UserService` already do for their own, simpler, single-unique-constraint
cases) would be unsafe here: `Action` has more than one constraint that can
raise one (`actor_consistency_check`, the `opportunity_id` tenant FK, and now
this index), and `Actor.nerva()` proposals never set `opportunity_id` at
all, so a genuine bug elsewhere could otherwise be silently swallowed as an
ordinary, expected `DuplicateProposal`. `_is_proposal_fingerprint_violation`
inspects the DBAPI error's own `diag.constraint_name` (psycopg's own
PostgreSQL diagnostics) and only treats *this exact* index's violation as a
duplicate; anything else re-raises unchanged.

## Why `ActionService.propose(..., commit=False)` + a SAVEPOINT

`ActionService.propose` is kept as the *only* semantic path that creates an
`Action` — see `nerva.services.actions`'s own docstring for the minimal,
backwards-compatible `proposal_fingerprint`/`provenance`/`commit` parameters
added for exactly this caller. The actual `INSERT` (and therefore the unique
-index check) happens inside `propose` (via `ActionRepository.add`'s
`flush()`), before `propose`'s own `commit()` would run — so this service
wraps the call in `Session.begin_nested()` and calls `propose(commit=False)`,
the same SAVEPOINT composition `MemoryService.replace` already documents at
length for an analogous reason: a failed `flush()` here must only undo *this
one insert attempt*, never anything else the caller's session may already
hold pending, and the row lock (there is none needed here — uniqueness is
index-enforced, not row-locked) is irrelevant to that distinction.

## Security boundaries

This service receives `AllowedProposal`, already fully checked by M7.1b —
it does not re-interpret or re-validate the model's original output, and it
has no path back to a raw `ActionSuggestion` at all (there is no parameter
for one). It cannot change the recipient (the payload it persists is
`allowed.validated_payload.model_dump(mode="json")`, the exact object
`ProposalPolicy` already validated — never reconstructed from scratch here),
cannot add an `ActionType` `ProposalPolicy` did not already allow, cannot
approve (`ActionService.approve`/`.reject` are never called here), cannot
execute (`ActionExecutionEngine` is never constructed here), and cannot use
a human actor (`Actor.nerva()` is hardcoded — there is no parameter to
override it, matching `ActionService.start_execution`/`.complete`/`.fail`'s
own "intentionally not a caller-supplied value" precedent).

## Provenance: internal ids are correct here

The "never an internal id reaches the model" rule (M7.1a/M7.1b) is about
what an LLM is ever shown — it says nothing about this workspace's own audit
trail. `_provenance_event_data` builds exactly the metadata merged into the
*same* `ACTION_PROPOSED` `ActivityEvent` `ActionService.propose` already
writes (never a second event — see that method's own docstring):
`conversation_id`, `analysis_id`, `source_fingerprint`,
`reasoning_version`, `proposal_policy_version`, `provider_prompt_version`
(M7.1e — `None` until a real provider supplies one),
`proposal_fingerprint`, `grounded_evidence`, and the model's own
`confidence`/`reason` — all of it either an internal id, a version stamp, or
already-safe text `AllowedProposal`/`ProposalProvenance` themselves carry.
Never a raw memory statement, a raw conversation/message body, a secret, an
OAuth token, or a provider prompt's *rendered text* — `provider_prompt_version`
is a version stamp identifying which wording was used, not the wording
itself, the same distinction `reasoning_version`/`proposal_policy_version`
already draw for their own contracts.
"""

from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from nerva.core.context import Actor, WorkspaceContext
from nerva.core.enums import ActionType
from nerva.models import Action
from nerva.models.actions import PROPOSAL_FINGERPRINT_UNIQUE_INDEX
from nerva.nerva_core.proposal_fingerprint import ProposalProvenance, compute_proposal_fingerprint
from nerva.nerva_core.proposal_policy import AllowedProposal
from nerva.repositories.workspace_data import ActionRepository
from nerva.schemas.actions import ActionCreate
from nerva.services.actions import ActionService
from nerva.services.base import ScopedService


@dataclass(frozen=True, slots=True)
class MaterializedProposal:
    """`CREATED`: a new `Action(PROPOSED)` row, attributed to `Actor.nerva()`,
    with `proposal_fingerprint` set."""

    action: Action


@dataclass(frozen=True, slots=True)
class DuplicateProposal:
    """`DUPLICATE`: an automatic proposal with this exact identity already
    existed in this workspace before this call — re-fetched after the
    unique-violation rollback. No new `Action`, and no new
    `ACTION_PROPOSED` event, was created by this call."""

    existing_action: Action


# A discriminated union, not `Action | None`: a duplicate is a distinct,
# equally first-class outcome (the caller may still want the existing row),
# not a degenerate "nothing happened" — the same reasoning
# `nerva_ai.agent_client.AgentReasoningDecision`/`nerva_core.proposal_policy.
# ProposalPolicyDecision` already apply one layer up.
ProposalMaterializationOutcome = MaterializedProposal | DuplicateProposal


class ProposalMaterializationService(ScopedService):
    def __init__(self, session: Session, ctx: WorkspaceContext) -> None:
        super().__init__(session, ctx)
        self._actions = ActionService(session, ctx)
        self._action_repo = ActionRepository(session, ctx.workspace_id)

    def materialize(
        self, allowed: AllowedProposal, *, provenance: ProposalProvenance
    ) -> ProposalMaterializationOutcome:
        """Attempts to persist `allowed` as a new `Action(PROPOSED)`. If an
        automatic proposal with the exact same `proposal_fingerprint`
        already exists in this workspace — including one created by a
        genuinely concurrent call — returns `DuplicateProposal` instead of a
        second `Action`, and never a second `ACTION_PROPOSED` event. See the
        module docstring for the atomicity this relies on."""
        fingerprint = compute_proposal_fingerprint(provenance=provenance, allowed=allowed)
        event_data = _provenance_event_data(provenance, allowed, fingerprint)

        nested = self.session.begin_nested()
        try:
            action = self._actions.propose(
                ActionCreate(
                    type=ActionType(allowed.action_type),
                    summary=allowed.summary,
                    payload=allowed.validated_payload.model_dump(mode="json"),
                ),
                actor=Actor.nerva(),
                proposal_fingerprint=fingerprint,
                provenance=event_data,
                # The only authoritative source (M8.1) — never a second,
                # independently-suppliable `conversation_id` parameter on this
                # method. See the module docstring's "Provenance".
                conversation_id=provenance.conversation_id,
                commit=False,
            )
            nested.commit()
        except IntegrityError as error:
            nested.rollback()
            if not _is_proposal_fingerprint_violation(error):
                raise
            existing = self._action_repo.get_by_proposal_fingerprint(fingerprint)
            if existing is None:
                # PostgreSQL only raises this violation against an already
                # -committed conflicting row (see the module docstring) — a
                # read that then fails to find it is not an ordinary
                # duplicate, and this service must not guess at one.
                raise
            return DuplicateProposal(existing_action=existing)

        self.session.commit()
        return MaterializedProposal(action=action)


def _provenance_event_data(
    provenance: ProposalProvenance, allowed: AllowedProposal, proposal_fingerprint: str
) -> dict[str, Any]:
    """Everything recorded on the single `ACTION_PROPOSED` `ActivityEvent`
    for an automatic proposal — see the module docstring's "Provenance"."""
    return {
        "conversation_id": str(provenance.conversation_id),
        "analysis_id": str(provenance.analysis_id),
        "source_fingerprint": provenance.source_fingerprint,
        "reasoning_version": provenance.reasoning_version,
        "proposal_policy_version": provenance.proposal_policy_version,
        "provider_prompt_version": provenance.provider_prompt_version,
        "proposal_fingerprint": proposal_fingerprint,
        "grounded_evidence": list(allowed.grounded_evidence),
        "confidence": allowed.confidence,
        "reason": allowed.reason,
    }


def _is_proposal_fingerprint_violation(error: IntegrityError) -> bool:
    """True only if `error` is specifically the partial unique index on
    `(workspace_id, proposal_fingerprint)` — never a blanket "any
    `IntegrityError` means duplicate" assumption. See the module docstring's
    "Atomic deduplication, not a blanket IntegrityError catch"."""
    orig = getattr(error, "orig", None)
    diag = getattr(orig, "diag", None)
    constraint_name = getattr(diag, "constraint_name", None)
    return constraint_name == PROPOSAL_FINGERPRINT_UNIQUE_INDEX
