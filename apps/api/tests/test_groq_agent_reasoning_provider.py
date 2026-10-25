"""`nerva_ai.providers.groq_agent_reasoning.GroqAgentReasoningProvider`: the
second real `AgentReasoningClient` (M7.1g).

Every test injects a stand-in for the underlying `groq.Groq` client's
`chat.completions.create` method — never a real network call. Failure tests
raise the SDK's actual exception classes (constructed directly, with no HTTP
call made) so the mapping in `GroqAgentReasoningProvider.reason` is verified
against the real type hierarchy, not a hand-rolled substitute — mirrors
`test_openai_agent_reasoning_provider.py`'s own discipline exactly, including
its `_StubCompletions`/`_StubClient`/`_status_error` helpers (duplicated here
rather than imported, the same "each test module keeps its own small
builders" precedent `test_proposal_fingerprint.py`/`test_proposal_
materialization.py` already set).

Unlike the OpenAI provider, there is no `.parse()` here: Groq's `create()`
returns a plain `message.content` string, which this provider validates
manually against the Groq-specific flat wire contract (`nerva_ai.providers.
_groq_send_email_wire`, M7.1i.1 — replacing the shared, nested contract this
module used through M7.1g/M7.1h after a real M7.1h smoke test showed Groq's
`openai/gpt-oss-20b` did not reliably produce that nested shape) — see
`groq_agent_reasoning`'s own module docstring for why. Tests build that
content as a flat JSON object directly: `{"decision": "no_action", ...}`,
never `{"decision": {...}}`.

After every successful `.reason()` call in this file:

    Groq output -> AgentReasoningDecision

and nothing further. `ProposalPolicy` is never invoked here, and no `Action`
is ever created — this module only proves the provider boundary maps
correctly. No test in this file makes a network call to Groq or to OpenAI,
and no test falls back from one provider to the other on failure — there is
no code path here that could."""

import json
from types import SimpleNamespace

import httpx
import pydantic
import pytest
from groq import (
    APIConnectionError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    InternalServerError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    UnprocessableEntityError,
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
from nerva.nerva_ai.providers.groq_agent_reasoning import GroqAgentReasoningProvider
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
        "groq_api_key": "gsk-test",
        "nerva_agent_reasoning_provider": "groq",
        "nerva_groq_agent_reasoning_model": "openai/gpt-oss-120b",
    }
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class _StubCompletions:
    """Stands in for `client.chat.completions`. Scripted via `result`
    (returned from `.create()`) or `raises` (raised instead) — every call is
    recorded so a test can assert on exactly what was sent."""

    def __init__(self, *, result: object = None, raises: Exception | None = None) -> None:
        self.result = result
        self.raises = raises
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return self.result


class _StubClient:
    def __init__(self, completions: _StubCompletions) -> None:
        self.chat = SimpleNamespace(completions=completions)


def _no_action_decision(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "decision": "no_action",
        "reason": "Nothing warrants a reply yet.",
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return defaults


def _send_email_decision(**overrides: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "decision": "suggest_send_email",
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
    return defaults


def _content(decision: dict[str, object]) -> str:
    """The flat wire contract is the decision dict itself, serialized
    directly — never wrapped in an outer `{"decision": {...}}` envelope."""
    return json.dumps(decision)


def _completion(
    *,
    content: str | None,
    model: str = "openai/gpt-oss-120b",
    usage: SimpleNamespace | None = SimpleNamespace(prompt_tokens=200, completion_tokens=60),
    choices: list[object] | None = None,
) -> SimpleNamespace:
    if choices is None:
        message = SimpleNamespace(content=content)
        choices = [SimpleNamespace(message=message)]
    return SimpleNamespace(choices=choices, usage=usage, model=model)


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def _status_error(
    cls: type[Exception], status_code: int, message: str, *, headers: dict[str, str] | None = None
) -> Exception:
    response = httpx.Response(
        status_code, request=_request(), headers=headers, json={"error": {"message": message}}
    )
    return cls(message, response=response, body={"error": {"message": message}})


# ---------------------------------------------------------------------------
# A. provider / model_id / DI
# ---------------------------------------------------------------------------


def test_provider_and_model_id_are_known_before_any_call() -> None:
    provider = GroqAgentReasoningProvider(
        _settings(nerva_groq_agent_reasoning_model="openai/gpt-oss-120b"),
        client=_StubClient(_StubCompletions()),
    )

    assert provider.provider == "groq"
    assert provider.model_id == "openai/gpt-oss-120b"


def test_get_agent_reasoning_client_returns_unconfigured_without_a_groq_key() -> None:
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="groq",
            groq_api_key="",
            nerva_groq_agent_reasoning_model="openai/gpt-oss-120b",
        )
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_get_agent_reasoning_client_returns_unconfigured_without_a_groq_model() -> None:
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="groq",
            groq_api_key="gsk-test",
            nerva_groq_agent_reasoning_model="",
        )
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_get_agent_reasoning_client_returns_the_real_groq_provider_once_configured() -> None:
    client = get_agent_reasoning_client(_settings(nerva_groq_agent_reasoning_model="llama-x"))

    assert isinstance(client, GroqAgentReasoningProvider)
    assert client.model_id == "llama-x"


