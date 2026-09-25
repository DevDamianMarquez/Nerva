"""Application settings, loaded from environment variables and the repository-root `.env`."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# apps/api/src/nerva/core/config.py -> repository root
REPO_ROOT = Path(__file__).resolve().parents[5]


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


@lru_cache
def get_settings() -> Settings:
    return Settings()
