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


@lru_cache
def get_settings() -> Settings:
    return Settings()