def test_unconfigured_agent_reasoning_client_raises_ai_auth_error() -> None:
    with pytest.raises(AIAuthError):
        UnconfiguredAgentReasoningClient().reason(SOME_REQUEST)


# ---------------------------------------------------------------------------
# B. NO_ACTION parse/mapping
# ---------------------------------------------------------------------------


def test_reason_maps_a_no_action_content_to_no_action_decision() -> None:
    content = _content(_no_action_decision(reason="Already answered.", confidence=0.7))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, NoActionDecision)
    assert outcome.decision.reason == "Already answered."
    assert outcome.decision.confidence == 0.7


# ---------------------------------------------------------------------------
# C. SEND_EMAIL parse/mapping
# ---------------------------------------------------------------------------


def test_reason_maps_a_send_email_content_to_action_suggestion() -> None:
    content = _content(_send_email_decision())
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.action_type == ActionType.SEND_EMAIL


def test_the_mapped_payload_carries_subject_and_body_only() -> None:
    """M7.1l.1: the model drafts subject/body only — there is no
    `recipients` key anywhere in the mapped payload."""
    content = _content(
        _send_email_decision(
            payload={
                "subject": "Pricing for 3 seats",
                "body": "Hi, here is the pricing you asked about.",
            }
        )
    )
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.payload == {
        "subject": "Pricing for 3 seats",
        "body": "Hi, here is the pricing you asked about.",
    }
    assert "recipients" not in outcome.decision.payload


def test_the_mapped_suggestion_carries_evidence() -> None:
    content = _content(_send_email_decision(evidence=["message_1", "message_2", "analysis"]))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.evidence == ["message_1", "message_2", "analysis"]


def test_the_mapped_suggestion_carries_confidence_reason_and_summary() -> None:
    content = _content(
        _send_email_decision(
            summary="Reply with the requested pricing.",
            confidence=0.65,
            reason="The prospect asked outright and has not yet received an answer.",
        )
    )
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.summary == "Reply with the requested pricing."
    assert outcome.decision.confidence == 0.65
    assert (
        outcome.decision.reason == "The prospect asked outright and has not yet received an answer."
    )


def test_reason_never_adds_data_the_model_did_not_return() -> None:
    content = _content(
        _send_email_decision(
            payload={"subject": "S", "body": "B"},
            evidence=["message_1"],
        )
    )
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert isinstance(outcome.decision, ActionSuggestion)
    assert outcome.decision.payload == {"subject": "S", "body": "B"}
    assert outcome.decision.evidence == ["message_1"]


# ---------------------------------------------------------------------------
# D. provider metadata / model_id / usage / latency / prompt_version
# ---------------------------------------------------------------------------


def test_reason_reports_provider_metadata() -> None:
    content = _content(_no_action_decision())
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert provider.provider == "groq"
    assert outcome.schema_version == REASONING_SCHEMA_VERSION
    assert outcome.prompt_version == AGENT_REASONING_PROMPT_VERSION


def test_reason_reports_the_actual_model_id_the_response_carries() -> None:
    content = _content(_no_action_decision())
    stub = _StubCompletions(result=_completion(content=content, model="openai/gpt-oss-20b"))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.model_id == "openai/gpt-oss-20b"


def test_reason_falls_back_to_the_configured_model_id_when_the_response_omits_it() -> None:
    content = _content(_no_action_decision())
    stub = _StubCompletions(result=_completion(content=content, model=""))
    provider = GroqAgentReasoningProvider(
        _settings(nerva_groq_agent_reasoning_model="openai/gpt-oss-120b"),
        client=_StubClient(stub),
    )

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.model_id == "openai/gpt-oss-120b"


