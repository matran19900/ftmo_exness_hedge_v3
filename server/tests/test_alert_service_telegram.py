"""Step 4.11 — AlertService Telegram dispatch + bypass_cooldown +
registry-extension tests.

Covers the 4.11 additions on top of the 4.7b contract (which
``test_alert_service.py`` already locks):

  - 3 new alert types (``hedge_closed`` INFO, ``leg_orphaned`` /
    ``secondary_liquidation`` CRITICAL) registered with the design
    severities and the cooldown=0 / 30-day-TTL shape.
  - Telegram dispatch path: success, 5xx, network exception, disabled,
    no http_client (defensive) — none of them affect the Redis HASH
    write or the WS broadcast.
  - ``bypass_cooldown=True`` lets the same (alert_type, cooldown_key)
    fire twice within a single cooldown window.
  - Plain-text format passes special characters verbatim (no
    parse_mode escaping).

These exercise only the AlertService surface. The fire-site
integration tests (HedgeService cascade success / secondary_failed,
event_handler stop_out split) live in their respective service test
files so they share fixtures with the existing 4.7b/4.8 suite.
"""

from __future__ import annotations

from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from app.services.alert_service import ALERT_TYPES, AlertService
from app.services.broadcast import BroadcastService
from app.services.redis_service import RedisService


class _CapturingBroadcast(BroadcastService):
    def __init__(self) -> None:
        super().__init__(redis_svc=None)
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, channel: str, data: dict[str, Any]) -> None:
        self.published.append((channel, data))


