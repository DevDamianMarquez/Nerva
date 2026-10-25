"""The real `AgentReasoningClient`: OpenAI's Chat Completions API with
Structured Outputs — the same technique, same SDK, and (barring the
differences documented below) the same error-mapping discipline as
`nerva_ai.providers.openai.OpenAIProvider`, this module's sibling for
conversation analysis.

The only module in Nerva, besides `nerva_ai.providers.openai`, allowed to
import the `openai` package: everything else depends on `nerva_ai.
agent_client.AgentReasoningClient` (see `nerva.api.deps.
get_agent_reasoning_client`). Never imports `nerva.services`, a database
module, or Gmail — this module only ever receives an already-built
`AgentReasoningInput` and returns an already-validated `AgentReasoningOutcome`.
`ProposalPolicy` (`nerva_core.proposal_policy.decide_proposal`) is not called
here and never will be: this module's only job is "ask the model, validate
its structural shape" — whether a validated suggestion is actually safe to
act on is entirely `nerva.services.agent_reasoning.AgentReasoningService`'s
business, downstream of this class's return value. See that service's own
module docstring for the full boundary: `OpenAI output -> AgentReasoningDecision
-> ProposalPolicy`, never `OpenAI output -> Action` directly — nothing in
this file is allowed to, or attempts to, shorten that path.

## Why a private wire schema, not `ActionSuggestion`/`NoActionDecision`

`nerva_ai.agent_client.ActionSuggestion.payload` is `dict[str, JsonValue]` —
a genuinely free-form JSON object. OpenAI's `strict` Structured Outputs mode
requires every object in the schema to declare `additionalProperties: false`;
a dict with arbitrary keys and arbitrary value types cannot satisfy that (its
schema is `{"type": "object", "additionalProperties": <some ref>}`, not
`false`) — passing `ActionSuggestion`/`NoActionDecision` directly as
`response_format` would build a schema OpenAI's API rejects. (Confirmed by
inspecting the schema the SDK actually generates for those two classes
pre-implementation; see the M7.1e audit for the exact JSON produced.)

So this provider validates against a provider-neutral "wire" schema
(`_WireNoAction`/`_WireSendEmailSuggestion`, wrapped in
`_WireReasoningResponse` — `nerva_ai.providers._send_email_wire`, M7.1g) with
a concretely-typed `payload` — `subject`/`body` as plain fields (no
`recipients` field at all since M7.1l.1; see that module's own docstring) —
instead of a dict. That module's `_map_wire_decision` turns a validated wire
response into the real `AgentReasoningDecision` (`NoActionDecision`/
`ActionSuggestion`) *after* parsing, so `nerva_ai.agent_client`,
`nerva_core.proposal_policy`, and every other M7.1a-d module are completely
unaware this mapping step exists — they still only ever see the one true
contract. `nerva_ai.providers.groq_agent_reasoning` (M7.1g) validates against
the exact same wire contract — this provider does not own it anymore, it
only still owns how the contract reaches OpenAI's Structured Outputs API
(`response_format=_WireReasoningResponse`, OpenAI SDK `.parse()`) and how
OpenAI's own errors map onto it.

**The wire schema permits only `send_email`, structurally — not merely by
prompt instruction.** `_WireReasoningResponse.decision` is a closed union of
exactly `_WireNoAction | _WireSendEmailSuggestion`: there is no `create_task`
or `update_opportunity_stage` variant for the model to select, because no
such variant exists in this schema. This is defense in depth beyond the
prompt's own CAPABILITIES wording (`nerva_ai.agent_reasoning_prompts`) — the
model cannot even *express* a suggestion of a type this policy would reject,
regardless of what it was asked or what it was told was possible. A future
capability needs a new wire variant here (mirroring the day
`nerva_core.proposal_policy._ALLOWED_ACTION_TYPES` needs a new entry) — this
coupling is deliberate, not an oversight: see `_WireSendEmailSuggestion`'s own
docstring.

**Never an authorization.** Mapping a wire response into `ActionSuggestion`
only proves the model's output is *shaped* like a candidate — exactly the
same "three separate questions" discipline `nerva_ai.agent_client`'s own
docstring already lays out for `ActionSuggestion` in general. `evidence`
labels are re-validated for *shape* by `ActionSuggestion`'s own Pydantic
constraints at construction time (same regex as `nerva_ai.agent_client.
_EVIDENCE_LABEL_PATTERN`, duplicated here deliberately — see
`_EvidenceLabel`'s own comment for why); their *existence* in this specific
request, and the action type's eligibility, are `nerva_core.proposal_policy.
decide_proposal`'s job alone, entirely outside this module.

**Recipient authority (M7.1l.1-M7.1l.3): never this module's, and no longer
`ProposalPolicy`'s alone either.** This provider — and the wire schema it
validates against — never owns a recipient at all: the model drafts
`subject`/`body` only, and nothing here ever sees, renders, or produces an
email address for the reply. The actual trusted recipient is injected by
`nerva.services.agent_reasoning.AgentReasoningService.reason_and_propose`,
which calls `nerva_core.send_email_assembly.assemble_trusted_recipients`
*after* this provider's `reason()` returns and *before* `decide_proposal`
ever sees the decision — overwriting (never merging with) anything a
model/attacker-tampered payload might otherwise have carried.
`nerva_core.proposal_policy.decide_proposal`'s own `RECIPIENT_MISMATCH` gate
still independently re-validates exact recipient equality on every call —
defense in depth, not the sole authority: a direct call to `decide_proposal`
that is handed a mismatched recipient is still rejected on its own, exactly
as before this provider ever produced anything.
"""

