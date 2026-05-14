"""One-shot migration: extract FTMO portion from symbol_mapping_ftmo_exness.json
into ``server/data/ftmo_whitelist.json`` per ``docs/phase-4-symbol-mapping-design.md``
§2.1 (D-SM-09).

Drops Exness-side fields (``exness``, ``exness_trade_contract_size``,
``exness_pip_size``, ``exness_pip_value``, ``match_type``). Preserves FTMO
fields plus derives ``asset_class`` from name pattern.

Usage:
    python scripts/migrate_extract_ftmo_whitelist.py
    python scripts/migrate_extract_ftmo_whitelist.py --input <path> --output <path>
    python scripts/migrate_extract_ftmo_whitelist.py --dry-run

Idempotent — re-running on the same input produces byte-identical output.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Repo-root defaults so the script "just works" when run from the repo root.
DEFAULT_INPUT = "symbol_mapping_ftmo_exness.json"
DEFAULT_OUTPUT = "server/data/ftmo_whitelist.json"

# Asset-class derivation heuristics. Order matters: most-specific patterns first.
_METALS_PREFIXES = ("XAU", "XAG", "XPT", "XPD")
_CRYPTO_PATTERN = re.compile(
    r"^(BTC|ETH|XRP|LTC|BCH|ADA|DOGE|DOT|LINK|SOL|AVAX|MATIC|UNI|ATOM|TRX|ETC|XLM|FIL)"
    r"(USD|USDT|BTC)$",
    re.IGNORECASE,
)
_FOREX_PATTERN = re.compile(r"^[A-Z]{6}$")
_ENERGIES_NAMES = {"UKOIL", "USOIL", "NATGAS"}
_INDICES_SUFFIX = ".cash"


def _derive_asset_class(name: str, ftmo_units_per_lot: float) -> str:
    """Map a symbol name + lot size to an asset class.

    Heuristics:
      - Metals via XAU/XAG/XPT/XPD prefix.
      - Crypto via known ticker + USD/USDT/BTC suffix.
      - Energies via known commodity names (UKOIL, USOIL, NATGAS) — checked
        BEFORE indices because they wear ``.cash`` like indices but are
        commodities semantically.
      - Indices via ``.cash`` suffix (FTMO convention for index CFDs).
      - Forex via 6-char uppercase pattern.
      - Everything else → stocks (FTMO trades AAPL/AMD/AMZN with
        units_per_lot=1 — common stock-CFD convention).
    """
    upper = name.upper()
    has_cash_suffix = upper.endswith(_INDICES_SUFFIX.upper())
    core = upper.removesuffix(_INDICES_SUFFIX.upper())

    if any(core.startswith(p) for p in _METALS_PREFIXES):
        return "metals"
    if _CRYPTO_PATTERN.match(core):
        return "crypto"
    if core in _ENERGIES_NAMES:
        return "energies"
    if has_cash_suffix:
        return "indices"
    if _FOREX_PATTERN.match(core):
        return "forex"
    return "stocks"


def _extract_entry(raw: dict[str, Any]) -> dict[str, Any]:
    """Build a single FTMOSymbol-shaped dict from an old-format mapping row."""
    name = raw["ftmo"]
    ftmo_units = float(raw["ftmo_units_per_lot"])
    ftmo_pip_size = float(raw["ftmo_pip_size"])
    ftmo_pip_value = float(raw["ftmo_pip_value"])
    quote_ccy = raw.get("quote_ccy")
    if not quote_ccy:
        logger.warning("entry %s missing quote_ccy; defaulting to USD", name)
        quote_ccy = "USD"
    return {
        "name": name,
        "asset_class": _derive_asset_class(name, ftmo_units),
        "quote_ccy": quote_ccy,
        "ftmo_units_per_lot": ftmo_units,
        "ftmo_pip_size": ftmo_pip_size,
        "ftmo_pip_value": ftmo_pip_value,
    }


def migrate(input_path: Path, output_path: Path, dry_run: bool) -> int:
    if not input_path.is_file():
        logger.error("input file not found: %s", input_path)
        return 1
    raw = json.loads(input_path.read_text(encoding="utf-8"))
    if "version" not in raw or "mappings" not in raw:
        logger.error(
            "input %s is missing required keys (version / mappings); not the "
            "expected schema",
            input_path,
        )
        return 1

    symbols: list[dict[str, Any]] = []
    for entry in raw["mappings"]:
        try:
            symbols.append(_extract_entry(entry))
        except KeyError as e:
            logger.error("entry missing field %s: %r", e, entry)
            return 1

    # Deterministic output: sort alphabetically by name (matches design
    # criterion 4 in step 4.A.1 acceptance).
    symbols.sort(key=lambda s: s["name"])

    out: dict[str, Any] = {
        "schema_version": 1,
        "version": int(raw["version"]),
        "symbols": symbols,
    }

    breakdown = Counter(s["asset_class"] for s in symbols)
    logger.info(
        "migrated %d entries from %s",
        len(symbols),
        input_path,
    )
    for cls, n in sorted(breakdown.items()):
        logger.info("  %s: %d", cls, n)

    if dry_run:
        logger.info("dry-run: would write %s", output_path)
        return 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(out, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    logger.info("wrote %s", output_path)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", default=DEFAULT_INPUT, help="source JSON")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="target JSON")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="parse + log breakdown but do not write output",
    )
    args = parser.parse_args()
    return migrate(Path(args.input), Path(args.output), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
