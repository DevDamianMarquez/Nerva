"""A fake for the agent-reasoning provider boundary (`nerva.nerva_ai.agent_client`).

Mirrors `tests.fakes.ai.FakeAIClient` exactly: no real provider exists yet
(M7.1a defines the contracts only), so this is the only implementation of
`AgentReasoningClient` — used to exercise the Protocol, and to stand in for
whatever real provider a later block adds.
"""

from nerva.nerva_ai.agent_client import (
    REASONING_SCHEMA_VERSION,
    AgentReasoningDecision,
    AgentReasoningInput,
    AgentReasoningOutcome,
    NoActionDecision,
)
from nerva.nerva_ai.errors import AIError

DEFAULT_PROVIDER = "fake"
DEFAULT_MODEL_ID = "fake-reasoning-model-1"
# A plain, stable sentinel — never a real prompt version (`"v1"`, …): this
# fake renders no prompt at all, so claiming one would be dishonest. See
# `AgentReasoningOutcome.prompt_version`'s own docstring.
DEFAULT_PROMPT_VERSION = "fake-no-prompt"


def make_no_action_decision(**overrides: object) -> NoActionDecision:
    """A minimal, always-valid `NoActionDecision`. Override any field for a
    specific scenario, e.g. `make_no_action_decision(confidence=0.4)`."""
    defaults: dict[str, object] = {
        "reason": "Nothing in this conversation warrants a proposed action.",
        "confidence": 0.9,
    }
    defaults.update(overrides)
    return NoActionDecision.model_validate(defaults)


class FakeAgentReasoningClient:
    """Scripted responses standing in for a real provider. Set `next_decision`
    to control what `reason()` returns, or `fail_with` to simulate a failure.
    `prompt_version` is settable the same way `provider`/`model_id` already
    are — e.g. to exercise two "different provider prompt wordings, same
    reasoning state" runs in a single test, without needing a second fake
    class. Every call is recorded in `calls`, the same basis `FakeAIClient.
    calls` already gives analysis tests."""

    def __init__(self, *, decision: AgentReasoningDecision | None = None) -> None:
        self.next_decision: AgentReasoningDecision = decision or make_no_action_decision()
        self.provider = DEFAULT_PROVIDER
        self.model_id = DEFAULT_MODEL_ID
        self.prompt_version = DEFAULT_PROMPT_VERSION
        self.fail_with: AIError | None = None
        self.calls: list[AgentReasoningInput] = []

    def reason(self, request: AgentReasoningInput) -> AgentReasoningOutcome:
        self.calls.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        return AgentReasoningOutcome(
            decision=self.next_decision,
            schema_version=REASONING_SCHEMA_VERSION,
            model_id=self.model_id,
            input_tokens=42,
            output_tokens=17,
            latency_ms=5,
            prompt_version=self.prompt_version,
        )

    @property
    def call_count(self) -> int:
        return len(self.calls)
