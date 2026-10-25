"""`nerva_ai.providers.openai_agent_reasoning.OpenAIAgentReasoningProvider`:
the real `AgentReasoningClient` (M7.1e).

Every test injects a stand-in for the underlying `openai.OpenAI` client's
`chat.completions.parse` method — never a real network call. Failure tests
raise the SDK's actual exception classes (constructed directly, with no HTTP
call made) so the mapping in `OpenAIAgentReasoningProvider.reason` is
verified against the real type hierarchy, not a hand-rolled substitute —
mirrors `test_openai_provider.py`'s own discipline exactly, including its
`_StubCompletions`/`_StubClient`/`_status_error` helpers (duplicated here
rather than imported, the same way `test_proposal_fingerprint.py`/`test_
proposal_materialization.py` each keep their own small row builders rather
than sharing across test modules).

After every successful `.reason()` call in this file:

    OpenAI output -> AgentReasoningDecision

and nothing further. `ProposalPolicy` is never invoked here — this module
only proves the provider boundary maps correctly; `test_agent_reasoning_
service.py` is what proves the *whole* loop (provider -> policy ->
materialization) stays safe end to end, with the fake provider standing in.
"""

from types import SimpleNamespace

import httpx2
import pydantic
import pytest
from openai import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    ContentFilterFinishReasonError,
    InternalServerError,
    LengthFinishReasonError,
    RateLimitError,
)

