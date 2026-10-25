"""The default `AIAnalysisClient`/`AgentReasoningClient` until a real provider
is configured.

Every call fails safely and explicitly rather than silently returning a fake
or best-guess result — the same discipline `GoogleOAuthClient.build_authorization_url`
already applies when Google OAuth is not configured. `provider`/`model_id` are
still meaningful ahead of any call: `nerva.services.analysis` reads them to
compute the idempotency fingerprint *before* deciding whether to call
`analyze` at all, so a cached analysis from a time when a real provider *was*
configured is still served without ever needing this client to succeed.
"""

from nerva.nerva_ai.agent_client import AgentReasoningInput, AgentReasoningOutcome
from nerva.nerva_ai.client import AnalysisInput, AnalysisOutcome
from nerva.nerva_ai.errors import AIAuthError


class UnconfiguredAIClient:
    """Structurally implements `AIAnalysisClient`. Every `analyze()` call
    raises `AIAuthError` — the same failure class a real provider rejecting a
    missing or invalid API key would raise — so the rest of the pipeline
    (safe error persistence, the API's 503 mapping) needs no special case for
    "not configured" versus "credentials rejected"."""

    provider = "none"
    model_id = "unconfigured"

    def analyze(self, request: AnalysisInput) -> AnalysisOutcome:
        raise AIAuthError("AI analysis is not configured on this server.")


class UnconfiguredAgentReasoningClient:
    """Structurally implements `AgentReasoningClient` (M7.1e). Mirrors
    `UnconfiguredAIClient` exactly: every `reason()` call raises `AIAuthError`
    rather than faking a decision — a workspace with no OpenAI credentials
    configured gets an honest, typed failure, never a silent `NoActionDecision`
    that could be mistaken for a real "nothing to do" read."""

    provider = "none"
    model_id = "unconfigured"

    def reason(self, request: AgentReasoningInput) -> AgentReasoningOutcome:
        raise AIAuthError("Agent reasoning is not configured on this server.")
