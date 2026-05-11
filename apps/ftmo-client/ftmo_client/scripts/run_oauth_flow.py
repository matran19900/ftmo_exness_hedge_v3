"""One-shot OAuth flow CLI for the FTMO trading client.

CEO runs this once per FTMO account before starting the client process.
It:

1. Builds the cTrader consent URL via ``hedger_shared.ctrader_oauth``.
2. Prints the URL and opens a local HTTP server bound to the
   ``CTRADER_REDIRECT_URI`` host/port for the consent callback.
3. CEO opens the URL in a browser, grants access; cTrader redirects
   back to the local server with ``?code=...``.
4. The script exchanges the code for tokens, fetches the trading-account
   list, picks the live account that matches what the operator
   pointed to via ``--account-id`` (or the first live account if no
   match — operator confirms in stdout), and saves the token to
   ``ctrader:ftmo:{account_id}:creds``.

Usage::

    python -m ftmo_client.scripts.run_oauth_flow --account-id ftmo_acc_001

Optionally ``--port`` to bind a different local port (must match
``CTRADER_REDIRECT_URI`` in ``.env``).
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import logging
import sys
import threading
import urllib.parse
from typing import TextIO
from urllib.parse import urlparse

import redis.asyncio as redis_asyncio
from hedger_shared.ctrader_oauth import (
    build_authorization_url,
    exchange_code_for_token,
    fetch_trading_accounts,
)

from ftmo_client.config import FtmoClientSettings
from ftmo_client.oauth_storage import save_token

logger = logging.getLogger(__name__)


EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VALIDATION = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_oauth_flow",
        description=(
            "Run the cTrader OAuth consent flow once and save the "
            "resulting token to Redis under "
            "ctrader:ftmo:{account-id}:creds."
        ),
    )
    parser.add_argument(
        "--account-id",
        required=True,
        help="FTMO account_id (the local key in Redis, ^[a-z0-9_]{3,64}$).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=("Local callback port. Defaults to the port in CTRADER_REDIRECT_URI from .env."),
    )
    return parser


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Minimal handler that captures ``?code=`` and stores it on the server."""

    def do_GET(self) -> None:
        query = urllib.parse.parse_qs(urlparse(self.path).query)
        code = query.get("code", [None])[0]
        # Stash the code on the server instance so the main thread can
        # pick it up after server.shutdown().
        if code:
            self.server.received_code = code  # type: ignore[attr-defined]
            body = (
                b"<html><body>"
                b"<h2>cTrader consent received.</h2>"
                b"<p>You can close this tab. The CLI will continue.</p>"
                b"</body></html>"
            )
            self.send_response(200)
        else:
            error = query.get("error", ["unknown"])[0]
            self.server.received_error = error  # type: ignore[attr-defined]
            body = b"<html><body><h2>OAuth failed</h2><p>Check the CLI output.</p></body></html>"
            self.send_response(400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        # Silence the default stderr access log; we have our own logger.
        return


def _wait_for_callback(host: str, port: int, out: TextIO) -> str | None:
    """Spin up an HTTP server, block until ``?code=`` arrives, return it.

    Returns None on user-aborted flow (Ctrl+C) or HTTP error response.
    """
    server = http.server.HTTPServer((host, port), _CallbackHandler)
    server.received_code = None  # type: ignore[attr-defined]
    server.received_error = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(
        f"Local callback server listening on http://{host}:{port}/callback. "
        "Waiting for cTrader redirect...",
        file=out,
    )
    try:
        # Poll the captured code; the handler sets it on first request.
        while True:
            if server.received_code:  # type: ignore[attr-defined]
                return str(server.received_code)  # type: ignore[attr-defined]
            if server.received_error:  # type: ignore[attr-defined]
                return None
            thread.join(timeout=0.5)
    except KeyboardInterrupt:
        return None
    finally:
        server.shutdown()
        server.server_close()


async def _amain(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    try:
        settings = FtmoClientSettings()  # type: ignore[call-arg]
    except Exception as exc:
        print(f"error: failed to load settings: {exc}", file=err)
        return EXIT_VALIDATION

    if not settings.ctrader_client_id or not settings.ctrader_client_secret:
        print(
            "error: CTRADER_CLIENT_ID + CTRADER_CLIENT_SECRET must be set in .env",
            file=err,
        )
        return EXIT_VALIDATION

    redirect_uri = settings.ctrader_redirect_uri
    parsed = urlparse(redirect_uri)
    bind_host = parsed.hostname or "localhost"
    bind_port = args.port or parsed.port or 8765

    auth_url = build_authorization_url(
        client_id=settings.ctrader_client_id,
        redirect_uri=redirect_uri,
    )
    print("Open this URL in a browser and grant access:", file=out)
    print(f"  {auth_url}", file=out)
    print("", file=out)

    # The redirect lands on bind_host:bind_port/callback; the URL is
    # what cTrader was registered to send the user to.
    code = _wait_for_callback(bind_host, bind_port, out)
    if code is None:
        print("error: did not receive an authorization code", file=err)
        return EXIT_ERROR

    print("Got authorization code; exchanging for token...", file=out)
    try:
        token = await exchange_code_for_token(
            client_id=settings.ctrader_client_id,
            client_secret=settings.ctrader_client_secret,
            code=code,
            redirect_uri=redirect_uri,
        )
    except RuntimeError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_ERROR

    try:
        accounts = await fetch_trading_accounts(token["access_token"])
    except RuntimeError as exc:
        print(f"error: {exc}", file=err)
        return EXIT_ERROR

    if not accounts:
        print(
            "error: no trading accounts associated with this user",
            file=err,
        )
        return EXIT_ERROR

    # FTMO client wants the live account. Operator confirms in stdout.
    live_accounts = [a for a in accounts if a.get("live", a.get("isLive", False))]
    chosen = live_accounts[0] if live_accounts else accounts[0]
    raw_ctid = chosen.get("accountId") or chosen.get("ctidTraderAccountId")
    if raw_ctid is None:
        print(
            f"error: cTrader account payload missing accountId field: {chosen}",
            file=err,
        )
        return EXIT_ERROR
    ctid_trader_account_id = int(raw_ctid)

    redis: redis_asyncio.Redis = redis_asyncio.from_url(  # type: ignore[no-untyped-call]
        settings.redis_url, decode_responses=True, max_connections=2
    )
    try:
        await redis.ping()
        await save_token(
            redis,
            args.account_id,
            token,
            ctid_trader_account_id=ctid_trader_account_id,
        )
    finally:
        await redis.aclose()

    print(
        f"OK Token saved: ctrader:ftmo:{args.account_id}:creds "
        f"(ctid_trader_account_id={ctid_trader_account_id}, "
        f"expires_in={token['expires_in']}s)",
        file=out,
    )
    return EXIT_OK


def main(
    argv: list[str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return asyncio.run(_amain(args, out or sys.stdout, err or sys.stderr))


if __name__ == "__main__":
    sys.exit(main())