from nerva.api.deps import get_agent_reasoning_client
from nerva.core.config import Settings
from nerva.core.enums import ActionType, ConversationCategory, ConversationPriority
from nerva.nerva_ai.agent_client import (
    REASONING_SCHEMA_VERSION,
    ActionSuggestion,
    AgentReasoningInput,
    NoActionDecision,
    ReasoningAnalysisContext,
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
from nerva.nerva_ai.providers._send_email_wire import (
    _WireNoAction,
    _WireReasoningResponse,
    _WireSendEmailPayload,
    _WireSendEmailSuggestion,
)
from nerva.nerva_ai.providers.openai_agent_reasoning import OpenAIAgentReasoningProvider
from nerva.nerva_ai.unconfigured import UnconfiguredAgentReasoningClient

SOME_ANALYSIS = ReasoningAnalysisContext(
    category=ConversationCategory.SALES_LEAD,
    priority=ConversationPriority.MEDIUM,
    sentiment=None,
    requires_response=True,
    commercial_signal=True,
    confidence=0.8,
    summary="Prospect asked about pricing for 3 seats.",
    intent="request_quote",
    suggested_next_step="Send pricing information.",
    estimated_value=None,
    deadline=None,
)

SOME_REQUEST = AgentReasoningInput(
    subject="Pricing for 3 seats",
    contact_display_name="Priya Nair",
    messages=(),
    analysis=SOME_ANALYSIS,
    capabilities=(ActionType.SEND_EMAIL,),
)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {
        "openai_api_key": "sk-test",
        "nerva_agent_reasoning_model": "gpt-4o-mini",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class _StubCompletions:
    """Stands in for `client.chat.completions`. Scripted via `result`
    (returned from `.parse()`) or `raises` (raised instead) — every call is
    recorded so a test can assert on exactly what was sent."""

    def __init__(self, *, result: object = None, raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    def parse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


class _StubClient:
    def __init__(self, completions: _StubCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _wire_no_action(**overrides: object) -> _WireNoAction:
    defaults: dict[str, object] = {"reason": "Nothing warrants a reply yet.", "confidence": 0.9}
    defaults.update(overrides)
    return _WireNoAction.model_validate(defaults)


def _wire_send_email(**overrides: object) -> _WireSendEmailSuggestion:
    defaults: dict[str, object] = {
        "summary": "Reply with pricing for 3 seats.",
        "payload": {
            "subject": "Pricing for 3 seats",
            "body": "Hi Priya, here is the pricing you asked about.",
        },
        "confidence": 0.8,
        "reason": "The prospect explicitly asked for pricing.",
        "evidence": ["message_1"],
    }
    defaults.update(overrides)
    return _WireSendEmailSuggestion.model_validate(defaults)


def _completion(
    *,
    parsed: _WireReasoningResponse | None,
    refusal: str | None = None,
    model: str = "gpt-4o-mini-2024-07-18",
    usage: SimpleNamespace | None = SimpleNamespace(prompt_tokens=200, completion_tokens=60),
    choices: list[object] | None = None,
) -> SimpleNamespace:
    if choices is None:
        message = SimpleNamespace(parsed=parsed, refusal=refusal)
        choices = [SimpleNamespace(message=message)]
    return SimpleNamespace(choices=choices, usage=usage, model=model)


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.openai.com/v1/chat/completions")


def _status_error(
    cls: type[Exception], status_code: int, message: str, *, headers: dict[str, str] | None = None
) -> Exception:
    response = httpx2.Response(
        status_code, request=_request(), headers=headers, json={"error": {"message": message}}
    )
    return cls(message, response=response, body={"error": {"message": message}})


# ---------------------------------------------------------------------------
# A. provider / model_id / DI
# ---------------------------------------------------------------------------


def test_provider_and_model_id_are_known_before_any_call() -> None:
    provider = OpenAIAgentReasoningProvider(
        _settings(nerva_agent_reasoning_model="gpt-4o-mini"), client=_StubClient(_StubCompletions())
    )

    assert provider.provider == "openai"
    assert provider.model_id == "gpt-4o-mini"


def test_model_id_falls_back_to_nerva_ai_model_when_unset() -> None:
    provider = OpenAIAgentReasoningProvider(
        _settings(nerva_agent_reasoning_model="", nerva_ai_model="gpt-4o-mini"),
        client=_StubClient(_StubCompletions()),
    )

    assert provider.model_id == "gpt-4o-mini"


def test_get_agent_reasoning_client_returns_unconfigured_client_without_an_api_key() -> None:
    client = get_agent_reasoning_client(Settings(openai_api_key=""))

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_get_agent_reasoning_client_returns_the_real_provider_once_an_api_key_is_set() -> None:
    client = get_agent_reasoning_client(_settings(nerva_agent_reasoning_model="gpt-4o-mini"))

    assert isinstance(client, OpenAIAgentReasoningProvider)
    assert client.model_id == "gpt-4o-mini"


def test_unconfigured_agent_reasoning_client_raises_ai_auth_error() -> None:
    with pytest.raises(AIAuthError):
        UnconfiguredAgentReasoningClient().reason(SOME_REQUEST)


# ---------------------------------------------------------------------------
# B. NO_ACTION parse/mapping
# ---------------------------------------------------------------------------


def test_reason_maps_a_wire_no_action_to_no_action_decision() -> None:
    wire = _WireReasoningResponse(
        decision=_wire_no_action(reason="Already answered.", confidence=0.7)
    )
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, NoActionDecision)
    assert outcome.decision.reason == "Already answered."
    assert outcome.decision.confidence == 0.7


# ---------------------------------------------------------------------------
# C. SEND_EMAIL parse/mapping (subject/body, evidence,
#    confidence/reason/summary)
# ---------------------------------------------------------------------------


def test_reason_maps_a_wire_send_email_suggestion_to_action_suggestion() -> None:
    wire = _WireReasoningResponse(decision=_wire_send_email())
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.action_type == ActionType.SEND_EMAIL


def test_the_mapped_payload_carries_subject_and_body_only() -> None:
    """M7.1l.1: the model drafts subject/body only — there is no
    `recipients` key anywhere in the mapped payload, since there is none in
    the wire schema to begin with."""
    wire = _WireReasoningResponse(
        decision=_wire_send_email(
            payload={
                "subject": "Pricing for 3 seats",
                "body": "Hi, here is the pricing you asked about.",
            }
        )
    )
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.payload == {
        "subject": "Pricing for 3 seats",
        "body": "Hi, here is the pricing you asked about.",
    }
    assert "recipients" not in outcome.decision.payload


def test_the_mapped_suggestion_carries_evidence() -> None:
    wire = _WireReasoningResponse(
        decision=_wire_send_email(evidence=["message_1", "message_2", "analysis"])
    )
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.evidence == ["message_1", "message_2", "analysis"]


def test_the_mapped_suggestion_carries_confidence_reason_and_summary() -> None:
    wire = _WireReasoningResponse(
        decision=_wire_send_email(
            summary="Reply with the requested pricing.",
            confidence=0.65,
            reason="The prospect asked outright and has not yet received an answer.",
        )
    )
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.summary == "Reply with the requested pricing."
    assert outcome.decision.confidence == 0.65
    assert (
        outcome.decision.reason == "The prospect asked outright and has not yet received an answer."
    )


def test_reason_never_adds_data_the_model_did_not_return() -> None:
    """The mapping is a 1:1 field copy, never an enrichment — this would
    catch, e.g., a fabricated recipient or evidence label sneaking into the
    mapping function. There is no `recipients` key for the mapper to add:
    the trusted recipient is assembled elsewhere, after this call returns
    (see `_send_email_wire._WireSendEmailPayload`'s own docstring)."""
    wire = _WireReasoningResponse(
        decision=_wire_send_email(
            payload={"subject": "S", "body": "B"},
            evidence=["message_1"],
        )
    )
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.payload == {"subject": "S", "body": "B"}
    assert outcome.decision.evidence == ["message_1"]


# ---------------------------------------------------------------------------
# D. provider metadata / model_id / usage / latency / prompt_version
# ---------------------------------------------------------------------------


def test_reason_reports_provider_metadata() -> None:
    wire = _WireReasoningResponse(decision=_wire_no_action())
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.schema_version == REASONING_SCHEMA_VERSION
    assert outcome.prompt_version == AGENT_REASONING_PROMPT_VERSION


def test_reason_reports_the_actual_model_id_the_response_carries() -> None:
    wire = _WireReasoningResponse(decision=_wire_no_action())
    stub = _StubCompletions(result=_completion(parsed=wire, model="gpt-4o-mini-2024-07-18"))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.model_id == "gpt-4o-mini-2024-07-18"


def test_reason_falls_back_to_the_configured_model_id_when_the_response_omits_it() -> None:
    wire = _WireReasoningResponse(decision=_wire_no_action())
    stub = _StubCompletions(result=_completion(parsed=wire, model=""))
    provider = OpenAIAgentReasoningProvider(
        _settings(nerva_agent_reasoning_model="gpt-4o-mini"), client=_StubClient(stub)
    )

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.model_id == "gpt-4o-mini"


def test_reason_maps_token_usage() -> None:
    wire = _WireReasoningResponse(decision=_wire_no_action())
    stub = _StubCompletions(
        result=_completion(
            parsed=wire, usage=SimpleNamespace(prompt_tokens=321, completion_tokens=88)
        )
    )
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.input_tokens == 321
    assert outcome.output_tokens == 88


def test_reason_maps_missing_usage_to_null_token_counts() -> None:
    wire = _WireReasoningResponse(decision=_wire_no_action())
    stub = _StubCompletions(result=_completion(parsed=wire, usage=None))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.input_tokens is None
    assert outcome.output_tokens is None


def test_reason_reports_a_non_negative_latency() -> None:
    wire = _WireReasoningResponse(decision=_wire_no_action())
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.latency_ms >= 0


def test_reason_sends_the_system_and_user_prompts_and_the_wire_response_format() -> None:
    wire = _WireReasoningResponse(decision=_wire_no_action())
    stub = _StubCompletions(result=_completion(parsed=wire))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    provider.reason(SOME_REQUEST)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["response_format"] is _WireReasoningResponse
    messages = call["messages"]
    assert messages[0] == {"role": "system", "content": AGENT_REASONING_SYSTEM_PROMPT}
    assert messages[1] == {
        "role": "user",
        "content": build_agent_reasoning_user_prompt(SOME_REQUEST),
    }


# ---------------------------------------------------------------------------
# E. refusal / no choices / unparsed content
# ---------------------------------------------------------------------------


def test_a_refusal_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(parsed=None, refusal="I can't help with that."))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_no_choices_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(parsed=None, choices=[]))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_unparsed_content_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(parsed=None))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