class _FakeResponse:
    def __init__(self, status_code: int = 200, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _RecordingHttpClient:
    """Drop-in for ``httpx.AsyncClient`` that records calls + lets
    each test inject a response or an exception per ``.post``."""

    def __init__(
        self,
        response: _FakeResponse | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.response = response or _FakeResponse(status_code=200)
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        if self.raises is not None:
            raise self.raises
        return self.response


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.fixture
def redis_svc(redis_client: fakeredis.aioredis.FakeRedis) -> RedisService:
    return RedisService(redis_client)


@pytest.fixture
def broadcast() -> _CapturingBroadcast:
    return _CapturingBroadcast()


def _build_service(
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    *,
    http_client: Any | None = None,
    enabled: bool = True,
    token: str | None = "fake-token",
    chat_id: str | None = "fake-chat",
) -> AlertService:
    return AlertService(
        redis_svc,
        broadcast,
        http_client=http_client,
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        telegram_enabled=enabled,
    )


# ---------- registry extension ----------


def test_alert_types_registry_includes_step_4_11_types() -> None:
    """3 new types registered with the design severities + per-type
    spec (cooldown=0 + extended 30-day TTL for the criticals)."""
    assert ALERT_TYPES["hedge_closed"]["severity"] == "INFO"
    assert ALERT_TYPES["hedge_closed"]["cooldown_seconds"] == 0
    assert ALERT_TYPES["hedge_closed"]["emoji"] == "✅"

    assert ALERT_TYPES["leg_orphaned"]["severity"] == "CRITICAL"
    assert ALERT_TYPES["leg_orphaned"]["cooldown_seconds"] == 0
    assert ALERT_TYPES["leg_orphaned"]["emoji"] == "🚨"
    assert ALERT_TYPES["leg_orphaned"]["ttl_seconds"] == 30 * 86400

    assert ALERT_TYPES["secondary_liquidation"]["severity"] == "CRITICAL"
    assert ALERT_TYPES["secondary_liquidation"]["cooldown_seconds"] == 0
    assert ALERT_TYPES["secondary_liquidation"]["ttl_seconds"] == 30 * 86400


def test_alert_types_registry_4_7b_types_unchanged() -> None:
    """Regression guard: step 4.11 must NOT mutate the 4.7b spec for
    the two original WARNING types."""
    for name in (
        "hedge_leg_external_close_warning",
        "hedge_leg_external_modify_warning",
    ):
        spec = ALERT_TYPES[name]
        assert spec["severity"] == "WARN"
        assert spec["cooldown_seconds"] == 300
        assert spec["ttl_seconds"] == 7 * 86400


# ---------- Telegram dispatch happy path ----------


@pytest.mark.asyncio
async def test_telegram_dispatch_success_calls_httpx_post(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """On a normal emit with enabled+token+chat the dispatcher fires
    one POST against ``api.telegram.org/bot{token}/sendMessage`` with a
    plain-text body. The alert_id returned is non-None (publish path
    succeeded)."""
    http = _RecordingHttpClient()
    svc = _build_service(redis_svc, broadcast, http_client=http)
    alert_id = await svc.emit(
        alert_type="hedge_closed",
        cooldown_key="ord_42",
        title_vi="✅ Đã đóng",
        body_vi="Order ord_42: cả 2 chân đã đóng.",
        context={"order_id": "ord_42", "outcome": "cascade_completed"},
    )
    assert alert_id is not None
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == "https://api.telegram.org/botfake-token/sendMessage"
    assert call["json"]["chat_id"] == "fake-chat"
    # parse_mode MUST NOT be set — design §2.E.4 plain-text.
    assert "parse_mode" not in call["json"]
    # Body composition: title (caller embeds emoji) + body + context.
    text = call["json"]["text"]
    assert text.startswith("✅ Đã đóng")
    # The emoji must appear exactly once — the formatter no longer
    # prepends ``payload.emoji`` because the 4.7b convention already
    # embeds the emoji in ``title_vi``.
    assert text.count("✅") == 1
    assert "Order ord_42" in text
    assert "order_id: ord_42" in text
    assert "outcome: cascade_completed" in text


# ---------- Telegram disabled = skip POST ----------


@pytest.mark.asyncio
async def test_telegram_dispatch_disabled_skips_httpx_call(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    http = _RecordingHttpClient()
    svc = _build_service(redis_svc, broadcast, http_client=http, enabled=False)
    alert_id = await svc.emit(
        alert_type="hedge_closed",
        cooldown_key="ord_disabled",
        title_vi="t", body_vi="b", context={"k": "v"},
    )
    assert alert_id is not None
    # No HTTP attempt.
    assert http.calls == []
    # Redis + WS publish still happened.
    keys = [k async for k in redis_client.scan_iter(match="alert:*")]
    assert len(keys) == 1
    assert len(broadcast.published) == 1


# ---------- Telegram 5xx logs WARN, does NOT fail emit ----------


@pytest.mark.asyncio
async def test_telegram_dispatch_5xx_does_not_fail_emit(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 503 from Telegram is logged WARNING and swallowed — the
    alert_id is still returned because Redis HASH + WS publish landed
    first. Phase 4 design §2.B.4: no retry queue."""
    import logging
    caplog.set_level(logging.WARNING)
    http = _RecordingHttpClient(
        response=_FakeResponse(status_code=503, text="server overloaded"),
    )
    svc = _build_service(redis_svc, broadcast, http_client=http)
    alert_id = await svc.emit(
        alert_type="hedge_closed",
        cooldown_key="ord_5xx",
        title_vi="t", body_vi="b", context={},
    )
    assert alert_id is not None
    assert len(http.calls) == 1
    # WARNING log captured.
    assert any(
        "telegram_dispatch_failed" in rec.getMessage()
        and "status=503" in rec.getMessage()
        for rec in caplog.records
    )


# ---------- Telegram network exception path ----------


@pytest.mark.asyncio
async def test_telegram_dispatch_network_exception_does_not_fail_emit(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    caplog.set_level(logging.WARNING)
    http = _RecordingHttpClient(raises=httpx.ConnectError("dns failure"))
    svc = _build_service(redis_svc, broadcast, http_client=http)
    alert_id = await svc.emit(
        alert_type="hedge_closed",
        cooldown_key="ord_neterr",
        title_vi="t", body_vi="b", context={},
    )
    assert alert_id is not None
    assert any(
        "telegram_dispatch_exception" in rec.getMessage()
        for rec in caplog.records
    )


# ---------- Telegram skipped when http_client is None (defensive) ----------


@pytest.mark.asyncio
async def test_telegram_dispatch_no_client_short_circuits(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """A test fixture or misconfigured boot can produce
    ``http_client=None`` while ``telegram_enabled=True``. The
    dispatcher must not crash — it logs INFO and skips."""
    svc = _build_service(
        redis_svc, broadcast, http_client=None, enabled=True
    )
    alert_id = await svc.emit(
        alert_type="hedge_closed",
        cooldown_key="ord_noclient",
        title_vi="t", body_vi="b", context={},
    )
    assert alert_id is not None
    # Publish path still landed.
    assert len(broadcast.published) == 1


# ---------- bypass_cooldown ----------


@pytest.mark.asyncio
async def test_bypass_cooldown_allows_repeat_emit_within_window(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Two emits with the same (alert_type, cooldown_key) within the
    cooldown window: the second is normally suppressed, but
    ``bypass_cooldown=True`` lets it land. Telegram POST fires twice."""
    http = _RecordingHttpClient()
    svc = _build_service(redis_svc, broadcast, http_client=http)

    first = await svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_bypass",
        title_vi="t", body_vi="b", context={},
    )
    assert first is not None

    # Without bypass the cooldown would suppress this.
    suppressed = await svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_bypass",
        title_vi="t", body_vi="b", context={},
    )
    assert suppressed is None

    # With bypass it lands.
    bypassed = await svc.emit(
        alert_type="hedge_leg_external_close_warning",
        cooldown_key="ord_bypass",
        title_vi="t", body_vi="b", context={},
        bypass_cooldown=True,
    )
    assert bypassed is not None
    # Two successful emits == two Telegram POSTs (first + bypassed).
    assert len(http.calls) == 2


# ---------- plain-text rendering ----------


@pytest.mark.asyncio
async def test_telegram_plain_text_preserves_special_chars(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """Body contains Markdown / HTML special characters; design §2.E.4
    forbids parse_mode so they MUST land verbatim on the wire."""
    http = _RecordingHttpClient()
    svc = _build_service(redis_svc, broadcast, http_client=http)
    body = "P&L total *$50_000* (with [brackets] and <tags>)"
    await svc.emit(
        alert_type="hedge_closed",
        cooldown_key="ord_chars",
        title_vi="t", body_vi=body, context={},
    )
    sent = http.calls[0]["json"]["text"]
    assert body in sent
    assert "parse_mode" not in http.calls[0]["json"]


# ---------- cooldown=0 types fire every time without bypass ----------


@pytest.mark.asyncio
async def test_cooldown_zero_types_skip_set_nx_ex_gate(
    redis_client: fakeredis.aioredis.FakeRedis,
    redis_svc: RedisService,
    broadcast: _CapturingBroadcast,
) -> None:
    """``hedge_closed`` carries ``cooldown_seconds=0`` so two emits with
    the same cooldown_key both fire (the SET NX EX gate is skipped).
    Upstream is responsible for one-shot semantics."""
    http = _RecordingHttpClient()
    svc = _build_service(redis_svc, broadcast, http_client=http)
    a = await svc.emit(
        alert_type="hedge_closed", cooldown_key="ord_same",
        title_vi="t", body_vi="b", context={},
    )
    b = await svc.emit(
        alert_type="hedge_closed", cooldown_key="ord_same",
        title_vi="t", body_vi="b", context={},
    )
    assert a is not None
    assert b is not None
    assert a != b
    # No cooldown key was created.
    cd_keys = [
        k async for k in redis_client.scan_iter(
            match="alert_cooldown:hedge_closed:*"
        )
    ]
    assert cd_keys == []
