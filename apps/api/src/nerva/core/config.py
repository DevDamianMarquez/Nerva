"""Application settings, loaded from environment variables and the repository-root `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/src/nerva/core/config.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[5]

AgentReasoningProvider = Literal["openai", "groq"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Literal["local", "test", "production"] = "local"
    database_url: str = "postgresql+psycopg://nerva:nerva@127.0.0.1:5433/nerva"
    test_database_url: str = "postgresql+psycopg://nerva:nerva@127.0.0.1:5433/nerva_test"
    cors_origins: str = "http://127.0.0.1:3000"
    # Where the API redirects the browser after the Google OAuth callback finishes.
    frontend_base_url: str = "http://127.0.0.1:3000"

    # --- Nerva Connect: Google OAuth / Gmail --------------------------------------
    # Blank until configured; see README.md for how to create these in Google Cloud.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8000/v1/integrations/google/callback"
    # Fernet key (44-char urlsafe-base64) used to encrypt stored OAuth refresh tokens.
    # Generate one with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    integration_encryption_key: str = ""

    # --- Nerva AI: conversation analysis ------------------------------------------
    # Blank until configured; see README.md for how to obtain one. Leaving it blank
    # runs Nerva with AI analysis disabled (`UnconfiguredAIClient`; see
    # `nerva.api.deps.get_ai_client`) rather than with a fake or best-guess result.
    openai_api_key: str = ""
    nerva_ai_model: str = "gpt-4o-mini"
    # --- Nerva AI: agent reasoning (M7.1e) -----------------------------------------
    # Same OpenAI account as analysis (`openai_api_key`/`ai_provider_configured`
    # above) — reasoning does not need a second API key. The model itself is
    # intentionally a *separate* knob from `nerva_ai_model`: proposing an action is
    # a different, higher-stakes decision than classifying a conversation, so the
    # two pipelines should be free to use different models later without an
    # architecture change today. Left blank by default — see
    # `agent_reasoning_model` for the fallback this enables.
    nerva_agent_reasoning_model: str = ""
    # --- Nerva AI: agent reasoning provider selection (M7.1g) ----------------------
    # Explicit, fail-closed selection between the two real `AgentReasoningClient`
    # implementations. Defaults to "openai" so an environment with no new variables
    # set at all keeps the exact behavior M7.1e already shipped — adding Groq support
    # must never silently change what an already-deployed workspace does. An
    # unrecognized value (a typo) fails `Settings()` construction itself, at
    # application startup, via this `Literal` — never a runtime "unconfigured"
    # fallback for a misspelled provider name (see `nerva.api.deps.
    # get_agent_reasoning_client` for why no cross-provider fallback ever exists
    # either).
    nerva_agent_reasoning_provider: AgentReasoningProvider = "openai"
    # Blank until configured; a second, independent API key — Groq is a different
    # account/vendor from OpenAI, unlike `nerva_agent_reasoning_model`'s relationship
    # to `openai_api_key` above.
    groq_api_key: str = ""
    # Deliberately no fallback to `nerva_ai_model`/`agent_reasoning_model`: those name
    # OpenAI model ids, which are not valid Groq model ids. There is no safe default
    # to guess here — see `groq_agent_reasoning_model`'s own docstring for the
    # fail-closed consequence of leaving this blank.
    nerva_groq_agent_reasoning_model: str = ""

    @property
    def agent_reasoning_model(self) -> str:
        """The model `OpenAIAgentReasoningProvider` calls. Falls back to
        `nerva_ai_model` when `nerva_agent_reasoning_model` is unset, so a
        single configured model works for both pipelines until someone
        deliberately points reasoning at a different one — never a silent,
        separate default that could drift from what an operator thinks is
        configured."""
        return self.nerva_agent_reasoning_model or self.nerva_ai_model

    @property
    def groq_agent_reasoning_model(self) -> str:
        """The model `GroqAgentReasoningProvider` calls — `""` (falsy) if
        unset. Unlike `agent_reasoning_model`, there is no fallback: a Groq
        model id is never interchangeable with an OpenAI one, so guessing one
        would be inventing configuration, not defaulting it. `groq_
        provider_configured` is what actually gates whether Groq is usable;
        this property is read only after that gate already passed (see
        `nerva.api.deps.get_agent_reasoning_client`)."""
        return self.nerva_groq_agent_reasoning_model

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def cookie_secure(self) -> bool:
        """Whether the session cookie requires HTTPS.

        Local development has no TLS, so this is only on in production. See
        `nerva.core.cookies` for the full reasoning.
        """
        return self.environment == "production"

    @property
    def google_oauth_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def ai_provider_configured(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def groq_provider_configured(self) -> bool:
        """True only if *both* a Groq API key and an explicit Groq reasoning
        model are set. Unlike `ai_provider_configured` (a key alone, because
        `agent_reasoning_model` always has a usable fallback), Groq has no
        fallback model to guess — so "usable" requires both, not just the
        key, or `get_agent_reasoning_client` would otherwise build a
        provider pointed at an empty model id."""
        return bool(self.groq_api_key and self.nerva_groq_agent_reasoning_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