def test_reason_maps_token_usage() -> None:
    content = _content(_no_action_decision())
    stub = _StubCompletions(
        result=_completion(
            content=content, usage=SimpleNamespace(prompt_tokens=321, completion_tokens=88)
        )
    )
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.input_tokens == 321
    assert outcome.output_tokens == 88


def test_reason_maps_missing_usage_to_null_token_counts() -> None:
    content = _content(_no_action_decision())
    stub = _StubCompletions(result=_completion(content=content, usage=None))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.input_tokens is None
    assert outcome.output_tokens is None


def test_reason_reports_a_non_negative_latency() -> None:
    content = _content(_no_action_decision())
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    outcome = provider.reason(SOME_REQUEST)

    assert outcome.latency_ms >= 0


def test_reason_sends_the_exact_same_prompt_text_as_the_openai_provider() -> None:
    """Both providers must render the exact same system/user prompt for the
    exact same request — see the module docstring's "PROMPTS" discipline:
    `AGENT_REASONING_SYSTEM_PROMPT`/`build_agent_reasoning_user_prompt` are
    reused verbatim, never a Groq-specific wording."""
    content = _content(_no_action_decision())
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    provider.reason(SOME_REQUEST)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    messages = call["messages"]
    assert messages[0] == {"role": "system", "content": AGENT_REASONING_SYSTEM_PROMPT}
    assert messages[1] == {
        "role": "user",
        "content": build_agent_reasoning_user_prompt(SOME_REQUEST),
    }


def test_reason_makes_exactly_one_provider_call() -> None:
    content = _content(_no_action_decision())
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    provider.reason(SOME_REQUEST)

    assert len(stub.calls) == 1


def test_a_malformed_response_makes_no_second_attempt() -> None:
    """A schema-invalid response fails closed on the first and only call —
    no "ask the model again", no repair, no second request of any kind."""
    stub = _StubCompletions(result=_completion(content="not json at all"))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)

    assert len(stub.calls) == 1


def test_groq_agent_reasoning_module_never_imports_the_openai_sdk() -> None:
    """Structural proof of "no fallback": this module cannot call OpenAI
    even if it wanted to — `openai` is simply not among its imports."""
    import nerva.nerva_ai.providers.groq_agent_reasoning as module

    assert "openai" not in vars(module)
    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        text = handle.read()
    assert "import openai" not in text
    assert "from openai" not in text


def test_groq_agent_reasoning_provider_has_no_database_or_session_dependency() -> None:
    """Structural proof of "no Action materialization": neither the
    constructor nor `reason()` accepts anything resembling a database
    session — this provider cannot create an `Action` even if it wanted to,
    `ProposalMaterializationService` is simply unreachable from here."""
    import inspect

    init_params = set(inspect.signature(GroqAgentReasoningProvider.__init__).parameters)
    reason_params = set(inspect.signature(GroqAgentReasoningProvider.reason).parameters)

    assert "session" not in init_params
    assert "session" not in reason_params
    assert "ctx" not in init_params
    assert "ctx" not in reason_params


# ---------------------------------------------------------------------------
# E. no choices / empty content
# ---------------------------------------------------------------------------


def test_no_choices_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(content=None, choices=[]))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_empty_content_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(content=""))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_none_content_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(content=None))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


# ---------------------------------------------------------------------------
# F. error mapping — mirrors test_openai_agent_reasoning_provider.py's own
#    coverage, extended for the extra status-code classes Groq's SDK exposes
# ---------------------------------------------------------------------------


def test_authentication_error_maps_to_ai_auth_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(AuthenticationError, 401, "Incorrect API key provided.")
    )
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIAuthError):
        provider.reason(SOME_REQUEST)


def test_permission_denied_error_maps_to_ai_auth_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(PermissionDeniedError, 403, "Not permitted for this key.")
    )
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIAuthError):
        provider.reason(SOME_REQUEST)


def test_rate_limit_error_maps_to_ai_rate_limit_error_with_retry_after() -> None:
    stub = _StubCompletions(
        raises=_status_error(
            RateLimitError, 429, "Rate limit reached.", headers={"retry-after": "30"}
        )
    )
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIRateLimitError) as excinfo:
        provider.reason(SOME_REQUEST)
    assert excinfo.value.retry_after == 30.0


def test_rate_limit_error_without_a_retry_after_header_leaves_it_none() -> None:
    stub = _StubCompletions(raises=_status_error(RateLimitError, 429, "Rate limit reached."))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIRateLimitError) as excinfo:
        provider.reason(SOME_REQUEST)
    assert excinfo.value.retry_after is None


