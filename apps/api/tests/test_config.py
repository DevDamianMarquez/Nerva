from nerva.app import create_app
from nerva.core.config import Settings


def test_production_starts_now_that_real_authentication_exists() -> None:
    # v0.1 refused to start in production because the only authentication was the
    # spoofable X-User-Id header. Real sessions replaced it, so production is supported.
    create_app(Settings(environment="production"))


def test_cors_origins_are_parsed_from_a_comma_separated_list() -> None:
    settings = Settings(cors_origins="http://127.0.0.1:3000, https://app.nerva.test ,")

    assert settings.cors_origin_list == ["http://127.0.0.1:3000", "https://app.nerva.test"]


def test_the_session_cookie_requires_https_only_in_production() -> None:
    assert Settings(environment="local").cookie_secure is False
    assert Settings(environment="test").cookie_secure is False
    assert Settings(environment="production").cookie_secure is True


def test_ai_provider_is_not_configured_without_an_api_key() -> None:
    assert Settings(openai_api_key="").ai_provider_configured is False


def test_ai_provider_is_configured_once_an_api_key_is_set() -> None:
    assert Settings(openai_api_key="sk-test").ai_provider_configured is True


def test_groq_provider_is_not_configured_without_a_key_and_a_model() -> None:
    neither = Settings(groq_api_key="", nerva_groq_agent_reasoning_model="")
    key_only = Settings(groq_api_key="gsk-test", nerva_groq_agent_reasoning_model="")
    model_only = Settings(groq_api_key="", nerva_groq_agent_reasoning_model="openai/gpt-oss-120b")

    assert neither.groq_provider_configured is False
    assert key_only.groq_provider_configured is False
    assert model_only.groq_provider_configured is False


def test_groq_provider_is_configured_once_both_a_key_and_a_model_are_set() -> None:
    settings = Settings(
        groq_api_key="gsk-test", nerva_groq_agent_reasoning_model="openai/gpt-oss-120b"
    )

    assert settings.groq_provider_configured is True


def test_groq_agent_reasoning_model_has_no_fallback_to_the_openai_model() -> None:
    """Unlike `agent_reasoning_model`, a blank Groq model stays blank — there
    is no OpenAI model id this property could sensibly fall back to."""
    settings = Settings(nerva_groq_agent_reasoning_model="", nerva_ai_model="gpt-4o-mini")

    assert settings.groq_agent_reasoning_model == ""
