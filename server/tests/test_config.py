"""Tests for Settings robustness — CORS parsing + cwd-independent .env loading."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import ENV_FILE_PATH, Settings


def test_cors_origins_comma_separated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", "http://a.example,http://b.example")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.cors_origins == ["http://a.example", "http://b.example"]


def test_cors_origins_json_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", '["http://a.example","http://b.example"]')
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.cors_origins == ["http://a.example", "http://b.example"]


def test_cors_origins_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.cors_origins == ["http://localhost:5173"]


def test_cors_origins_with_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORS_ORIGINS", " http://a.example , http://b.example ")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.cors_origins == ["http://a.example", "http://b.example"]


def test_env_file_loads_from_any_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Settings should locate the repo .env even when cwd is unrelated."""
    assert ENV_FILE_PATH.is_absolute()

    if not ENV_FILE_PATH.is_file() or "JWT_SECRET=" not in ENV_FILE_PATH.read_text(
        encoding="utf-8"
    ):
        pytest.skip("Repo .env does not contain JWT_SECRET; cannot exercise dotenv fallback.")

    monkeypatch.chdir(tmp_path)
    # Drop the env var so the value can only come from the dotenv at repo root.
    monkeypatch.delenv("JWT_SECRET", raising=False)

    s = Settings()
    assert len(s.jwt_secret) >= 32
