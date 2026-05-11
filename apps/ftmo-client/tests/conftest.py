"""Shared fixtures for ftmo-client tests.

Mirrors the server's conftest pattern: a fresh fakeredis per test, no
network IO. Tests construct ``RedisService`` instances on top of it
when they need higher-level methods (e.g. seeding accounts), but most
ftmo-client modules talk to the raw redis-py client directly so
fakeredis suffices.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest


@pytest.fixture
def fake_redis() -> fakeredis.aioredis.FakeRedis:
    """A fresh in-process fakeredis client per test, decoded as strings."""
    return fakeredis.aioredis.FakeRedis(decode_responses=True)
