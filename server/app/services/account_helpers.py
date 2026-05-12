"""Shared helpers for account row → typed entry conversion (step 3.13a).

Both step 3.12's REST ``GET /api/accounts`` and ``account_status_loop``
broadcast need to map ``redis_svc.get_all_accounts_with_status()`` rows
(plain ``dict[str, str]``) to typed ``AccountStatusEntry`` instances.

Pre-3.13a only the REST endpoint did the typed conversion (via a
private ``_row_to_entry`` in ``accounts.py``); the WS loop shipped the
raw row dicts. That meant the WS payload's ``enabled`` field arrived
as a JSON string ``"true"`` / ``"false"`` and JavaScript's
``Boolean("false") === true`` evaluation made disabled accounts render
as if they were still enabled. Step 3.13a hoists the conversion here
so both code paths produce identical shapes.

Import direction (no cycles): ``app.services.account_helpers`` imports
``AccountStatusEntry`` from ``app.api.accounts``. Both
``app.api.accounts`` (REST) and ``app.services.account_status`` (loop)
import ``row_to_entry`` from here. ``app.api.accounts`` does NOT import
from ``app.services.account_status``; no cycle exists.
"""

from __future__ import annotations

from typing import Literal, cast

from app.api.accounts import AccountStatusEntry


def row_to_entry(row: dict[str, str]) -> AccountStatusEntry:
    """Convert a HASH-string row to a typed ``AccountStatusEntry``.

    Field-level details:
      - ``enabled``: parsed ``"true"`` → ``True``, anything else → ``False``.
        Matches the convention in ``RedisService.add_account`` /
        ``update_account_meta`` which write the lowercase literal.
      - ``broker`` + ``status``: cast-narrowed to their ``Literal`` types.
        The upstream ``get_all_accounts_with_status`` only emits values
        from the documented set, so the cast is safe at runtime;
        Pydantic re-validates at construction time anyway.
      - Money fields stay as strings — D-108 scaling happens at the
        frontend render boundary.
    """
    return AccountStatusEntry(
        broker=cast(Literal["ftmo", "exness"], row["broker"]),
        account_id=row["account_id"],
        name=row["name"],
        enabled=row["enabled"] == "true",
        status=cast(Literal["online", "offline", "disabled"], row["status"]),
        balance_raw=row["balance_raw"],
        equity_raw=row["equity_raw"],
        margin_raw=row["margin_raw"],
        free_margin_raw=row["free_margin_raw"],
        currency=row["currency"],
        money_digits=row["money_digits"],
    )
