"""The second real `AgentReasoningClient`: Groq's Chat Completions API with
JSON Schema Structured Outputs (M7.1g).

Mirrors `nerva_ai.providers.openai_agent_reasoning.OpenAIAgentReasoningProvider`
as closely as the two APIs allow — same prompt
(`nerva_ai.agent_reasoning_prompts`), same error-mapping discipline, same
"exactly one provider call, fail closed on anything else" rule. This module
adds Groq as an *alternative* engine behind the same `AgentReasoningClient`
Protocol — it never replaces `OpenAIAgentReasoningProvider`, never falls back
to it, and is never called by it. `ProposalPolicy`
(`nerva_core.proposal_policy.decide_proposal`) remains the only authority on
whether a decision this module returns is safe to act on; nothing here
approves, executes, or touches a database or Gmail.

## Wire contract: Groq-specific and flat, unlike OpenAI's (M7.1i.1)

Through M7.1g/M7.1h this module validated against the same shared, nested
wire contract OpenAI uses (`nerva_ai.providers._send_email_wire`). A real
M7.1h smoke test against `openai/gpt-oss-20b` showed that choice does not
hold in practice: Groq's own API rejected the model's response with
`json_validate_failed` because the model returned a flat object
(`{"decision": "no_action", "reason": ..., "confidence": ...}`) instead of
the required `{"decision": {...}}` envelope — even though the schema sent
was confirmed byte-identical to what OpenAI's own strict-mode converter
would produce for the same Pydantic model, and confirmed to be within
Groq's documented-supported JSON Schema subset (anyOf + $ref + nested
`$defs` unions are explicitly documented as supported). The M7.1i audit's
conclusion: this is a model/provider generation-reliability gap, not a
malformed schema, and the fix belongs in the wire layer, not the prompt or
`ProposalPolicy`.

This module now validates against `nerva_ai.providers._groq_send_email_wire.
_GroqWireReasoningResponse` — a Groq-only, flat wire shape where the
discriminator and every field are direct siblings on one object, never
nested — instead of the shared contract. See that module's own docstring for
exactly why a flat merged object, not a root-level `anyOf`, is the shape
used. `OpenAIAgentReasoningProvider` is completely unaffected: it continues
using `_send_email_wire._WireReasoningResponse` unchanged. Both providers'
wire responses still map onto the exact same `NoActionDecision`/
`ActionSuggestion` domain types before anything downstream (including
`ProposalPolicy`) ever sees them — this divergence is confined entirely to
"how does this one provider's API get asked for, and validate, its
response," never to what a decision *means*.

The only module in Nerva allowed to import the `groq` package — everything
else depends on `nerva_ai.agent_client.AgentReasoningClient` (see
`nerva.api.deps.get_agent_reasoning_client`), exactly as
`nerva_ai.providers.openai`/`openai_agent_reasoning` are the only modules
allowed to import `openai`.

## Why `response_format={"type": "json_schema", ...}`, never JSON Object mode

Groq's own documentation (verified against `console.groq.com/docs/
structured-outputs` during the M7.1g.1 audit) draws a hard line between
`{"type": "json_object"}` ("JSON Mode" — syntactically valid JSON, no
schema enforcement) and `{"type": "json_schema", "json_schema": {...}}`
("Structured Outputs" — schema-conformant, `strict` optionally guaranteed).
This module only ever uses the latter. JSON Object mode would accept
anything merely JSON-shaped, defeating the entire reason the wire contract
(`_groq_send_email_wire`) exists.

## Why a local `_strict_schema` helper, not a raw `model_json_schema()` call

Plain `pydantic.BaseModel.model_json_schema()` does not reproduce what
OpenAI's SDK's own `strict`-mode converter does for `OpenAIAgentReasoningProvider`
(confirmed during the M7.1g.1 audit — see that report's §2/§8): a field with
a default (an optional field such as `_GroqWireReasoningResponse.payload`),
is, by Pydantic's own ordinary rules, omittable from `required` even though
Structured Outputs' `strict` mode demands every declared property be listed
there regardless of nullability. Sending a schema that violates this would
either be silently accepted as non-strict (defeating the point) or rejected
by Groq's API outright — neither of which this module is willing to risk
discovering by trial and error against a real endpoint (M7.1g is mock-only;
see the module docstring's own "NO network calls" constraint, enforced by
every test in this package). `_strict_schema` walks the raw schema Pydantic
already produces (which, thanks to every wire class already declaring
`extra="forbid"`, already sets `additionalProperties: false` everywhere) and
forces `required` to equal `properties` at every object node, including
every entry under `$defs` — never removing a constraint, never widening the
schema, never adding a property that was not already there.
"""

import time
from typing import Any

import pydantic
from groq import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    GroqError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
)
from groq import Groq as GroqClient

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
from nerva.nerva_ai.providers._groq_send_email_wire import (
    _GroqWireReasoningResponse,
    _map_groq_wire_decision,
)

# Independent of `nerva_ai.providers.openai_agent_reasoning._TIMEOUT_SECONDS`/
# `_MAX_RETRIES` by design, not oversight — see that module's own comment on
# its matching constants: the two providers' budgets must be free to diverge
# later without coupling them today. Same values today, by choice.
_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 2

_RESPONSE_FORMAT_NAME = "agent_reasoning_decision"


