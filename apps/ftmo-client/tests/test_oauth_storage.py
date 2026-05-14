"""Save/load roundtrip + expiry boundary tests for oauth_storage."""

from __future__ import annotations

import time

import fakeredis.aioredis
import pytest

from ftmo_client.ctrader_oauth import TokenResponse
from ftmo_client.oauth_storage import (
    TokenData,
    is_token_expired,
    load_token,
    save_token,
)


@pytest.mark.asyncio
async def test_save_and_load_roundtrip(fake_redis: fakeredis.aioredis.FakeRedis) -> None:
    token_in = TokenResponse(
        access_token="acc-123",
        refresh_token="ref-456",
        expires_in=3600,
        token_type="Bearer",
    )
    await save_token(fake_redis, "ftmo_001", token_in, ctid_trader_account_id=42)

    loaded = await load_token(fake_redis, "ftmo_001")
    assert loaded is not None
    assert loaded["access_token"] == "acc-123"
    assert loaded["refresh_token"] == "ref-456"
    assert loaded["ctid_trader_account_id"] == "42"
    assert loaded["token_type"] == "Bearer"
    # expires_at = saved_at + 3600s. Both are written as epoch ms strings.
    saved_at_ms = int(loaded["saved_at"])
    expires_at_ms = int(loaded["expires_at"])
    assert expires_at_ms - saved_at_ms == 3600 * 1000


@pytest.mark.asyncio
async def test_load_returns_none_for_missing_account(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    assert await load_token(fake_redis, "ftmo_missing") is None


def test_is_token_expired_far_future_returns_false() -> None:
    """A token expiring 1h from now with default 5m skew is not expired."""
    now_ms = int(time.time() * 1000)
    token: TokenData = {
        "access_token": "x",
        "refresh_token": "y",
        "expires_at": str(now_ms + 3600 * 1000),
        "saved_at": str(now_ms),
        "ctid_trader_account_id": "42",
        "token_type": "Bearer",
    }
    assert is_token_expired(token) is False


def test_is_token_expired_already_past_returns_true() -> None:
    """A token whose expiry is in the past is expired regardless of skew."""
    past_ms = int(time.time() * 1000) - 60_000
    token: TokenData = {
        "access_token": "x",
        "refresh_token": "y",
        "expires_at": str(past_ms),
        "saved_at": str(past_ms - 10_000),
        "ctid_trader_account_id": "42",
        "token_type": "Bearer",
    }
    assert is_token_expired(token) is True


def test_is_token_expired_inside_skew_returns_true() -> None:
    """Token expiring 60s from now is treated as expired with default 300s skew."""
    soon_ms = int(time.time() * 1000) + 60_000
    token: TokenData = {
        "access_token": "x",
        "refresh_token": "y",
        "expires_at": str(soon_ms),
        "saved_at": str(soon_ms - 1000),
        "ctid_trader_account_id": "42",
        "token_type": "Bearer",
    }
    assert is_token_expired(token, skew_seconds=300) is True


def test_is_token_expired_skew_boundary_exact_returns_true() -> None:
    """At exactly the skew boundary the token is treated as expired (<=)."""
    skew = 300
    boundary_ms = int(time.time() * 1000) + skew * 1000
    token: TokenData = {
        "access_token": "x",
        "refresh_token": "y",
        "expires_at": str(boundary_ms),
        "saved_at": str(boundary_ms - 1000),
        "ctid_trader_account_id": "42",
        "token_type": "Bearer",
    }
    # Boundary case: expires_at - now == skew * 1000 → expired.
    assert is_token_expired(token, skew_seconds=skew) is True


def test_is_token_expired_malformed_expires_at_returns_true() -> None:
    """A non-integer ``expires_at`` is treated as expired (force re-auth)."""
    token: TokenData = {
        "access_token": "x",
        "refresh_token": "y",
        "expires_at": "not-a-number",
        "saved_at": "0",
        "ctid_trader_account_id": "42",
        "token_type": "Bearer",
    }
    assert is_token_expired(token) is True
