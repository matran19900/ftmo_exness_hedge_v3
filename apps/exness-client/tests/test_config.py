"""ExnessClientSettings env loading + validation tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from exness_client.config import ExnessClientSettings


def test_settings_loads_from_explicit_values() -> None:
    """Explicit kwargs bypass the .env discovery (used by test fixtures)."""
    s = ExnessClientSettings(
        account_id="exness_acc_001",
        redis_url="redis://localhost:6379/0",
        mt5_login=12345678,
        mt5_password=SecretStr("p"),
        mt5_server="Exness-MT5Trial7",
    )
    assert s.account_id == "exness_acc_001"
    assert s.redis_url == "redis://localhost:6379/0"
    assert s.mt5_login == 12345678
    assert s.mt5_server == "Exness-MT5Trial7"
    assert s.mt5_path is None
    # Defaults
    assert s.heartbeat_interval_s == 5.0
    assert s.cmd_stream_block_ms == 1000
    assert s.log_level == "INFO"


def test_settings_password_is_secret() -> None:
    """SecretStr keeps the password out of repr/log lines."""
    s = ExnessClientSettings(
        account_id="exness_acc_001",
        redis_url="redis://localhost:6379/0",
        mt5_login=12345678,
        mt5_password=SecretStr("super-secret-pwd"),
        mt5_server="Exness-Stub",
    )
    assert "super-secret-pwd" not in repr(s)
    assert "super-secret-pwd" not in str(s)
    assert s.mt5_password.get_secret_value() == "super-secret-pwd"


def test_settings_missing_required_field_raises() -> None:
    """Omitting a required field surfaces a ValidationError."""
    with pytest.raises(ValidationError):
        ExnessClientSettings(  # type: ignore[call-arg]
            account_id="exness_acc_001",
            redis_url="redis://localhost:6379/0",
            # mt5_login intentionally missing
            mt5_password=SecretStr("p"),
            mt5_server="Exness-Stub",
        )


def test_settings_account_id_format_validated() -> None:
    """Account id must match ^[a-z0-9_]{3,64}$ — same rule as RedisService."""
    with pytest.raises(ValidationError):
        ExnessClientSettings(
            account_id="UPPERCASE",  # uppercase rejected
            redis_url="redis://localhost:6379/0",
            mt5_login=1,
            mt5_password=SecretStr("p"),
            mt5_server="s",
        )
    with pytest.raises(ValidationError):
        ExnessClientSettings(
            account_id="ab",  # too short (<3)
            redis_url="redis://localhost:6379/0",
            mt5_login=1,
            mt5_password=SecretStr("p"),
            mt5_server="s",
        )


def test_settings_optional_mt5_path_accepted() -> None:
    s = ExnessClientSettings(
        account_id="exness_acc_001",
        redis_url="redis://localhost:6379/0",
        mt5_login=1,
        mt5_password=SecretStr("p"),
        mt5_server="s",
        mt5_path=r"C:\Program Files\MetaTrader 5\terminal64.exe",
    )
    assert s.mt5_path == r"C:\Program Files\MetaTrader 5\terminal64.exe"
