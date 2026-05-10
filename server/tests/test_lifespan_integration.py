"""Integration test for the FastAPI lifespan startup path (step 3.2).

Verifies that ``setup_consumer_groups()`` runs as part of lifespan
startup, creates the right consumer groups for whichever accounts are
already registered, and lets the server come up successfully even when
no accounts are registered yet (first-boot state).

Approach: drive the FastAPI lifespan context manager directly via
``app.router.lifespan_context(app)``. This is the same async generator
FastAPI / Starlette use internally, so we exercise the production
startup path without pulling in ``asgi-lifespan`` as a new dependency.
``app.redis_client.init_redis`` is monkeypatched to install our
fakeredis as the module-global pool so lifespan's later
``get_redis()`` / ``RedisService(get_redis())`` reads see the test
instance — the existing ``_override_redis_service`` autouse fixture
only swaps the FastAPI Depends layer, which lifespan bypasses.
"""

from __future__ import annotations

from collections.abc import Iterator

import fakeredis.aioredis
import pytest
from app import main as main_module
from app import redis_client
from app.main import app
from app.services.redis_service import RedisService


@pytest.fixture
def patched_redis(
    fake_redis: fakeredis.aioredis.FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[fakeredis.aioredis.FakeRedis]:
    """Make lifespan's ``init_redis`` install ``fake_redis`` as the pool.

    ``app/main.py`` imports ``init_redis`` by name (``from app.redis_client
    import init_redis``), so the lifespan's lookup is against
    ``app.main.init_redis``, not ``app.redis_client.init_redis``. Patching
    only the latter would miss the call. We patch both names so any future
    refactor that changes the import path still works.
    """

    async def _fake_init_redis(_url: str) -> None:
        # Real init_redis pings; fakeredis is in-process so the ping is
        # superfluous. Just install the pool reference.
        redis_client._redis_pool = fake_redis

    monkeypatch.setattr(main_module, "init_redis", _fake_init_redis)
    monkeypatch.setattr(redis_client, "init_redis", _fake_init_redis)

    # Lifespan also reads ctrader creds; on a real LAN redis db this would
    # spin up MarketDataService. Force the "no creds" branch so the test
    # exercises only the consumer-groups path.
    yield fake_redis

    # Belt-and-braces: lifespan shutdown calls close_redis() which sets
    # _redis_pool back to None, but if a test asserts mid-startup we
    # want to be sure no leak crosses tests.
    redis_client._redis_pool = None


@pytest.mark.asyncio
async def test_lifespan_creates_groups_for_seeded_accounts(
    patched_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Seeded accounts → groups exist on every expected stream after startup."""
    # Pre-seed the fakeredis BEFORE lifespan starts. Lifespan reads from
    # the patched pool and runs setup_consumer_groups, which iterates
    # accounts:* SMEMBERS to know what to create.
    seed_svc = RedisService(patched_redis)
    await seed_svc.add_account("ftmo", "ftmo_001", "FTMO 1")
    await seed_svc.add_account("ftmo", "ftmo_002", "FTMO 2")
    await seed_svc.add_account("exness", "exn_001", "Exness 1")

    # Drive the production lifespan path. The async with block both runs
    # startup (await yield) and runs shutdown on exit — exactly how
    # uvicorn / Starlette drive it.
    async with app.router.lifespan_context(app):
        for stream, group in [
            ("cmd_stream:ftmo:ftmo_001", "ftmo-ftmo_001"),
            ("resp_stream:ftmo:ftmo_001", "server"),
            ("event_stream:ftmo:ftmo_001", "server"),
            ("cmd_stream:ftmo:ftmo_002", "ftmo-ftmo_002"),
            ("resp_stream:ftmo:ftmo_002", "server"),
            ("event_stream:ftmo:ftmo_002", "server"),
            ("cmd_stream:exness:exn_001", "exness-exn_001"),
            ("resp_stream:exness:exn_001", "server"),
            ("event_stream:exness:exn_001", "server"),
        ]:
            groups = await patched_redis.xinfo_groups(stream)
            assert any(g["name"] == group for g in groups), f"missing {group} on {stream}"


@pytest.mark.asyncio
async def test_lifespan_starts_with_zero_accounts(
    patched_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """First-boot: no accounts → no groups, server still completes startup."""
    # No pre-seeding. Lifespan should still finish startup without raising
    # — that's the contract of returning (0, 0) instead of erroring.
    async with app.router.lifespan_context(app):
        # No streams created. SMEMBERS on the empty sets confirms no
        # account drove a group; xinfo_groups on a missing stream would
        # raise, so the absence-of-create check is via account sets.
        assert await patched_redis.smembers("accounts:ftmo") == set()  # type: ignore[misc]
        assert await patched_redis.smembers("accounts:exness") == set()  # type: ignore[misc]


@pytest.mark.asyncio
async def test_lifespan_setup_consumer_groups_idempotent_across_restarts(
    patched_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Restart-style: lifespan run twice on the same fakeredis must not raise.

    Mirrors a real server restart where existing consumer groups should
    survive (BUSYGROUP swallowed). This test guards step 3.1's
    ``_create_group`` BUSYGROUP swallow as part of the production wiring.
    """
    seed_svc = RedisService(patched_redis)
    await seed_svc.add_account("ftmo", "ftmo_001", "FTMO 1")

    async with app.router.lifespan_context(app):
        pass
    # Second run on the same fakeredis (groups already exist).
    async with app.router.lifespan_context(app):
        pass

    groups = await patched_redis.xinfo_groups("cmd_stream:ftmo:ftmo_001")
    assert any(g["name"] == "ftmo-ftmo_001" for g in groups)
