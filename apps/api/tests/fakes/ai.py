"""A fake for the AI analysis boundary (`nerva.nerva_ai`).

Tests never make a real call to any LLM provider: the FastAPI app under test has
`get_ai_client` (added when the API layer is wired, in a later block) overridden
to return this, the same way Google OAuth/Gmail are faked (see
`tests/fakes/google.py`).
"""

from nerva.core.enums import ConversationCategory, ConversationPriority
from nerva.nerva_ai.client import AnalysisInput, AnalysisOutcome
from nerva.nerva_ai.errors import AIError, AIProviderError
from nerva.nerva_ai.prompts import PROMPT_VERSION
from nerva.nerva_ai.schemas import SCHEMA_VERSION, ConversationAnalysisResult

DEFAULT_PROVIDER = "fake"
DEFAULT_MODEL_ID = "fake-model-1"


def make_analysis_result(**overrides: object) -> ConversationAnalysisResult:
    """A minimal, always-valid result. Override any field for a specific scenario,
    e.g. `make_analysis_result(commercial_signal=True, confidence=0.9)`."""
    defaults: dict[str, object] = {
        "summary": "A short summary of the conversation.",
        "category": ConversationCategory.OTHER,
        "priority": ConversationPriority.LOW,
        "requires_response": False,
        "commercial_signal": False,
        "confidence": 0.5,
    }
    defaults.update(overrides)
    return ConversationAnalysisResult.model_validate(defaults)


class FakeAIClient:
    """Scripted responses standing in for a real provider. Set `next_result` to
    control what `analyze()` returns, or `fail_with` to simulate a failure. Every
    call is recorded in `calls`, so a test can assert on how many times (or with
    what content) the "provider" was actually invoked — the basis of the
    idempotency tests in later blocks."""

    def __init__(self, *, result: ConversationAnalysisResult | None = None) -> None:
        self.next_result = result or make_analysis_result()
        self.provider = DEFAULT_PROVIDER
        self.model_id = DEFAULT_MODEL_ID
        self.fail_with: AIError | None = None
        # Subjects that should fail even while `fail_with` is unset elsewhere —
        # lets a test script "this one conversation fails, the rest succeed"
        # (e.g. for a backfill batch), without a global on/off failure switch.
        self.fail_for_subjects: set[str] = set()
        self.calls: list[AnalysisInput] = []

    def analyze(self, request: AnalysisInput) -> AnalysisOutcome:
        self.calls.append(request)
        if request.subject in self.fail_for_subjects:
            raise self.fail_with or AIProviderError("fake: scripted per-conversation failure")
        if self.fail_with is not None:
            raise self.fail_with
        return AnalysisOutcome(
            result=self.next_result,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model_id=self.model_id,
            input_tokens=42,
            output_tokens=17,
            latency_ms=5,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)
