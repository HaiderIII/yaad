"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from typing import Literal

from pydantic import PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    app_env: Literal["development", "production", "test"] = "development"
    app_secret_key: str

    @field_validator("app_secret_key")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Ensure secret key is strong enough."""
        if len(v) < 32:
            raise ValueError("APP_SECRET_KEY must be at least 32 characters long")
        if v == "change-me-to-a-secure-random-string":
            raise ValueError("APP_SECRET_KEY must be changed from the default value")
        return v

    app_url: str = "http://localhost:8080"
    app_name: str = "Yaad"

    # Database
    database_url: PostgresDsn

    # Redis
    redis_url: RedisDsn

    # GitHub OAuth
    github_client_id: str
    github_client_secret: str

    # External APIs
    tmdb_api_key: str = ""

    # Google OAuth (Phase 3 - YouTube)
    google_client_id: str = ""
    google_client_secret: str = ""

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.app_env == "production"

    @property
    def database_url_async(self) -> str:
        """Get async database URL (postgresql+asyncpg)."""
        url = str(self.database_url)
        return url.replace("postgresql://", "postgresql+asyncpg://")


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()  # type: ignore[call-arg]
