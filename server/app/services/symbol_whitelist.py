"""Symbol whitelist service: cached in-process accessor over the JSON mapping file."""

from __future__ import annotations

from hedger_shared.symbol_mapping import (
    SymbolMapping,
    SymbolMappingFile,
    build_ftmo_index,
    load_symbol_mapping,
)

_mapping_file: SymbolMappingFile | None = None
_index: dict[str, SymbolMapping] = {}


def load_whitelist(path: str) -> None:
    """Load the symbol mapping JSON and cache parsed file + lookup index.

    Idempotent: a subsequent call replaces the cached state.
    """
    global _mapping_file, _index
    parsed = load_symbol_mapping(path)
    _mapping_file = parsed
    _index = build_ftmo_index(parsed)


def is_loaded() -> bool:
    """Return True once `load_whitelist` has populated the cache."""
    return _mapping_file is not None


def get_all_symbols() -> list[str]:
    """Return the sorted list of FTMO symbol names from the cached whitelist."""
    return sorted(_index.keys())


def get_symbol_mapping(ftmo_symbol: str) -> SymbolMapping | None:
    """Return the SymbolMapping for an FTMO symbol, or None when not in the whitelist."""
    return _index.get(ftmo_symbol)
