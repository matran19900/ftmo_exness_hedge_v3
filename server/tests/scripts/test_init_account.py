"""Smoke tests for ``scripts/init_account.py``.

We patch the script's ``_open_service`` helper to return a fakeredis-backed
``RedisService`` instead of opening a real Redis connection. The CLI is
exercised by parsing argv through the production parser and awaiting
``_dispatch`` directly — sidestepping ``main()``'s ``asyncio.run(...)``,
which can't be re-entered from a pytest-asyncio test that's already
running inside an event loop. The argparse + handler layers under test
are still the production code paths.

Coverage: add happy + 3 validation paths, list empty + populated, remove
dry-run + confirmed + nonexistent. Ten test cases — meets §3 §8 (≥8).
"""

from __future__ import annotations

import io

import fakeredis.aioredis
import pytest
from app.services.redis_service import RedisService

from scripts import init_account  # type: ignore[import-not-found]


@pytest.fixture
def patched_open_service(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> fakeredis.aioredis.FakeRedis:
    """Replace ``_open_service`` with a stub that returns a fakeredis service.

    The real helper would call ``redis_asyncio.from_url(...)`` against
    whatever URL Settings reports — fine for production, fatal for tests.
    The stub also yields the fake_redis client so its ``aclose()`` in the
    finally block of every command is a safe no-op.
    """

    async def _stub_open() -> tuple[RedisService, fakeredis.aioredis.FakeRedis]:
        return RedisService(fake_redis), fake_redis

    monkeypatch.setattr(init_account, "_open_service", _stub_open)
    return fake_redis


async def _run(argv: list[str]) -> tuple[int, str, str]:
    """Parse argv and await ``_dispatch`` with captured streams.

    Avoids ``main()``'s ``asyncio.run`` which can't be called from a
    pytest-asyncio test that is already inside an event loop.
    """
    out = io.StringIO()
    err = io.StringIO()
    parser = init_account._build_parser()
    args = parser.parse_args(argv)
    rc = await init_account._dispatch(args, out, err)
    return rc, out.getvalue(), err.getvalue()


@pytest.mark.asyncio
async def test_add_happy_path_creates_account(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    rc, out, err = await _run(
        [
            "add",
            "--broker",
            "ftmo",
            "--account-id",
            "ftmo_acc_001",
            "--name",
            "FTMO Challenge $100k",
        ]
    )

    assert rc == init_account.EXIT_OK, err
    assert "Account added: ftmo / ftmo_acc_001 (enabled=true)" in out
    assert "Meta key: account_meta:ftmo:ftmo_acc_001" in out
    assert "Restart server now" in out

    # Verify Redis state matches RedisService.add_account semantics.
    svc = RedisService(patched_open_service)
    meta = await svc.get_account_meta("ftmo", "ftmo_acc_001")
    assert meta is not None and meta["name"] == "FTMO Challenge $100k"
    assert meta["enabled"] == "true"
    assert "ftmo_acc_001" in await svc.get_all_account_ids("ftmo")


def test_add_rejects_bad_broker_pre_argparse() -> None:
    """argparse `choices=` rejects bad broker before our handler runs.

    Sync test on purpose: argparse raises SystemExit during parse, before
    any async code runs, so this never enters the event loop. Exit code 2
    + a usage line on stderr is argparse's own behavior; we just confirm
    the exit code so the test isn't coupled to argparse's wording.
    """
    parser = init_account._build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["add", "--broker", "WRONG", "--account-id", "x_001", "--name", "X"])
    assert exc_info.value.code == 2


@pytest.mark.asyncio
async def test_add_rejects_bad_account_id_format(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    rc, out, err = await _run(["add", "--broker", "ftmo", "--account-id", "Bad-ID!", "--name", "X"])
    assert rc == init_account.EXIT_VALIDATION
    assert "must match" in err
    # No account written.
    svc = RedisService(patched_open_service)
    assert await svc.get_all_account_ids("ftmo") == []


@pytest.mark.asyncio
async def test_add_duplicate_returns_validation_error(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    rc1, _, _ = await _run(
        ["add", "--broker", "ftmo", "--account-id", "ftmo_001", "--name", "First"]
    )
    assert rc1 == init_account.EXIT_OK

    rc2, out2, err2 = await _run(
        ["add", "--broker", "ftmo", "--account-id", "ftmo_001", "--name", "Second"]
    )
    assert rc2 == init_account.EXIT_VALIDATION
    assert "already exists" in err2
    # The original "First" name survives — duplicate must not overwrite.
    svc = RedisService(patched_open_service)
    meta = await svc.get_account_meta("ftmo", "ftmo_001")
    assert meta is not None and meta["name"] == "First"


@pytest.mark.asyncio
async def test_list_empty(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    rc, out, err = await _run(["list"])
    assert rc == init_account.EXIT_OK
    assert "== ftmo (0 accounts) ==" in out
    assert "== exness (0 accounts) ==" in out


@pytest.mark.asyncio
async def test_list_populated_shows_status(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(patched_open_service)
    await svc.add_account("ftmo", "ftmo_001", "FTMO 1")
    await svc.add_account("exness", "exn_001", "Exness 1")
    # Mark ftmo_001 online via heartbeat key so we exercise the status read.
    await patched_open_service.setex("client:ftmo:ftmo_001", 30, "alive")

    rc, out, err = await _run(["list"])
    assert rc == init_account.EXIT_OK
    assert "== ftmo (1 accounts) ==" in out
    assert 'ftmo_001  "FTMO 1"  enabled=true  status=online' in out
    assert "== exness (1 accounts) ==" in out
    assert 'exn_001  "Exness 1"  enabled=true  status=offline' in out


@pytest.mark.asyncio
async def test_list_filtered_by_broker(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(patched_open_service)
    await svc.add_account("ftmo", "ftmo_001", "FTMO 1")
    await svc.add_account("exness", "exn_001", "Exness 1")

    rc, out, _ = await _run(["list", "--broker", "ftmo"])
    assert rc == init_account.EXIT_OK
    assert "== ftmo (1 accounts) ==" in out
    # exness section should NOT be rendered.
    assert "exness" not in out


@pytest.mark.asyncio
async def test_remove_dry_run_without_yes(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(patched_open_service)
    await svc.add_account("ftmo", "ftmo_001", "FTMO 1")

    rc, out, err = await _run(["remove", "--broker", "ftmo", "--account-id", "ftmo_001"])
    assert rc == init_account.EXIT_VALIDATION
    assert "Would remove: ftmo / ftmo_001" in out
    assert "Pass --yes to confirm" in out

    # Account still present — dry-run must not touch state.
    assert await svc.get_account_meta("ftmo", "ftmo_001") is not None


@pytest.mark.asyncio
async def test_remove_with_yes_drops_account(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    svc = RedisService(patched_open_service)
    await svc.add_account("ftmo", "ftmo_001", "FTMO 1")
    await patched_open_service.setex("client:ftmo:ftmo_001", 30, "alive")

    rc, out, err = await _run(["remove", "--broker", "ftmo", "--account-id", "ftmo_001", "--yes"])
    assert rc == init_account.EXIT_OK
    assert "Account removed: ftmo / ftmo_001" in out
    assert "NOT deleted" in out  # the orders-not-deleted note

    assert await svc.get_account_meta("ftmo", "ftmo_001") is None
    assert await svc.get_all_account_ids("ftmo") == []
    assert await svc.get_client_status("ftmo", "ftmo_001") == "offline"


@pytest.mark.asyncio
async def test_remove_nonexistent_returns_validation_error(
    patched_open_service: fakeredis.aioredis.FakeRedis,
) -> None:
    rc, out, err = await _run(
        ["remove", "--broker", "ftmo", "--account-id", "no_such_acc", "--yes"]
    )
    assert rc == init_account.EXIT_VALIDATION
    assert "does not exist" in err