def _strict_schema(model: type[pydantic.BaseModel]) -> dict[str, Any]:
    """`model.model_json_schema()`, walked to force Structured Outputs'
    `strict` requirement — every object node's `required` set to exactly its
    `properties` keys — onto every node in the tree, `$defs` included. See
    the module docstring's "Why a local `_strict_schema` helper" for exactly
    why plain `model_json_schema()` cannot be trusted to already satisfy
    this. Never removes an existing constraint (`additionalProperties`,
    `minItems`/`maxItems`, `pattern`, `enum`, …) and never adds a property
    that was not already declared — purely a `required`-list correction."""
    schema = model.model_json_schema()
    _force_required(schema)
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        for definition in defs.values():
            if isinstance(definition, dict):
                _force_required(definition)
    return schema


def _force_required(node: dict[str, Any]) -> None:
    """Mutates one object schema node in place: if it declares `properties`,
    `required` becomes exactly that key set, regardless of what Pydantic's
    own default-aware logic originally put there. A no-op on a node with no
    `properties` (e.g. a `$ref`-only node) — there is nothing to require."""
    properties = node.get("properties")
    if isinstance(properties, dict) and properties:
        node["required"] = list(properties.keys())


class GroqAgentReasoningProvider:
    """The real `AgentReasoningClient` backed by Groq. Tests inject a
    stand-in for `client` exposing the same `chat.completions.create`
    surface (see `tests/test_groq_agent_reasoning_provider.py`) — never a
    real network call, mirroring `OpenAIAgentReasoningProvider`'s own test
    discipline exactly.

    `model_id` has no fallback (unlike `OpenAIAgentReasoningProvider`'s
    fallback to `nerva_ai_model`): constructing this class with a blank
    model id is a caller bug, not something this class papers over — see
    `nerva.api.deps.get_agent_reasoning_client`, the only real caller, which
    never constructs this class unless `Settings.groq_provider_configured`
    already confirmed both the key and the model are set."""

    provider = "groq"

    def __init__(self, settings: Settings, *, client: GroqClient | None = None) -> None:
        self.model_id = settings.groq_agent_reasoning_model
        self._client = client or GroqClient(
            api_key=settings.groq_api_key,
            timeout=_TIMEOUT_SECONDS,
            max_retries=_MAX_RETRIES,
        )

    def reason(self, request: AgentReasoningInput) -> AgentReasoningOutcome:
        started = time.monotonic()
        try:
            completion = self._client.chat.completions.create(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": AGENT_REASONING_SYSTEM_PROMPT},
                    {"role": "user", "content": build_agent_reasoning_user_prompt(request)},
                ],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": _RESPONSE_FORMAT_NAME,
                        "schema": _strict_schema(_GroqWireReasoningResponse),
                        "strict": True,
                    },
                },
            )
        except AuthenticationError as exc:
            raise AIAuthError(f"Groq rejected our credentials: {exc.message}") from exc
        except PermissionDeniedError as exc:
            raise AIAuthError(f"Groq denied permission for this request: {exc.message}") from exc
        except RateLimitError as exc:
            raise AIRateLimitError(
                f"Groq is rate-limiting requests: {exc.message}",
                retry_after=_retry_after_seconds(exc),
            ) from exc
        except (APIConnectionError, APITimeoutError) as exc:
            raise AIProviderError(f"Could not reach Groq: {exc.message}") from exc
        except (BadRequestError, NotFoundError, UnprocessableEntityError) as exc:
            raise AIProviderError(f"Groq rejected our request: {exc.message}") from exc
        except APIStatusError as exc:
            # Catches 5xx (`InternalServerError`), 409 (`ConflictError`), and any
            # other status Groq's API returns that is not one of the specific
            # cases already handled above — the same "fail closed on the
            # unexpected" discipline `OpenAIAgentReasoningProvider` applies to
            # its own `APIStatusError` catch-all.
            raise AIProviderError(f"Groq returned {exc.status_code}: {exc.message}") from exc
        except GroqError as exc:
            raise AIProviderError(f"Groq request failed: {exc}") from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        wire = _extract_wire_response(completion)
        decision = _map_groq_wire_decision(wire)
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


def _extract_wire_response(completion: Any) -> _GroqWireReasoningResponse:
    """Validates the raw response content against the wire contract — never
    trusting Groq's `strict: true` request flag as a guarantee the response
    actually honored it (see the module docstring's "Why a local
    `_strict_schema` helper": this is exactly the same fail-closed discipline
    `OpenAIAgentReasoningProvider`'s own `except pydantic.ValidationError`
    branch already applies, just reached via `create()` + manual parsing
    instead of a `.parse()` convenience — no such convenience is assumed to
    exist for the `groq` SDK). Malformed JSON, a schema violation, an
    unsupported decision variant, or any extra field all fail the same way:
    `AIOutputValidationError`, never a second attempt, never a repair."""
    if not completion.choices:
        raise AIOutputValidationError("Groq returned no choices.")
    message = completion.choices[0].message
    content = message.content
    if not content:
        raise AIOutputValidationError("Groq's response had no content to parse.")
    try:
        return _GroqWireReasoningResponse.model_validate_json(content)
    except pydantic.ValidationError as exc:
        raise AIOutputValidationError("Groq's response did not match the expected schema.") from exc


def _retry_after_seconds(exc: RateLimitError) -> float | None:
    header = exc.response.headers.get("retry-after")
    if header is None:
        return None
    try:
        return float(header)
    except ValueError:
        return None
