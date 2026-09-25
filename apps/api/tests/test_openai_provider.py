"""`nerva_ai.providers.openai.OpenAIProvider`: the real `AIAnalysisClient`.

Every test injects a stand-in for the underlying `openai.OpenAI` client's
`chat.completions.parse` method — never a real network call. Failure tests raise
the SDK's actual exception classes (constructed directly, with no HTTP call made)
so the mapping in `OpenAIProvider.analyze` is verified against the real type
hierarchy, not a hand-rolled substitute.
"""

from datetime import UTC, datetime
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

from nerva.api.deps import get_ai_client
from nerva.core.config import Settings
from nerva.core.enums import ConversationCategory, ConversationPriority, MessageDirection
from nerva.nerva_ai.client import AnalysisInput, AnalysisMessage
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from nerva.nerva_ai.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from nerva.nerva_ai.providers.openai import OpenAIProvider
from nerva.nerva_ai.schemas import SCHEMA_VERSION, ConversationAnalysisResult
from nerva.nerva_ai.unconfigured import UnconfiguredAIClient

SOME_REQUEST = AnalysisInput(
    subject="Pricing for 3 seats",
    messages=[
        AnalysisMessage(
            direction=MessageDirection.INBOUND,
            sender_label="Priya Nair",
            sent_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
            body_text="Can you send pricing for 3 seats?",
        )
    ],
)


def _settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {"openai_api_key": "sk-test", "nerva_ai_model": "gpt-4o-mini"}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


class _StubCompletions:
    """Stands in for `client.chat.completions`. Scripted via `result` (returned
    from `.parse()`) or `raises` (raised instead) — every call is recorded so a
    test can assert on exactly what was sent to the "provider"."""

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


def _valid_result(**overrides: object) -> ConversationAnalysisResult:
    defaults: dict[str, object] = {
        "summary": "Asking for pricing on a 3-seat plan.",
        "category": ConversationCategory.SALES_LEAD,
        "priority": ConversationPriority.MEDIUM,
        "requires_response": True,
        "commercial_signal": True,
        "confidence": 0.8,
    }
    defaults.update(overrides)
    return ConversationAnalysisResult.model_validate(defaults)


def _completion(
    *,
    parsed: ConversationAnalysisResult | None,
    refusal: str | None = None,
    model: str = "gpt-4o-mini-2024-07-18",
    usage: SimpleNamespace | None = SimpleNamespace(prompt_tokens=120, completion_tokens=45),
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


# --- provider / model_id -------------------------------------------------------


def test_provider_and_model_id_are_known_before_any_call() -> None:
    provider = OpenAIProvider(
        _settings(nerva_ai_model="gpt-4o-mini"), client=_StubClient(_StubCompletions())
    )

    assert provider.provider == "openai"
    assert provider.model_id == "gpt-4o-mini"


# --- dependency wiring (nerva.api.deps.get_ai_client) ---------------------------


def test_get_ai_client_returns_unconfigured_client_without_an_api_key() -> None:
    client = get_ai_client(Settings(openai_api_key=""))

    assert isinstance(client, UnconfiguredAIClient)


def test_get_ai_client_returns_the_real_provider_once_an_api_key_is_set() -> None:
    client = get_ai_client(_settings(nerva_ai_model="gpt-4o-mini"))

    assert isinstance(client, OpenAIProvider)
    assert client.model_id == "gpt-4o-mini"


# --- valid result / usage mapping -----------------------------------------------


def test_analyze_maps_a_valid_result() -> None:
    scripted = _valid_result()
    stub = _StubCompletions(result=_completion(parsed=scripted))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    outcome = provider.analyze(SOME_REQUEST)

    assert outcome.result == scripted
    assert outcome.schema_version == SCHEMA_VERSION
    assert outcome.prompt_version == PROMPT_VERSION
    assert outcome.model_id == "gpt-4o-mini-2024-07-18"
    assert outcome.input_tokens == 120
    assert outcome.output_tokens == 45
    assert outcome.latency_ms >= 0


def test_analyze_sends_the_system_and_user_prompts() -> None:
    stub = _StubCompletions(result=_completion(parsed=_valid_result()))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    provider.analyze(SOME_REQUEST)

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["model"] == "gpt-4o-mini"
    assert call["response_format"] is ConversationAnalysisResult
    messages = call["messages"]
    assert messages[0] == {"role": "system", "content": SYSTEM_PROMPT}
    assert messages[1] == {"role": "user", "content": build_user_prompt(SOME_REQUEST)}


def test_analyze_survives_a_result_with_every_optional_field_null() -> None:
    scripted = _valid_result(
        intent=None, sentiment=None, estimated_value=None, deadline=None, suggested_next_step=None
    )
    stub = _StubCompletions(result=_completion(parsed=scripted))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    outcome = provider.analyze(SOME_REQUEST)

    assert outcome.result.intent is None
    assert outcome.result.sentiment is None
    assert outcome.result.estimated_value is None
    assert outcome.result.deadline is None
    assert outcome.result.suggested_next_step is None


def test_analyze_maps_missing_usage_to_null_token_counts() -> None:
    stub = _StubCompletions(result=_completion(parsed=_valid_result(), usage=None))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    outcome = provider.analyze(SOME_REQUEST)

    assert outcome.input_tokens is None
    assert outcome.output_tokens is None


def test_analyze_falls_back_to_the_configured_model_id_when_the_response_omits_it() -> None:
    stub = _StubCompletions(result=_completion(parsed=_valid_result(), model=""))
    provider = OpenAIProvider(_settings(nerva_ai_model="gpt-4o-mini"), client=_StubClient(stub))

    outcome = provider.analyze(SOME_REQUEST)

    assert outcome.model_id == "gpt-4o-mini"


# --- error mapping ---------------------------------------------------------------


def test_authentication_error_maps_to_ai_auth_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(AuthenticationError, 401, "Incorrect API key provided.")
    )
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIAuthError):
        provider.analyze(SOME_REQUEST)


