"""Shared fixtures for exness-client tests.

Mirrors ftmo-client/tests/conftest.py: fresh fakeredis per test, no
network IO. Autouse fixture resets the ``mt5_stub`` module state so
test order is independent.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest
from pydantic import SecretStr

from exness_client import mt5_stub
from exness_client.config import ExnessClientSettings


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    """A fresh in-process fakeredis client per test, decoded as strings."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture(autouse=True)
def _reset_mt5_stub() -> None:
    """Reset stub state before each test to keep behavior independent."""
    mt5_stub.reset_state_for_tests()


@pytest.fixture
def settings() -> ExnessClientSettings:
    """A valid Settings instance with hard-coded test values.

    Bypasses ``.env`` discovery (the tests run in a directory whose
    ``.env`` may or may not exist; the values here are stable).
    """
    return ExnessClientSettings(
        account_id="exness_acc_001",
        redis_url="redis://localhost:6379/0",
        mt5_login=12345678,
        mt5_password=SecretStr("test-password"),
        mt5_server="Exness-Stub",
        mt5_path=None,
        heartbeat_interval_s=0.05,
        cmd_stream_block_ms=100,
        log_level="INFO",
    )
