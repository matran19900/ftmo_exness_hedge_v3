"""main.amain lifecycle tests using the stub mt5 module + fakeredis."""

from __future__ import annotations

import asyncio
import os
import signal as _signal
import sys
from typing import Any

import fakeredis.aioredis
import pytest
from pydantic import SecretStr

from exness_client import main as main_module
from exness_client import mt5_stub
from exness_client.config import ExnessClientSettings


def _settings(redis_url: str) -> ExnessClientSettings:
    return ExnessClientSettings(
        account_id="exness_acc_001",
        redis_url=redis_url,
        mt5_login=12345678,
        mt5_password=SecretStr("test-password"),
        mt5_server="Exness-Stub",
        heartbeat_interval_s=0.05,
        cmd_stream_block_ms=50,
        log_level="INFO",
    )


@pytest.mark.asyncio
async def test_amain_starts_tasks_and_exits_on_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path: amain spins up cmd_proc + heartbeat, then we trigger
    shutdown and assert it exits 0."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_connect_redis(_url: str) -> Any:
        return fake

    monkeypatch.setattr(main_module, "_connect_redis", fake_connect_redis)

    settings = _settings("redis://stub/0")

    async def _kicker() -> None:
        # Give amain time to install signal handlers + start tasks.
        for _ in range(40):
            if await fake.exists("client:exness:exness_acc_001"):
                break
            await asyncio.sleep(0.02)
        # Raise SIGTERM via the loop's signal handler.
        os.kill(os.getpid(), _signal.SIGTERM)

    kicker = asyncio.create_task(_kicker())
    exit_code = await main_module.amain(settings=settings, mt5_module=mt5_stub)
    await kicker
    assert exit_code == main_module.EXIT_OK


@pytest.mark.asyncio
async def test_amain_netting_mode_returns_exit_code_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MT5 account in netting mode → amain returns EXIT_NETTING_MODE (2)."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_connect_redis(_url: str) -> Any:
        return fake

    monkeypatch.setattr(main_module, "_connect_redis", fake_connect_redis)

    mt5_stub.set_state_for_tests(
        account_info=mt5_stub.AccountInfo(
            login=1, balance=0.0, currency="USD",
            margin_mode=mt5_stub.ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
            leverage=100, server="s",
        ),
    )

    settings = _settings("redis://stub/0")
    exit_code = await main_module.amain(settings=settings, mt5_module=mt5_stub)
    assert exit_code == main_module.EXIT_NETTING_MODE


@pytest.mark.asyncio
async def test_amain_init_failure_returns_exit_code_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mt5.initialize returning False → MT5ConnectError → exit 1."""
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def fake_connect_redis(_url: str) -> Any:
        return fake

    monkeypatch.setattr(main_module, "_connect_redis", fake_connect_redis)
    mt5_stub.set_state_for_tests(init_should_fail=True)

    settings = _settings("redis://stub/0")
    exit_code = await main_module.amain(settings=settings, mt5_module=mt5_stub)
    assert exit_code == main_module.EXIT_CONNECT_FAILED


def test_select_mt5_module_returns_stub_on_linux() -> None:
    """Acceptance criterion 10: on non-Windows the entry point picks
    the stub. We're running on Linux CI so we can assert directly."""
    assert sys.platform != "win32", "this assertion is for Linux dev/CI"
    mod = main_module._select_mt5_module()
    assert mod is mt5_stub