def test_rate_limit_error_maps_to_ai_rate_limit_error_with_retry_after() -> None:
    stub = _StubCompletions(
        raises=_status_error(
            RateLimitError, 429, "Rate limit reached.", headers={"retry-after": "30"}
        )
    )
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIRateLimitError) as excinfo:
        provider.analyze(SOME_REQUEST)
    assert excinfo.value.retry_after == 30.0


def test_rate_limit_error_without_a_retry_after_header_leaves_it_none() -> None:
    stub = _StubCompletions(raises=_status_error(RateLimitError, 429, "Rate limit reached."))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIRateLimitError) as excinfo:
        provider.analyze(SOME_REQUEST)
    assert excinfo.value.retry_after is None


def test_connection_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=APIConnectionError(request=_request()))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.analyze(SOME_REQUEST)


def test_timeout_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(raises=APITimeoutError(request=_request()))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.analyze(SOME_REQUEST)


def test_a_5xx_status_error_maps_to_ai_provider_error() -> None:
    stub = _StubCompletions(
        raises=_status_error(InternalServerError, 500, "Internal server error.")
    )
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIProviderError):
        provider.analyze(SOME_REQUEST)


def test_length_finish_reason_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(raises=LengthFinishReasonError(completion=SimpleNamespace(usage=None)))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.analyze(SOME_REQUEST)


def test_content_filter_finish_reason_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(raises=ContentFilterFinishReasonError())
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.analyze(SOME_REQUEST)


def test_a_pydantic_validation_error_maps_to_ai_output_validation_error() -> None:
    try:
        ConversationAnalysisResult.model_validate({})
    except pydantic.ValidationError as exc:
        validation_error = exc
    stub = _StubCompletions(raises=validation_error)
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.analyze(SOME_REQUEST)


def test_no_choices_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(parsed=None, choices=[]))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.analyze(SOME_REQUEST)


def test_a_refusal_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(parsed=None, refusal="I can't help with that."))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.analyze(SOME_REQUEST)


def test_unparsed_content_maps_to_ai_output_validation_error() -> None:
    stub = _StubCompletions(result=_completion(parsed=None))
    provider = OpenAIProvider(_settings(), client=_StubClient(stub))

    with pytest.raises(AIOutputValidationError):
        provider.analyze(SOME_REQUEST)


# --- privacy: no conversation content ever leaks into an error message ---------


def test_no_error_message_contains_private_conversation_content() -> None:
    sensitive_marker = "ACME-CONFIDENTIAL-9f3e2b"
    request = AnalysisInput(
        subject="Contract renewal",
        messages=[
            AnalysisMessage(
                direction=MessageDirection.INBOUND,
                sender_label="Priya Nair",
                sent_at=datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
                body_text=f"Our internal deal code is {sensitive_marker}.",
            )
        ],
    )

    scenarios: list[Exception] = [
        _status_error(AuthenticationError, 401, "Incorrect API key provided."),
        _status_error(RateLimitError, 429, "Rate limit reached."),
        APIConnectionError(request=_request()),
        _status_error(InternalServerError, 500, "Internal server error."),
        LengthFinishReasonError(completion=SimpleNamespace(usage=None)),
        ContentFilterFinishReasonError(),
    ]
    for scripted_error in scenarios:
        stub = _StubCompletions(raises=scripted_error)
        provider = OpenAIProvider(_settings(), client=_StubClient(stub))
        with pytest.raises(Exception) as excinfo:  # noqa: PT011 (asserting on the message below)
            provider.analyze(request)
        assert sensitive_marker not in str(excinfo.value)
