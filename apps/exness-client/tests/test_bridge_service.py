"""MT5BridgeService connect / health / hedging-mode assertion tests."""

from __future__ import annotations

import pytest

from exness_client import mt5_stub
from exness_client.bridge_service import (
    MT5BridgeService,
    MT5ConnectError,
    MT5HedgingModeRequiredError,
)
from exness_client.config import ExnessClientSettings


@pytest.mark.asyncio
async def test_connect_happy_path_hedging_mode(settings: ExnessClientSettings) -> None:
    """Default stub state is hedging mode → connect succeeds."""
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    assert not bridge.is_connected()
    await bridge.connect()
    assert bridge.is_connected()
    await bridge.disconnect()
    assert not bridge.is_connected()


@pytest.mark.asyncio
async def test_connect_netting_mode_raises_hedging_required(
    settings: ExnessClientSettings,
) -> None:
    """CTO Q7: connect MUST fail-fast on netting margin mode."""
    mt5_stub.set_state_for_tests(
        account_info=mt5_stub.AccountInfo(
            login=1, balance=0.0, currency="USD",
            margin_mode=mt5_stub.ACCOUNT_MARGIN_MODE_RETAIL_NETTING,
            leverage=100, server="s",
        ),
    )
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    with pytest.raises(MT5HedgingModeRequiredError) as excinfo:
        await bridge.connect()
    assert excinfo.value.observed_mode == 0
    assert not bridge.is_connected()


@pytest.mark.asyncio
async def test_connect_exchange_mode_raises_hedging_required(
    settings: ExnessClientSettings,
) -> None:
    mt5_stub.set_state_for_tests(
        account_info=mt5_stub.AccountInfo(
            login=1, balance=0.0, currency="USD",
            margin_mode=mt5_stub.ACCOUNT_MARGIN_MODE_EXCHANGE,
            leverage=100, server="s",
        ),
    )
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    with pytest.raises(MT5HedgingModeRequiredError):
        await bridge.connect()


@pytest.mark.asyncio
async def test_connect_initialize_returns_false_raises(
    settings: ExnessClientSettings,
) -> None:
    """``mt5.initialize`` returning False surfaces as MT5ConnectError with last_error."""
    mt5_stub.set_state_for_tests(init_should_fail=True)
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    with pytest.raises(MT5ConnectError) as excinfo:
        await bridge.connect()
    assert excinfo.value.last_error[0] == -10003


@pytest.mark.asyncio
async def test_connect_account_info_none_raises(settings: ExnessClientSettings) -> None:
    """If ``mt5.initialize`` succeeds but account_info returns None → MT5ConnectError."""
    mt5_stub.set_state_for_tests(account_info=None)
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    with pytest.raises(MT5ConnectError):
        await bridge.connect()
    # Stub state cleanup happens in autouse fixture.


@pytest.mark.asyncio
async def test_disconnect_idempotent(settings: ExnessClientSettings) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    await bridge.disconnect()  # never connected — must not raise
    await bridge.connect()
    await bridge.disconnect()
    await bridge.disconnect()  # second call no-op
    assert not bridge.is_connected()


@pytest.mark.asyncio
async def test_health_check_when_disconnected(settings: ExnessClientSettings) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    health = await bridge.health_check()
    assert health["connected"] is False
    assert health["terminal_ok"] is False
    assert health["trade_allowed"] is False
    assert health["account_login"] == 0
    assert "checked_at" in health


@pytest.mark.asyncio
async def test_health_check_when_connected(settings: ExnessClientSettings) -> None:
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    await bridge.connect()
    health = await bridge.health_check()
    assert health["connected"] is True
    assert health["terminal_ok"] is True
    assert health["trade_allowed"] is True
    assert health["account_login"] == 12345678
    assert isinstance(health["checked_at"], int)


@pytest.mark.asyncio
async def test_health_check_terminal_disconnected(settings: ExnessClientSettings) -> None:
    """Connected to MT5 lib but terminal_info reports disconnected → terminal_ok False."""
    bridge = MT5BridgeService(settings, mt5_module=mt5_stub)
    await bridge.connect()
    mt5_stub.set_state_for_tests(
        terminal_info=mt5_stub.TerminalInfo(
            connected=False, trade_allowed=False, name="stub"
        ),
    )
    health = await bridge.health_check()
    assert health["connected"] is True
    assert health["terminal_ok"] is False
    assert health["trade_allowed"] is False
