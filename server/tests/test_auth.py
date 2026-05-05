"""Tests for /api/auth/login and the JWT/bcrypt helpers in app.auth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from app.auth import (
    ALGORITHM,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)
from httpx import AsyncClient

SECRET = "unit-test-secret-at-least-32-chars-long-yyyyy"


@pytest.mark.asyncio
async def test_login_success(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert isinstance(body["access_token"], str)
    assert body["access_token"].count(".") == 2  # header.payload.signature


@pytest.mark.asyncio
async def test_login_wrong_username(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/auth/login",
        json={"username": "not-admin", "password": "admin"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


@pytest.mark.asyncio
async def test_login_wrong_password(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "WRONG"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid credentials"


@pytest.mark.asyncio
async def test_login_missing_field(client: AsyncClient) -> None:
    resp = await client.post("/api/auth/login", json={"username": "admin"})
    assert resp.status_code == 422


def test_create_decode_roundtrip() -> None:
    token = create_access_token(subject="alice", secret=SECRET, expires_minutes=15)
    payload = decode_access_token(token, SECRET)
    assert payload["sub"] == "alice"
    assert payload["type"] == "access"
    assert "exp" in payload and "iat" in payload


def test_decode_expired_token() -> None:
    expired = jwt.encode(
        {
            "sub": "alice",
            "iat": datetime.now(UTC) - timedelta(hours=2),
            "exp": datetime.now(UTC) - timedelta(hours=1),
            "type": "access",
        },
        SECRET,
        algorithm=ALGORITHM,
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(expired, SECRET)


def test_decode_invalid_signature() -> None:
    token = create_access_token(subject="alice", secret=SECRET, expires_minutes=15)
    with pytest.raises(jwt.InvalidTokenError):
        decode_access_token(token, "different-secret-also-32-chars-long-zzzz")


def test_hash_verify_roundtrip() -> None:
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True
    assert verify_password("wrong", h) is False
