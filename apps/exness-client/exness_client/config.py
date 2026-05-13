"""Settings loaded from env / .env file at the package root.

Mirrors ``apps/ftmo-client/ftmo_client/config.py`` so a CTO familiar
with the FTMO client recognizes the pattern immediately.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# ``.env`` sits next to the package directory (apps/exness-client/.env).
# Resolving from ``__file__`` keeps it working regardless of cwd.
PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ENV_FILE_PATH = PACKAGE_ROOT / ".env"

# Same shape as RedisService.add_account validation so the CLI-registered
# account_id format and the env-supplied one can't drift.
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_]{3,64}$")


class ExnessClientSettings(BaseSettings):
    """Per-process configuration. One process drives one Exness account."""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Identity
    account_id: str

    # Redis
    redis_url: str

    # MT5 broker credentials. SecretStr keeps the password out of any
    # accidental repr / log line.
    mt5_login: int
    mt5_password: SecretStr
    mt5_server: str
    mt5_path: str | None = None

    # Operational. Heartbeat 5s is half the FTMO 10s cadence — Exness'
    # synchronous lib needs faster pulse-checks to detect MT5 terminal
    # disconnects within an actionable window.
    heartbeat_interval_s: float = 5.0
    cmd_stream_block_ms: int = 1000

    log_level: str = "INFO"

    @field_validator("account_id")
    @classmethod
    def _validate_account_id(cls, v: str) -> str:
        if not _ACCOUNT_ID_RE.match(v):
            raise ValueError(
                f"ACCOUNT_ID {v!r} must match {_ACCOUNT_ID_RE.pattern} "
                "(lowercase alphanum + underscore, 3-64 chars)"
            )
        return v


@lru_cache(maxsize=1)
def get_settings() -> ExnessClientSettings:
    """Cached construction so each module shares one parsed Settings."""
    return ExnessClientSettings()  # type: ignore[call-arg]