# ---------------------------------------------------------------------------
# F. error mapping — mirrors test_openai_provider.py's own coverage exactly
# ---------------------------------------------------------------------------


def test_authentication_error_maps_to_ai_auth_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(AuthenticationError, 401, "Incorrect API key provided.")
    )
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIAuthError):
        provider.reason(SOME_REQUEST)


def test_rate_limit_error_maps_to_ai_rate_limit_error_with_retry_after() -> None:
    stub = _StubCompletions(
        raises=_status_error(
            RateLimitError, 429, "Rate limit reached.", headers={"retry-after": "30"}
        )
    )
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIRateLimitError) as excinfo:
        provider.reason(SOME_REQUEST)
    assert excinfo.value.retry_after == 30.0


def test_rate_limit_error_without_a_retry_after_header_leaves_it_none() -> None:
    stub = _StubCompletions(raises=_status_error(RateLimitError, 429, "Rate limit reached."))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIRateLimitError) as excinfo:
        provider.reason(SOME_REQUEST)
    assert excinfo.value.retry_after is None


def test_connection_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=APIConnectionError(request=_request()))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_timeout_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=APITimeoutError(request=_request()))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_a_5xx_status_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(InternalServerError, 500, "Internal server error.")
    )
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_length_finish_reason_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(raises=LengthFinishReasonError(completion=SimpleNamespace(usage=None)))
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_content_filter_finish_reason_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(raises=ContentFilterFinishReasonError())
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_a_pydantic_validation_error_maps_to_ai_output_validation_error() -> None:
    try:
        _WireReasoningResponse.model_validate({"decision": {"decision": "no_action"}})
    except pydantic.ValidationError as exc:
        validation_error = exc
    stub = _StubCompletions(raises=validation_error)
    provider = OpenAIAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