import time

import pydantic
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    ContentFilterFinishReasonError,
    LengthFinishReasonError,
    OpenAI,
    OpenAIError,
    RateLimitError,
)
from openai.types.chat import ParsedChatCompletion

from nerva.core.config import Settings
from nerva.nerva_ai.agent_client import (
    REASONING_SCHEMA_VERSION,
    AgentReasoningInput,
    AgentReasoningOutcome,
)
from nerva.nerva_ai.agent_reasoning_prompts import (
    AGENT_REASONING_PROMPT_VERSION,
    AGENT_REASONING_SYSTEM_PROMPT,
    build_agent_reasoning_user_prompt,
)
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from nerva.nerva_ai.providers._send_email_wire import _map_wire_decision, _WireReasoningResponse

# Mirrors `nerva_ai.providers.openai.OpenAIProvider`'s own constants exactly —
# see that module for the reasoning (generous relative to typical latency,
# transient-transport retries handled beneath this by the SDK itself). Kept
# as this module's own constants, not imported, so the two providers' timeout
# budgets can diverge later without coupling them today.
_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 2


class OpenAIAgentReasoningProvider:
    """The real implementation of `nerva_ai.agent_client.AgentReasoningClient`.
    Tests inject a stand-in for `client` exposing the same `chat.completions.
    parse` surface (see `tests/test_openai_agent_reasoning_provider.py`) —
    never a real network call, mirroring `OpenAIProvider`'s own test
    discipline exactly."""

    provider = "openai"

    def __init__(self, settings: Settings, *, client: OpenAI | None = None) -> None:
        self.model_id = settings.agent_reasoning_model
        self._client = client or OpenAI(
            api_key=settings.openai_api_key,
            timeout=_TIMEOUT_SECONDS,
            max_retries=_MAX_RETRIES,
        )

    def reason(self, request: AgentReasoningInput) -> AgentReasoningOutcome:
        started = time.monotonic()
        try:
            completion = self._client.chat.completions.parse(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": AGENT_REASONING_SYSTEM_PROMPT},
                    {"role": "user", "content": build_agent_reasoning_user_prompt(request)},
                ],
                response_format=_WireReasoningResponse,
            )
        except AuthenticationError as exc:
            raise AIAuthError(f"OpenAI rejected our credentials: {exc.message}") from exc
        except RateLimitError as exc:
            raise AIRateLimitError(
                f"OpenAI is rate-limiting requests: {exc.message}",
                retry_after=_retry_after_seconds(exc),
            ) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise AIProviderError(f"Could not reach OpenAI: {exc.message}") from exc
        except APIStatusError as exc:
            raise AIProviderError(f"OpenAI returned {exc.status_code}: {exc.message}") from exc
        except LengthFinishReasonError as exc:
            raise AIOutputValidationError(
                "OpenAI's response was truncated before it could be parsed."
            ) from exc
        except ContentFilterFinishReasonError as exc:
            raise AIOutputValidationError("OpenAI's content filter rejected the request.") from exc
        except OpenAIError as exc:
            raise AIProviderError(f"OpenAI request failed: {exc}") from exc
        except pydantic.ValidationError as exc:
            # Structured Outputs constrains the model to our JSON schema, so this
            # should be rare, but the model's JSON must never be trusted as
            # already-valid: the same discipline as any other provider boundary
            # (and the only place `_EvidenceLabel`'s pattern is actually
            # enforced — see that constant's own comment).
            raise AIOutputValidationError(
                "OpenAI's response did not match the expected schema."
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        wire = _extract_wire_response(completion)
        decision = _map_wire_decision(wire)
        usage = completion.usage
        return AgentReasoningOutcome(
            decision=decision,
            schema_version=REASONING_SCHEMA_VERSION,
            model_id=completion.model or self.model_id,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=latency_ms,
            prompt_version=AGENT_REASONING_PROMPT_VERSION,
        )


def _extract_wire_response(
    completion: ParsedChatCompletion[_WireReasoningResponse],
) -> _WireReasoningResponse:
    if not completion.choices:
        raise AIOutputValidationError("OpenAI returned no choices.")
    message = completion.choices[0].message
    if message.refusal:
        raise AIOutputValidationError("OpenAI declined to reason about this conversation.")
    if message.parsed is None:
        raise AIOutputValidationError("OpenAI's response did not match the expected schema.")
    return message.parsed


def _retry_after_seconds(exc: RateLimitError) -> float | None:
    header = exc.response.headers.get("retry-after")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
