"""M7.1c — the deterministic identity of an automatic proposal.

    ProposalProvenance + AllowedProposal (M7.1b's safe, validated product)
            v
    compute_proposal_fingerprint
            v
    a stable SHA-256 hex digest — "this exact proposal, from this exact
    reasoning state, under these exact versions" — persisted as
    `Action.proposal_fingerprint` and enforced unique per workspace by a
    partial index (`nerva.models.actions.PROPOSAL_FINGERPRINT_UNIQUE_INDEX`)

Pure — no database, no HTTP, no provider call — mirroring `nerva.services.
analysis.compute_fingerprint` exactly: same canonicalization technique
(`json.dumps(..., sort_keys=True, separators=(",", ":"))` then SHA-256), same
"no timestamp of 'now', no random value, ever" guarantee, so the same inputs
always produce the same fingerprint regardless of which process or when it
ran.

## Reasoning state identity — supplied, never recomputed

`ConversationAnalysis.id` cannot identify a reasoning state on its own: that
row is updated in place on re-analysis (see `nerva.models.analysis`'s own
module docstring), so the same id can describe different content at
different times. `ProposalProvenance.source_fingerprint` is what actually
pins "this specific reasoning state" — the exact same role `Memory.
source_fingerprint`/`BusinessSignal.source_fingerprint` already play. This
module does not — and architecturally cannot — recompute that value itself:
it has no database access, and recomputing it from a *current*
`ConversationAnalysis` row would silently substitute "whatever the row says
now" for "what it said when this proposal was reasoned about," defeating the
entire point of pinning it. The orchestration layer
(`nerva.services.agent_reasoning`, M7.1d) is what captures it, at the moment
it builds an `AgentReasoningInput`, and passes the same value through here
unchanged.

**What that value actually is (M7.1d).** `ConversationAnalysis.
input_fingerprint` was the originally-anticipated source, and it is *not*
what M7.1d supplies: an `AgentReasoningInput` carries the current memory
context and contact display name as well as the analysis content, so the
analysis fingerprint alone would under-identify the state the reasoner
actually saw — two runs over the same analysis but a changed memory context
would collide. `nerva_core.agent_reasoning_fingerprint.
compute_agent_reasoning_fingerprint` is computed over the real
`AgentReasoningInput` instead, and is what reaches `source_fingerprint`.
Nothing in this module depends on which of the two it is: it only requires
that the caller captured it at reasoning time and passes it through
unchanged.

## What participates in the fingerprint, and why

Per the approved design: "same reasoning state + same effective action +
same relevant versions = same proposal fingerprint." Concretely:

- `provenance.conversation_id`, `.analysis_id`, `.source_fingerprint` — the
  reasoning state. `conversation_id` is included even though
  `source_fingerprint` alone is already, in practice, specific enough: two
  *different* conversations producing byte-identical `source_fingerprint`
  values (e.g. both brand new, zero messages, empty memory context) is not
  structurally impossible, and `conversation_id` closes that gap for free.
- `provenance.reasoning_version`, `.proposal_policy_version` — the
  versions that could change what "the same reasoning state" produces.
  `reasoning_version` was spelled `reasoning_prompt_version` as first
  written in M7.1c; it was renamed in M7.1d, before any fingerprint had
  ever been persisted outside a test, because no reasoning *prompt* exists
  to version — the name presupposed infrastructure the codebase does not
  have (there is deliberately no prompt-version constant in
  `nerva_ai.agent_client` either, for the same reason). A real provider
  that does render a prompt can add its own prompt version alongside this
  one later; naming this field after one now would have meant persisting a
  value that identifies nothing. `PROPOSAL_FINGERPRINT_SCHEMA_VERSION` was
  deliberately *not* bumped for that rename: the renamed JSON key already
  changes every digest it participates in, so old and new values cannot
  collide silently — which is the only thing a bump would have bought.
- `allowed.action_type` and a *canonical* form of `allowed.validated_payload`
  — the effective action. See `_canonical_payload` for exactly what
  "canonical" means for `SendEmailPayload` today.

Deliberately **excluded**: `allowed.confidence`, `.reason` (prose), `.summary`
(prose), and the order (or even presence vs. absence beyond M7.1b's own
non-empty requirement) of `.grounded_evidence`. None of these change what the
action *is* — only how it is explained — and letting any of them perturb the
fingerprint would make two requests that reasoned to the exact same email,
for the exact same reason, differ only in prose wording and yet be treated as
two different proposals. This is a deliberately *broader* reading than the
one explicit instruction ("evidence ordering must not change the
fingerprint") — the content of `grounded_evidence` is excluded entirely, not
just its order; see this module's own tests for the reasoning spelled out
case by case.

`PROPOSAL_FINGERPRINT_SCHEMA_VERSION` is folded into the hash input itself
(as a plain field, not a magic prefix) so that if this module's own
canonicalization rules ever change, old and new fingerprints are
guaranteed not to collide silently — the same "stamps that should also
invalidate" reasoning `nerva.services.analysis.compute_fingerprint` already
documents for `schema_version`/`prompt_version`/`model_id`.

## `provider_prompt_version` (M7.1e): provenance, deliberately not identity

`ProposalProvenance.provider_prompt_version` — the real provider's own
rendered-prompt version (e.g. `nerva_ai.agent_reasoning_prompts.
AGENT_REASONING_PROMPT_VERSION` for `OpenAIAgentReasoningProvider`) — is the
field this module's own earlier note anticipated ("A real provider that does
render a prompt can add its own prompt version alongside this one later").
It is captured on every `AllowedProposal` and recorded on the
`ACTION_PROPOSED` `ActivityEvent` for audit, exactly like `reasoning_version`/
`proposal_policy_version`, but **it is deliberately excluded from this
function's hash input.**

Why it differs from `reasoning_version`/`proposal_policy_version`, which
*are* included: those two name the versions of *contracts this module's own
design depends on* — a changed `AgentReasoningInput` shape or a changed
`ProposalPolicy` ruleset can make an old fingerprint describe a proposal that
would no longer even be reachable the same way, so folding them in is part
of "same reasoning state + same effective action + same relevant versions."
`provider_prompt_version` is neither: it is wording inside one provider's own
prompt, the same category of "how a decision was explained, not what it is"
`allowed.reason`/`.summary`/`.confidence` already fall into just above. Per
the design handed down for M7.1e: "same reasoning state + same effective
action + same ProposalPolicy = same proposal identity, even if we reworded
the provider prompt." Two runs over the same `AgentReasoningInput`, under the
same `ProposalPolicy`, that reason to the exact same `SEND_EMAIL` for the
exact same reason, must still collide into one pending proposal regardless of
which wording of the system prompt produced it — otherwise a harmless prompt
tweak would silently duplicate every proposal still pending re-approval.
`test_proposal_fingerprint.py` asserts this directly: two provenances
differing only in `provider_prompt_version` fingerprint identically.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass

from nerva.nerva_actions.payloads import ExecutablePayload, SendEmailPayload
from nerva.nerva_core.proposal_policy import AllowedProposal

# Bumped only if this module's own canonicalization rules change in a way
# that should invalidate every previously-computed fingerprint — see the
# module docstring.
PROPOSAL_FINGERPRINT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProposalProvenance:
    """The reasoning-state identity a proposal was produced from, and the
    versions that governed how it was produced — supplied entirely by the
    caller (`nerva.services.agent_reasoning.AgentReasoningService`), never
    recomputed or looked up by this module. See the module docstring's
    "Reasoning state identity" for why `source_fingerprint` in particular
    must be captured by the caller at reasoning time, not re-derived here
    from a (mutable) `ConversationAnalysis` row.

    `provider_prompt_version` (M7.1e) is `None` for a provider with no real
    prompt (never the case for a real caller: `AgentReasoningOutcome.
    prompt_version` is a required, non-optional field — the `None` default
    here only keeps a dataclass field with a default last; see the module
    docstring's "provider_prompt_version" section for why it is carried for
    audit but excluded from `compute_proposal_fingerprint`'s hash."""

    conversation_id: uuid.UUID
    analysis_id: uuid.UUID
    source_fingerprint: str
    reasoning_version: str
    proposal_policy_version: str
    provider_prompt_version: str | None = None


def compute_proposal_fingerprint(
    *, provenance: ProposalProvenance, allowed: AllowedProposal
) -> str:
    """A stable SHA-256 hex digest of exactly what makes `allowed` *this*
    proposal rather than some other one — see the module docstring for
    precisely what is, and is not, included. No timestamp, no random value,
    no database id of `allowed`/`provenance` themselves (there is none to
    begin with — both are plain, already-validated values, never ORM rows)."""
    normalized = {
        "schema_version": PROPOSAL_FINGERPRINT_SCHEMA_VERSION,
        "conversation_id": str(provenance.conversation_id),
        "analysis_id": str(provenance.analysis_id),
        "source_fingerprint": provenance.source_fingerprint,
        "reasoning_version": provenance.reasoning_version,
        "proposal_policy_version": provenance.proposal_policy_version,
        "action_type": allowed.action_type.value,
        "payload": _canonical_payload(allowed.validated_payload),
    }
    canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_payload(payload: ExecutablePayload) -> dict[str, object]:
    if isinstance(payload, SendEmailPayload):
        return _canonical_send_email_payload(payload)
    # `ProposalPolicy` (M7.1b) only ever allows `SEND_EMAIL` through today,
    # so `AllowedProposal.validated_payload` cannot actually be anything
    # else — but `ExecutablePayload` is a wider type, so this branch is kept
    # explicit (and loud) rather than silently falling through, the same
    # "fail closed on the unexpected" discipline the rest of this codebase
    # already applies (e.g. `GmailSendExecutor`'s own payload-type guard).
    raise TypeError(f"No canonical fingerprint representation for payload type {type(payload)!r}.")


def _canonical_send_email_payload(payload: SendEmailPayload) -> dict[str, object]:
    """Canonical form of a validated `SendEmailPayload` for fingerprinting.

    - `recipients`: de-duplicated and sorted. *Who* receives the email is
      part of its identity; the order they were listed in, and whether one
      address was repeated, are not — see the module docstring's broader
      "what participates" rationale. Already lower-cased by
      `SendEmailPayload`'s own validator, so no further case normalization
      happens here.
    - `subject`/`body`: included byte-for-byte, exactly as validated — no
      trimming, no case change, no semantic normalization of any kind.
      `"Hello"` and `"Hello!"` are different actions; this function must
      never decide otherwise on the caller's behalf.
    """
    return {
        "recipients": sorted(set(payload.recipients)),
        "subject": payload.subject,
        "body": payload.body,
    }
