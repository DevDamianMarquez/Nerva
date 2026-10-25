"""M7.1l.2 — the pure, deterministic assembler that gives
`ActionSuggestion.payload["recipients"]` its only legitimate source: Nerva's
own trusted recipient resolution, never the model.

    ActionSuggestion (SEND_EMAIL, model-drafted subject/body only — M7.1l.1)
            +
    trusted_recipients: frozenset[str]   (resolved by Nerva, e.g.
                                           `AgentReasoningService._resolve_contact`)
            v
    assemble_trusted_recipients
            v
    ActionSuggestion (SEND_EMAIL, payload now also carries `recipients`)
            v
    [FUTURE — M7.1l.3, not this block] nerva_core.proposal_policy.decide_proposal

Pure — no database, no HTTP, no provider call, no `ActionService`, no
SQLAlchemy, no network — mirroring every other `nerva_core` rule module
(`proposal_policy`, `rules`, `memory_rules`) exactly. This module is
deliberately **not** wired into `nerva.services.agent_reasoning.
AgentReasoningService` yet (that is M7.1l.3) — it exists on its own, fully
unit-testable in isolation first.

## Why this exists at all (M7.1l.1 recap)

Through M7.1g/M7.1h/M7.1j, every provider's wire schema asked the model for
`recipients` even though it was never shown a real address to base one on —
`nerva_core.proposal_policy`'s `RECIPIENT_MISMATCH` gate was the only thing
actually enforcing the real recipient either way. M7.1l.1 removed
`recipients` from the model-facing wire contract entirely
(`nerva_ai.providers._send_email_wire._WireSendEmailPayload`): a provider now
drafts `subject`/`body` only. `ActionSuggestion.payload` for a freshly
produced `SEND_EMAIL` suggestion is therefore `{"subject": ..., "body":
...}` — missing the one key `nerva_actions.payloads.SendEmailPayload`
requires. This module is what fills that gap back in, from a source the
model never touches.

## Defensive, not merely structural

`_WireSendEmailPayload` now makes it structurally impossible for a *wire*
response to carry `recipients` — but `ActionSuggestion.payload` itself stays
a free-form `dict[str, JsonValue]` (see that class's own docstring): nothing
stops a hand-built, fake, or future-provider `ActionSuggestion` from
containing a `"recipients"` key anyway. This module never reads
`suggestion.payload["recipients"]` for any reason, under any circumstance —
the output's `recipients` come exclusively from `trusted_recipients`, always
replacing whatever (if anything) the input payload already had there, never
merged with it. See `test_send_email_assembly.py`'s attacker-recipient tests
for the proof.

## Fail closed on an empty trusted set

A caller is expected to always resolve a non-empty `trusted_recipients`
before invoking this for `SEND_EMAIL` — but "expected to" is not a
guarantee this module gets to assume silently. An empty set raises
`EmptyTrustedRecipientsError` rather than producing `payload["recipients"]
== []` (which would otherwise fail far downstream, through
`SendEmailPayload.recipients`'s own `min_length=1`, for an unrelated-looking
reason) or falling back to anything in `suggestion.payload`. This mirrors
`nerva.services.agent_reasoning.RecipientNotResolvableError` one layer up
(that one is raised when `Contact.email` cannot be resolved at all, before a
reasoning call is even made) — this module defines its own exception rather
than importing that one because `nerva_core` stays pure and must never
depend on a DB-aware service module.

## What this module does not do

`ProposalPolicy` (`nerva_core.proposal_policy.decide_proposal`) remains the
only authority that validates the *resulting* payload — this module never
calls `parse_action_payload`, never checks `subject`/`body` bounds, never
checks evidence grounding, never checks the action-type allowlist, and
never checks that a trusted address is even a syntactically valid email. A
malformed `subject`, a malformed trusted address, or any other structural
problem survives assembly unchanged and is expected to fail later, through
`ProposalPolicy`/`SendEmailPayload`, exactly as it would have before this
module existed. Duplicating any of that checking here would just be a
second, drifting copy of `ProposalPolicy`'s own job.

For any action type other than `SEND_EMAIL`, this module injects nothing:
the input `ActionSuggestion` is returned as-is (the same object, not a
field-for-field copy of it) since recipient authority is a
`SEND_EMAIL`-specific concept that does not yet apply to anything else
`ProposalPolicy` allows.
"""

from pydantic import JsonValue

from nerva.core.enums import ActionType
from nerva.nerva_ai.agent_client import ActionSuggestion


class EmptyTrustedRecipientsError(Exception):
    """Raised by `assemble_trusted_recipients` when asked to assemble a
    `SEND_EMAIL` suggestion with no trusted recipient at all. A caller
    (M7.1l.3) must resolve at least one trusted address — e.g. via
    `nerva.services.agent_reasoning.AgentReasoningService._resolve_contact`
    — before ever calling this function for `SEND_EMAIL`; this module never
    guesses, and never silently produces an empty `recipients` list."""


def assemble_trusted_recipients(
    suggestion: ActionSuggestion, *, trusted_recipients: frozenset[str]
) -> ActionSuggestion:
    """Returns a new `ActionSuggestion` whose `SEND_EMAIL` payload carries
    `recipients` sourced exclusively from `trusted_recipients` — never from
    `suggestion.payload`, which may already contain an attacker- or
    model-supplied `"recipients"` key that is unconditionally discarded, not
    merged with. `trusted_recipients` is sorted before being written, so the
    same set always produces the same `recipients` list regardless of the
    frozenset's own (arbitrary) iteration order — see the module docstring
    for why that matters for `nerva_core.proposal_fingerprint`.

    For any `action_type` other than `SEND_EMAIL`, returns `suggestion`
    itself unchanged — no recipient of any kind is this module's concern for
    an action type that does not have one yet.

    Never mutates `suggestion` or its `.payload` dict in place.

    Raises `EmptyTrustedRecipientsError` if `action_type` is `SEND_EMAIL`
    and `trusted_recipients` is empty — see the module docstring's "Fail
    closed on an empty trusted set"."""
    if suggestion.action_type is not ActionType.SEND_EMAIL:
        return suggestion

    if not trusted_recipients:
        raise EmptyTrustedRecipientsError(
            "SEND_EMAIL requires at least one trusted recipient; none were provided."
        )

    new_payload: dict[str, JsonValue] = dict(suggestion.payload)
    new_payload["recipients"] = list[JsonValue](sorted(trusted_recipients))

    return ActionSuggestion(
        action_type=suggestion.action_type,
        summary=suggestion.summary,
        payload=new_payload,
        confidence=suggestion.confidence,
        reason=suggestion.reason,
        evidence=list(suggestion.evidence),
    )
