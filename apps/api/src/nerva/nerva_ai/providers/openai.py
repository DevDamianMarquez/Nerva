"""The real `AIAnalysisClient`: OpenAI's Chat Completions API with Structured
Outputs (`response_format=<a Pydantic model>`), which parses the model's reply
directly into `ConversationAnalysisResult` — the same validated-shape guarantee
`FakeAIClient` gives tests, now backed by a real provider.

The only module in Nerva allowed to import the `openai` package: everything else
depends on `nerva_ai.client.AIAnalysisClient` (see `nerva.api.deps.get_ai_client`),
the same discipline `nerva_connect.google` applies to Google's SDKs (see
docs/ARCHITECTURE.md). Never imports or touches Gmail — this module only ever
receives an already-built `AnalysisInput`.
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
from nerva.nerva_ai.client import AnalysisInput, AnalysisOutcome
from nerva.nerva_ai.errors import (
    AIAuthError,
    AIOutputValidationError,
    AIProviderError,
    AIRateLimitError,
)
from nerva.nerva_ai.prompts import PROMPT_VERSION, SYSTEM_PROMPT, build_user_prompt
from nerva.nerva_ai.schemas import SCHEMA_VERSION, ConversationAnalysisResult

# Generous enough for a real completion (well beyond typical latency) without
# letting one call hang the request indefinitely; the SDK's own retry handles
# transient transport failures beneath this.
_TIMEOUT_SECONDS = 30.0
_MAX_RETRIES = 2


class OpenAIProvider:
    """The real implementation. Tests inject a stand-in for `client` exposing the
    same `chat.completions.parse` surface (see `tests/test_openai_provider.py`) —
    never a real network call."""

    provider = "openai"

    def __init__(self, settings: Settings, *, client: OpenAI | None = None) -> None:
        self.model_id = settings.nerva_ai_model
        self._client = client or OpenAI(
            api_key=settings.openai_api_key,
            timeout=_TIMEOUT_SECONDS,
            max_retries=_MAX_RETRIES,
        )

    def analyze(self, request: AnalysisInput) -> AnalysisOutcome:
        started = time.monotonic()
        try:
            completion = self._client.chat.completions.parse(
                model=self.model_id,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_user_prompt(request)},
                ],
                response_format=ConversationAnalysisResult,
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
            # already-valid: the same discipline as any other provider boundary.
            raise AIOutputValidationError(
                "OpenAI's response did not match the expected schema."
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        result = _extract_result(completion)
        usage = completion.usage
        return AnalysisOutcome(
            result=result,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model_id=completion.model or self.model_id,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            latency_ms=latency_ms,
        )


def _extract_result(
    completion: ParsedChatCompletion[ConversationAnalysisResult],
) -> ConversationAnalysisResult:
    if not completion.choices:
        raise AIOutputValidationError("OpenAI returned no choices.")
    message = completion.choices[0].message
    if message.refusal:
        raise AIOutputValidationError("OpenAI declined to analyze this conversation.")
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