def test_connection_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=APIConnectionError(request=_request()))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_timeout_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=APITimeoutError(request=_request()))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_bad_request_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=_status_error(BadRequestError, 400, "Unsupported parameter."))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_not_found_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=_status_error(NotFoundError, 404, "Unknown model."))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_unprocessable_entity_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(UnprocessableEntityError, 422, "Semantically invalid request.")
    )
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_a_5xx_status_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(InternalServerError, 500, "Internal server error.")
    )
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.reason(SOME_REQUEST)


def test_error_messages_never_leak_the_api_key() -> None:
    """The mapped error message must never contain the credential used to
    make the call — only the provider's own (already-public) error text."""
    stub = _StubCompletions(
        raises=_status_error(AuthenticationError, 401, "Incorrect API key provided.")
    )
    provider = GroqAgentReasoningProvider(
        _settings(groq_api_key="gsk-super-secret-value"), client=_StubClient(stub)
    )

    with pytest.raises(AIAuthError) as excinfo:
        provider.reason(SOME_REQUEST)
    assert "gsk-super-secret-value" not in str(excinfo.value)


# ---------------------------------------------------------------------------
# G. malformed/forbidden structured output — same wire schema the OpenAI
#    provider validates against, reached via manual `model_validate_json`
#    instead of `.parse()`
# ---------------------------------------------------------------------------


def test_malformed_json_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(content="{not valid json"))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_an_unknown_decision_discriminator_maps_to_ai_output_validation_error() -> None:
    content = json.dumps(
        {"decision": "suggest_create_task", "reason": "Follow up", "confidence": 0.5}
    )
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_extra_top_level_fields_map_to_ai_output_validation_error() -> None:
    content = json.dumps({**_no_action_decision(), "extra": "sneaky field"})
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_no_action_carrying_a_send_email_payload_maps_to_ai_output_validation_error() -> None:
    """The exact mixed-variant case a flat merged schema cannot rule out
    structurally — `_GroqWireReasoningResponse._check_variant_consistency`
    (`nerva_ai.providers._groq_send_email_wire`) is what rejects it."""
    content = json.dumps(
        {
            **_no_action_decision(),
            "payload": {
                "subject": "S",
                "body": "B",
            },
        }
    )
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_a_send_email_decision_missing_its_payload_maps_to_ai_output_validation_error() -> None:
    content = _content(_send_email_decision())
    data = json.loads(content)
    del data["payload"]
    stub = _StubCompletions(result=_completion(content=json.dumps(data)))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_extra_fields_inside_the_suggestion_map_to_ai_output_validation_error() -> None:
    content = _content(_send_email_decision(action_type="send_email"))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_an_invalid_evidence_label_maps_to_ai_output_validation_error() -> None:
    content = _content(_send_email_decision(evidence=["not_a_real_label"]))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_an_empty_evidence_list_maps_to_ai_output_validation_error() -> None:
    content = _content(_send_email_decision(evidence=[]))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_an_invalid_payload_maps_to_ai_output_validation_error() -> None:
    payload = {"subject": "", "body": "b"}
    content = _content(_send_email_decision(payload=payload))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_a_raw_response_containing_recipients_maps_to_ai_output_validation_error() -> None:
    """M7.1l.1: `recipients` is not merely unused — it is not a field of
    this payload at all, so a provider response attempting to supply one
    fails the same `extra="forbid"` check any other unknown field would."""
    payload = {
        "recipients": ["priya@halcyon.example"],
        "subject": "Pricing for 3 seats",
        "body": "Hi Priya, here is the pricing you asked about.",
    }
    content = _content(_send_email_decision(payload=payload))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.reason(SOME_REQUEST)


def test_a_pydantic_validation_error_is_never_re_raised_directly() -> None:
    """Confirms the mapping really happens: `provider.reason` never leaks
    the raw `pydantic.ValidationError` itself for malformed output."""
    content = _content(_send_email_decision(evidence=[]))
    stub = _StubCompletions(result=_completion(content=content))
    provider = GroqAgentReasoningProvider(_settings(), client=_StubClient(stub))

    try:
        provider.reason(SOME_REQUEST)
    except pydantic.ValidationError:
        pytest.fail("pydantic.ValidationError leaked past the provider boundary")
    except AIOutputValidationError:
        pass
