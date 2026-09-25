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
