"""Settings loaded from env / .env file at the package root.

Same pattern as ``server/app/config.py`` so a CTO familiar with the
server repo recognizes it instantly.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ``.env`` sits next to the package directory (apps/ftmo-client/.env).
# Resolving from ``__file__`` keeps it working regardless of cwd.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE_PATH = PACKAGE_ROOT / ".env"

# Same shape as RedisService.add_account validation (step 3.1) so the
# CLI-registered account_id format and the env-supplied one can't drift.
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_]{3,64}$")


class FtmoClientSettings(BaseSettings):
    """Per-process configuration. One process drives one FTMO account."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ftmo_account_id: str
    redis_url: str
    ctrader_client_id: str
    ctrader_client_secret: str
    ctrader_redirect_uri: str = "http://localhost:8765/callback"
    ctrader_host: str = "live.ctraderapi.com"
    ctrader_port: int = 5035
    log_level: str = "INFO"

    @field_validator("ftmo_account_id")
    @classmethod
    def _validate_account_id(cls, v: str) -> str:
        if not _ACCOUNT_ID_RE.match(v):
            raise ValueError(
                f"FTMO_ACCOUNT_ID {v!r} must match {_ACCOUNT_ID_RE.pattern} "
                "(lowercase alphanum + underscore, 3-64 chars)"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> FtmoClientSettings:
    """Cached construction so each module shares one parsed Settings."""
    return FtmoClientSettings()  # type: ignore[call-arg]