# ---------------------------------------------------------------------------
# G. malformed/forbidden structured output — the wire schema's own strictness
# ---------------------------------------------------------------------------


def test_the_wire_schema_rejects_an_unknown_decision_discriminator() -> None:
    """There is no `"suggest_create_task"`/`"suggest_update_opportunity_stage"`
    variant anywhere in this schema — not disallowed by instruction, simply
    absent. An unrecognized discriminator value fails Pydantic validation
    outright, the same as any other malformed structured output."""
    with pytest.raises(pydantic.ValidationError):
        _WireReasoningResponse.model_validate(
            {"decision": {"decision": "suggest_create_task", "title": "Follow up"}}
        )


def test_create_task_and_update_opportunity_stage_cannot_be_expressed_at_all() -> None:
    """Structural proof, not merely a runtime rejection: neither forbidden
    type name appears as a selectable value anywhere in the wire schema's
    own JSON Schema — only as prose in a docstring-derived `description`,
    which is not something a structured-output response can ever select."""
    schema = _WireReasoningResponse.model_json_schema()
    selectable_values = _collect_schema_values(schema, keys=("const", "enum", "default"))

    assert "create_task" not in selectable_values
    assert "update_opportunity_stage" not in selectable_values
    assert selectable_values & {"no_action", "suggest_send_email"} == {
        "no_action",
        "suggest_send_email",
    }


def _collect_schema_values(node: object, *, keys: tuple[str, ...]) -> set[str]:
    """Walks a JSON Schema dict/list structure, collecting every string value
    under one of `keys` (`const`/`enum`/`default` — where a discriminator or
    closed set of selectable values actually lives), ignoring free-text
    fields like `description`/`title` entirely."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in keys:
                if isinstance(value, str):
                    found.add(value)
                elif isinstance(value, list):
                    found.update(item for item in value if isinstance(item, str))
            else:
                found |= _collect_schema_values(value, keys=keys)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_schema_values(item, keys=keys)
    return found


def test_the_wire_send_email_suggestion_rejects_extra_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        _WireSendEmailSuggestion.model_validate(
            {
                "summary": "x",
                "payload": {"subject": "s", "body": "b"},
                "confidence": 0.5,
                "reason": "y",
                "evidence": ["message_1"],
                "action_type": "send_email",  # not a real field — must be rejected
            }
        )


def test_the_wire_no_action_rejects_extra_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        _WireNoAction.model_validate({"reason": "x", "confidence": 0.5, "extra": "nope"})


def test_the_wire_send_email_payload_rejects_extra_fields() -> None:
    with pytest.raises(pydantic.ValidationError):
        _WireSendEmailPayload.model_validate(
            {
                "subject": "s",
                "body": "b",
                "cc": ["sneaky@b.example"],
            }
        )


def test_the_wire_send_email_payload_rejects_a_recipients_field() -> None:
    """M7.1l.1: `recipients` is not merely unused — it does not exist in
    this schema at all, so any provider response attempting to supply one
    fails the same `extra="forbid"` check any other unknown field would."""
    with pytest.raises(pydantic.ValidationError):
        _WireSendEmailPayload.model_validate(
            {"recipients": ["a@b.example"], "subject": "s", "body": "b"}
        )


def test_the_wire_send_email_payload_has_only_subject_and_body() -> None:
    assert set(_WireSendEmailPayload.model_fields) == {"subject", "body"}


def test_the_wire_send_email_suggestion_rejects_an_invalid_evidence_label() -> None:
    """The wire schema's own regex catches an obviously malformed label
    before it ever reaches the mapping step — `ProposalPolicy`'s existence
    check is still unconditionally required regardless (see the module
    docstring)."""
    with pytest.raises(pydantic.ValidationError):
        _wire_send_email(evidence=["not_a_real_label"])


def test_the_wire_send_email_suggestion_requires_at_least_one_evidence_label() -> None:
    with pytest.raises(pydantic.ValidationError):
        _wire_send_email(evidence=[])
