"""Operational CLI for managing FTMO / Exness accounts in Redis.

CEO runs this from the project root. The script writes directly to the
Redis instance configured in ``.env`` via ``app.config.Settings``; it does
NOT go through any FastAPI endpoint (those land in Phase 4). Server must
be restarted after ``add`` / ``remove`` for ``setup_consumer_groups()`` to
pick up the change — runtime account management is also Phase 4.

Usage (run from repository root)::

    python -m scripts.init_account add --broker ftmo --account-id ftmo_acc_001 --name "FTMO 100k"
    python -m scripts.init_account add --broker exness --account-id exn_001 --name "Exness Live" --enabled true
    python -m scripts.init_account list
    python -m scripts.init_account list --broker ftmo
    python -m scripts.init_account remove --broker ftmo --account-id ftmo_acc_001 --yes

``account-id`` must match ``^[a-z0-9_]{3,64}$`` (lowercase alphanum +
underscore, 3–64 chars). ``broker`` must be ``ftmo`` or ``exness``.

Exit codes:
    0  success
    1  unexpected error (Redis unreachable etc.)
    2  validation error (bad input, duplicate add, missing --yes, etc.)
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from typing import TextIO

import redis.asyncio as redis_asyncio

# Lazy-import the server package so this script runs without launching FastAPI.
from app.config import get_settings
from app.services.redis_service import RedisService

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VALIDATION = 2

_VALID_BROKERS = ("ftmo", "exness")
_ACCOUNT_ID_RE = re.compile(r"^[a-z0-9_]{3,64}$")


def _validate_inputs(broker: str, account_id: str | None, err: TextIO) -> int | None:
    """Return an exit code on validation failure, None on success.

    Mirrors the regex enforced inside ``RedisService.add_account`` so the
    CLI fails fast with a friendly message instead of letting the
    ValueError bubble through asyncio.run.
    """
    if broker not in _VALID_BROKERS:
        print(
            f"error: --broker must be one of {_VALID_BROKERS!r}, got {broker!r}",
            file=err,
        )
        return EXIT_VALIDATION
    if account_id is not None and not _ACCOUNT_ID_RE.match(account_id):
        print(
            f"error: --account-id {account_id!r} must match "
            f"{_ACCOUNT_ID_RE.pattern} (lowercase alphanum + underscore, 3–64 chars)",
            file=err,
        )
        return EXIT_VALIDATION
    return None


async def _open_service() -> tuple[RedisService, redis_asyncio.Redis]:
    """Build a RedisService bound to a fresh connection from Settings.

    Returns the redis client too so the caller can ``aclose()`` it in a
    ``finally`` block — Settings construction would re-read .env on each
    call, so we want one explicit lifecycle here.
    """
    settings = get_settings()
    # redis-py's ``from_url`` lacks complete type stubs in the bundled
    # version; the call is untyped to mypy strict. Mirror the cast used
    # elsewhere in the project (see app/redis_client.py for the runtime
    # shape of the returned client).
    client: redis_asyncio.Redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url, decode_responses=True, max_connections=4
    )
    # Surface connectivity issues early — without this, the first command
    # call would raise a less obvious ConnectionError downstream.
    await client.ping()
    return RedisService(client), client


async def _cmd_add(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    code = _validate_inputs(args.broker, args.account_id, err)
    if code is not None:
        return code

    svc, client = await _open_service()
    try:
        existing = await svc.get_account_meta(args.broker, args.account_id)
        if existing is not None:
            print(
                f"error: account {args.broker}/{args.account_id} already exists "
                f"(name={existing.get('name', '')!r}); "
                f"remove it first or pick a different account-id",
                file=err,
            )
            return EXIT_VALIDATION

        await svc.add_account(
            args.broker, args.account_id, args.name, enabled=args.enabled
        )
    finally:
        await client.aclose()

    enabled_str = "true" if args.enabled else "false"
    print(
        f"OK Account added: {args.broker} / {args.account_id} "
        f"(enabled={enabled_str})",
        file=out,
    )
    print(f"  Meta key: account_meta:{args.broker}:{args.account_id}", file=out)
    print(
        "-> Restart server now so setup_consumer_groups() picks up this new account.",
        file=out,
    )
    return EXIT_OK


async def _cmd_remove(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    code = _validate_inputs(args.broker, args.account_id, err)
    if code is not None:
        return code

    svc, client = await _open_service()
    try:
        existing = await svc.get_account_meta(args.broker, args.account_id)
        if existing is None:
            print(
                f"error: account {args.broker}/{args.account_id} does not exist",
                file=err,
            )
            return EXIT_VALIDATION

        if not args.yes:
            # Dry-run preview. Counts mirror what remove_account drops:
            # 1 SET membership entry + 1 meta hash + 1 heartbeat key (if present).
            print(
                f"Would remove: {args.broker} / {args.account_id} "
                "(set membership + meta hash + heartbeat key)",
                file=out,
            )
            print("Pass --yes to confirm.", file=out)
            return EXIT_VALIDATION

        await svc.remove_account(args.broker, args.account_id)
    finally:
        await client.aclose()

    print(f"OK Account removed: {args.broker} / {args.account_id}", file=out)
    print(
        "  Note: existing orders referencing this account are NOT deleted "
        "(out of scope per docs).",
        file=out,
    )
    return EXIT_OK


async def _cmd_list(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    if args.broker is not None and args.broker not in _VALID_BROKERS:
        print(
            f"error: --broker must be one of {_VALID_BROKERS!r}, got {args.broker!r}",
            file=err,
        )
        return EXIT_VALIDATION

    svc, client = await _open_service()
    try:
        brokers = (args.broker,) if args.broker else _VALID_BROKERS
        for broker in brokers:
            ids = await svc.get_all_account_ids(broker)
            print(f"== {broker} ({len(ids)} accounts) ==", file=out)
            for acc_id in ids:
                meta = await svc.get_account_meta(broker, acc_id) or {}
                status = await svc.get_client_status(broker, acc_id)
                name = meta.get("name", "")
                enabled = meta.get("enabled", "true")
                print(
                    f'  {acc_id}  "{name}"  enabled={enabled}  status={status}',
                    file=out,
                )
    finally:
        await client.aclose()
    return EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="init_account",
        description="Manage FTMO / Exness account registrations in Redis.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Register a new account.")
    p_add.add_argument("--broker", required=True, choices=_VALID_BROKERS)
    p_add.add_argument("--account-id", required=True)
    p_add.add_argument("--name", required=True)
    p_add.add_argument(
        "--enabled",
        type=lambda v: v.lower() == "true",
        default=True,
        help="true|false (default: true)",
    )

    p_rm = sub.add_parser("remove", help="Remove an account (preview unless --yes).")
    p_rm.add_argument("--broker", required=True, choices=_VALID_BROKERS)
    p_rm.add_argument("--account-id", required=True)
    p_rm.add_argument(
        "--yes",
        action="store_true",
        help="Confirm removal; without this the call is a dry-run preview.",
    )

    p_ls = sub.add_parser("list", help="List registered accounts (both brokers by default).")
    p_ls.add_argument(
        "--broker",
        choices=_VALID_BROKERS,
        default=None,
        help="Filter to a single broker.",
    )

    return parser


async def _dispatch(
    args: argparse.Namespace, out: TextIO, err: TextIO
) -> int:
    """Route to the right async handler. Catches connection errors uniformly."""
    try:
        if args.command == "add":
            return await _cmd_add(args, out, err)
        if args.command == "remove":
            return await _cmd_remove(args, out, err)
        if args.command == "list":
            return await _cmd_list(args, out, err)
        # argparse `required=True` on subparsers prevents this branch in
        # practice, but keep the guard so mypy sees a definitive return.
        print(f"error: unknown command {args.command!r}", file=err)
        return EXIT_VALIDATION
    except Exception as exc:  # noqa: BLE001  — top-level CLI catch-all
        print(f"error: {exc.__class__.__name__}: {exc}", file=err)
        return EXIT_ERROR


def main(
    argv: list[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Entry point. Returns the process exit code.

    Streams default to ``sys.stdout`` / ``sys.stderr`` for normal CLI use;
    tests inject ``io.StringIO`` to capture output.
    """
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_dispatch(args, out or sys.stdout, err or sys.stderr))


if __name__ == "__main__":
    sys.exit(main())
