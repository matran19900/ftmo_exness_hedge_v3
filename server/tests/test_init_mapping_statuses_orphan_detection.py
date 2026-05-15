"""Step 4.5a — ``_init_mapping_statuses`` orphan-pointer detection.

Defensive layer for any pre-4.5a Redis state that leaked a
``account_to_mapping:{id}`` pointer past ``remove_account`` (the bug
documented in verify-mapping-status-leak.md). On every server boot, if
the lifespan sees an Exness account with a pointer but no status key, it
must treat the pointer as a ghost — delete it and force
``mapping_status="pending_mapping"`` so the wizard runs again. The
fresh-account fallback (no pointer, no status → pending_mapping) is
preserved; status keys already present are still left alone.
"""

from __future__ import annotations

import logging

import fakeredis.aioredis
import pytest
from app.main import _init_mapping_statuses


@pytest.mark.asyncio
async def test_orphan_pointer_detected_and_cleared(
    fake_redis: fakeredis.aioredis.FakeRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Criterion #9 (sample test in self-check §7): pointer + no status →
    orphan branch fires. Pointer is deleted, status forced to
    ``pending_mapping``, and a WARNING log row is emitted with the
    account_id so a CEO inspecting boot logs sees the legacy cleanup."""
    await fake_redis.sadd("accounts:exness", "exness_001")
    await fake_redis.set("account_to_mapping:exness_001", "sig_ghost")
    # No mapping_status:exness_001 key — that's the orphan condition.

    with caplog.at_level(logging.WARNING, logger="app.main"):
        count = await _init_mapping_statuses(fake_redis)

    assert count == 1
    assert await fake_redis.get("mapping_status:exness_001") == "pending_mapping"
    assert await fake_redis.get("account_to_mapping:exness_001") is None
    assert any(
        "orphan_pointer_detected" in rec.getMessage()
        and "exness_001" in rec.getMessage()
        for rec in caplog.records
    )


@pytest.mark.asyncio
async def test_existing_active_status_respected(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Criterion #10: an explicit ``mapping_status="active"`` key from a
    successful wizard save is preserved verbatim. Pointer (if any) is
    untouched too — the lifespan never overwrites operator-confirmed
    state on restart."""
    await fake_redis.sadd("accounts:exness", "exness_001")
    await fake_redis.set("mapping_status:exness_001", "active")
    await fake_redis.set("account_to_mapping:exness_001", "sig_real")

    count = await _init_mapping_statuses(fake_redis)

    assert count == 0
    assert await fake_redis.get("mapping_status:exness_001") == "active"
    assert await fake_redis.get("account_to_mapping:exness_001") == "sig_real"


@pytest.mark.asyncio
async def test_existing_pending_status_respected(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Criterion #10 (pending variant): a ``pending_mapping`` key from an
    earlier boot survives the next lifespan untouched."""
    await fake_redis.sadd("accounts:exness", "exness_001")
    await fake_redis.set("mapping_status:exness_001", "pending_mapping")

    count = await _init_mapping_statuses(fake_redis)

    assert count == 0
    assert await fake_redis.get("mapping_status:exness_001") == "pending_mapping"


@pytest.mark.asyncio
async def test_fresh_account_no_pointer_defaults_pending(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Criterion #12: clean fresh-account path is unchanged — no pointer
    and no status → ``pending_mapping`` so AccountsTab renders the
    "Map Symbols" CTA on first paint."""
    await fake_redis.sadd("accounts:exness", "exness_001")

    count = await _init_mapping_statuses(fake_redis)

    assert count == 1
    assert await fake_redis.get("mapping_status:exness_001") == "pending_mapping"


@pytest.mark.asyncio
async def test_lifespan_idempotent_second_call_noop(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Criterion #12: ``_init_mapping_statuses`` is safe to call twice (the
    test boot harness does this). Second invocation finds every key
    present from the first call and returns 0 with no mutation."""
    await fake_redis.sadd("accounts:exness", "exness_001", "exness_002")
    await fake_redis.set("account_to_mapping:exness_002", "sig_ghost")

    first = await _init_mapping_statuses(fake_redis)
    assert first == 2  # exness_001 fresh + exness_002 orphan
    second = await _init_mapping_statuses(fake_redis)
    assert second == 0

    assert await fake_redis.get("mapping_status:exness_001") == "pending_mapping"
    assert await fake_redis.get("mapping_status:exness_002") == "pending_mapping"
    assert await fake_redis.get("account_to_mapping:exness_002") is None


@pytest.mark.asyncio
async def test_empty_exness_set_returns_zero(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Criterion #15: no Exness accounts registered → no-op, count=0."""
    count = await _init_mapping_statuses(fake_redis)
    assert count == 0


@pytest.mark.asyncio
async def test_ftmo_accounts_not_touched(
    fake_redis: fakeredis.aioredis.FakeRedis,
) -> None:
    """Criterion #13: only ``accounts:exness`` is scanned — FTMO members
    are not given mapping_status keys, and any pre-existing
    ``mapping_status`` / ``account_to_mapping`` keys for FTMO ids stay
    inert (mapping is Exness-only by design)."""
    await fake_redis.sadd("accounts:ftmo", "ftmo_001")
    # Even if junk keys happen to share the FTMO id namespace, the loop
    # iterates ``accounts:exness`` only and won't see them.
    await fake_redis.set("mapping_status:ftmo_001", "active")  # noise

    count = await _init_mapping_statuses(fake_redis)

    assert count == 0
    # Untouched — _init_mapping_statuses didn't iterate ftmo members.
    assert await fake_redis.get("mapping_status:ftmo_001") == "active"
