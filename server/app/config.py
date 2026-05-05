"""Application settings loaded from environment variables via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    redis_url: str
    symbol_mapping_path: str = "/workspaces/ftmo_exness_hedge_v3/symbol_mapping_ftmo_exness.json"
    cors_origins: list[str] = ["http://localhost:5173"]
    log_level: str = "INFO"

    jwt_secret: str
    jwt_expires_minutes: int = 60
    admin_username: str = "admin"
    admin_password_hash: str

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, v: object) -> object:
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("jwt_secret")
    @classmethod
    def _jwt_secret_strong(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @field_validator("admin_password_hash")
    @classmethod
    def _admin_hash_format(cls, v: str) -> str:
        # bcrypt hashes start with $2a$, $2b$, or $2y$ (variant).
        if not v.startswith(("$2a$", "$2b$", "$2y$")):
            raise ValueError("ADMIN_PASSWORD_HASH must be a bcrypt hash (starts with $2a/$2b/$2y$)")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance for use as a FastAPI dependency."""
    return Settings()  # type: ignore[call-arg]
