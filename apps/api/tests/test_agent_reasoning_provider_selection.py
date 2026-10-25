"""`nerva.api.deps.get_agent_reasoning_client` provider-selection matrix
(M7.1g) — explicit, fail-closed, never a fallback between providers.

Each test here constructs `Settings` directly (no app, no DB, no network)
and asserts which concrete client `get_agent_reasoning_client` returns. See
`nerva.core.config.Settings.nerva_agent_reasoning_provider`/
`groq_provider_configured` and `nerva.api.deps.get_agent_reasoning_client`'s
own docstrings for the rules this matrix exercises."""

import pydantic
import pytest

from nerva.api.deps import get_agent_reasoning_client
from nerva.core.config import Settings
from nerva.nerva_ai.providers.groq_agent_reasoning import GroqAgentReasoningProvider
from nerva.nerva_ai.providers.openai_agent_reasoning import OpenAIAgentReasoningProvider
from nerva.nerva_ai.unconfigured import UnconfiguredAgentReasoningClient


def test_provider_openai_with_key_returns_the_openai_provider() -> None:
    client = get_agent_reasoning_client(
        Settings(nerva_agent_reasoning_provider="openai", openai_api_key="sk-test")
    )

    assert isinstance(client, OpenAIAgentReasoningProvider)


def test_provider_openai_without_a_key_returns_unconfigured() -> None:
    client = get_agent_reasoning_client(
        Settings(nerva_agent_reasoning_provider="openai", openai_api_key="")
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_provider_defaults_to_openai_when_unset() -> None:
    """An environment with no `NERVA_AGENT_REASONING_PROVIDER` set at all
    must behave exactly as it did before M7.1g — adding Groq support must
    never silently change existing OpenAI-only behavior."""
    client = get_agent_reasoning_client(Settings(openai_api_key="sk-test"))

    assert isinstance(client, OpenAIAgentReasoningProvider)


def test_provider_groq_with_key_and_model_returns_the_groq_provider() -> None:
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="groq",
            groq_api_key="gsk-test",
            nerva_groq_agent_reasoning_model="openai/gpt-oss-120b",
        )
    )

    assert isinstance(client, GroqAgentReasoningProvider)
    assert client.model_id == "openai/gpt-oss-120b"


def test_provider_groq_with_key_but_no_model_returns_unconfigured() -> None:
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="groq",
            groq_api_key="gsk-test",
            nerva_groq_agent_reasoning_model="",
        )
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_provider_groq_with_model_but_no_key_returns_unconfigured() -> None:
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="groq",
            groq_api_key="",
            nerva_groq_agent_reasoning_model="openai/gpt-oss-120b",
        )
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_provider_groq_with_neither_key_nor_model_returns_unconfigured() -> None:
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="groq",
            groq_api_key="",
            nerva_groq_agent_reasoning_model="",
        )
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_provider_groq_never_falls_back_to_openai_when_unconfigured() -> None:
    """Even with a perfectly valid OpenAI key present, an unconfigured Groq
    selection must stay `Unconfigured` — never silently try OpenAI."""
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="groq",
            groq_api_key="",
            nerva_groq_agent_reasoning_model="",
            openai_api_key="sk-test",
        )
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_provider_openai_never_falls_back_to_groq_when_unconfigured() -> None:
    """Symmetric check: an unconfigured OpenAI selection must stay
    `Unconfigured` even with a perfectly valid Groq key+model present."""
    client = get_agent_reasoning_client(
        Settings(
            nerva_agent_reasoning_provider="openai",
            openai_api_key="",
            groq_api_key="gsk-test",
            nerva_groq_agent_reasoning_model="openai/gpt-oss-120b",
        )
    )

    assert isinstance(client, UnconfiguredAgentReasoningClient)


def test_unknown_provider_value_is_rejected_at_settings_construction() -> None:
    """A typo'd or unknown provider name never reaches `get_agent_reasoning_
    client` at all — `Settings`' own `Literal` field fails to construct,
    which is this project's fail-closed-at-startup convention (see
    `tests/test_config.py`'s own `test_production_starts_now_that_real_
    authentication_exists` neighbor for the same discipline applied to a
    different setting)."""
    with pytest.raises(pydantic.ValidationError):
        Settings(nerva_agent_reasoning_provider="anthropic")  # type: ignore[arg-type]
