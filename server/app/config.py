"""Application settings loaded from environment variables via pydantic-settings."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# This file lives at <repo>/server/app/config.py; the .env file lives at <repo>/.env.
# Resolving via __file__ keeps Settings working regardless of which directory
# uvicorn / pytest / ad-hoc python is launched from.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE_PATH = PROJECT_ROOT / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    redis_url: str
    symbol_mapping_path: str = "/workspaces/ftmo_exness_hedge_v3/server/data/ftmo_whitelist.json"
    # Phase 4.A.2: per-Exness-account mapping cache files live under this
    # directory. One file per signature; see docs/phase-4-symbol-mapping-design.md
    # §2.2 for layout and §8 for the atomic write contract.
    symbol_mapping_cache_dir: str = (
        "/workspaces/ftmo_exness_hedge_v3/server/data/symbol_mapping_cache"
    )
    # Phase 4.A.3: AutoMatchEngine tier-3 manual hints config (D-SM-12).
    # Bootstrapped from the 14 archived manual entries; CEO can hand-edit
    # afterwards. See docs/phase-4-symbol-mapping-design.md §2.3 + §6.
    symbol_match_hints_path: str = (
        "/workspaces/ftmo_exness_hedge_v3/server/config/symbol_match_hints.json"
    )
    # NoDecode disables pydantic-settings' eager JSON parse for this list field
    # so the validator below can accept either CSV or JSON-list strings from env.
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]
    log_level: str = "INFO"

    jwt_secret: str
    jwt_expires_minutes: int = 60
    admin_username: str = "admin"
    admin_password_hash: str

    # cTrader Open API (Phase 2+ market-data feed)
    ctrader_client_id: str = ""
    ctrader_client_secret: str = ""
    ctrader_host: str = "live.ctraderapi.com"
    ctrader_port: int = 5035
    ctrader_redirect_uri: str = "http://localhost:8000/api/auth/ctrader/callback"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors(cls, v: object) -> object:
        if isinstance(v, str):
            stripped = v.strip()
            if stripped.startswith("["):
                # JSON-list form, e.g. ["http://a","http://b"].
                return json.loads(stripped)
            # Comma-separated form, e.g. http://a,http://b.
            return [item.strip() for item in stripped.split(",") if item.strip()]
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
        if not v.startswith(("$2a$", "$2b$", "$2y$")):
            raise ValueError("ADMIN_PASSWORD_HASH must be a bcrypt hash (starts with $2a/$2b/$2y$)")
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance for use as a FastAPI dependency."""
    return Settings()  # type: ignore[call-arg]
