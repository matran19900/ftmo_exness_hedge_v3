"""Volume calculator implementing R6 + R15 + R13/R17 + R11.

R6 sizes the primary leg from a USD risk amount. R7/R15 derives the secondary
leg from the primary using the FTMO/Exness contract-size ratio and a user
``ratio`` multiplier.

Phase 4.A.5 (D-4.A.0-4): ``calculate_volume`` now consumes the split type
parameters ``ftmo_symbol`` (FTMOSymbol) + ``exness_mapping`` (MappingEntry)
instead of the legacy ``SymbolMapping``. The caller is responsible for
resolving these via ``MappingService.get_pair_mapping`` so the calculator
itself stays pure-function.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.services.ftmo_whitelist_service import FTMOSymbol
from app.services.mapping_cache_schemas import MappingEntry

logger = logging.getLogger(__name__)

#: Default minimum SL distance, in pips. R17 (Phase 4 makes this configurable
#: via app:settings; for Phase 2 we hardcode the documented default).
MIN_SL_PIPS_DEFAULT = 5.0


def calculate_volume(
    *,
    risk_amount: float,
    entry: float,
    sl: float,
    symbol_config: dict[str, str],
    ftmo_symbol: FTMOSymbol,
    exness_mapping: MappingEntry,
    ratio: float = 1.0,
    quote_to_usd_rate: float,
    min_sl_pips: float = MIN_SL_PIPS_DEFAULT,
) -> dict[str, Any]:
    """Return primary + secondary volumes plus debug breakdown.

    Raises :class:`ValueError` for any input that violates R11/R13/R17 or for
    obviously bad numerics (zero/negative entry, sl, risk, ratio, rate).
    """
    if entry <= 0 or sl <= 0:
        raise ValueError(f"entry and sl must be positive, got entry={entry}, sl={sl}")
    if entry == sl:
        raise ValueError("entry must differ from sl")
    if risk_amount <= 0:
        raise ValueError(f"risk_amount must be positive, got {risk_amount}")
    if quote_to_usd_rate <= 0:
        raise ValueError(f"quote_to_usd_rate must be positive, got {quote_to_usd_rate}")
    if ratio <= 0:
        raise ValueError(f"ratio must be positive, got {ratio}")

    pip_size = float(ftmo_symbol.ftmo_pip_size)
    ftmo_contract_size = float(ftmo_symbol.ftmo_units_per_lot)
    exness_contract_size = float(exness_mapping.contract_size)

    # R6 — sl_pips and the per-lot pip value.
    sl_pips = abs(entry - sl) / pip_size
    if sl_pips < min_sl_pips:
        raise ValueError(f"SL too tight: {sl_pips:.2f} pips < min {min_sl_pips}")

    pip_value_quote_per_lot = pip_size * ftmo_contract_size
    pip_value_usd_per_lot = pip_value_quote_per_lot * quote_to_usd_rate
    sl_usd_per_lot = sl_pips * pip_value_usd_per_lot
    if sl_usd_per_lot <= 0:
        # Defensive: would only trigger if pip math collapsed to 0.
        raise ValueError(
            f"sl_usd_per_lot resolved to {sl_usd_per_lot}; check pip_size / contract_size"
        )

    volume_p_raw = risk_amount / sl_usd_per_lot

    # R11 — clamp to broker min/max and round DOWN to step. cTrader's
    # symbol_config stores volume fields as integer base-units (e.g. 100 means
    # 0.01 lot for FX given a lot_size of 10000000). Convert to lot-fractions
    # using the lot_size.
    min_units = _safe_float(symbol_config.get("min_volume"), default=0.0)
    max_units = _safe_float(symbol_config.get("max_volume"), default=math.inf)
    step_units = _safe_float(symbol_config.get("step_volume"), default=0.0)
    lot_size_units = _safe_float(symbol_config.get("lot_size"), default=1.0)
    if lot_size_units <= 0:
        lot_size_units = 1.0

    min_vol_lot = min_units / lot_size_units if min_units > 0 else 0.0
    max_vol_lot = max_units / lot_size_units if math.isfinite(max_units) else math.inf
    step_vol_lot = step_units / lot_size_units if step_units > 0 else 0.0

    volume_p = _clamp_round(volume_p_raw, min_vol_lot, max_vol_lot, step_vol_lot)

    # R7 — secondary leg.
    if exness_contract_size <= 0:
        raise ValueError(f"exness_contract_size must be positive, got {exness_contract_size}")
    volume_s_raw = volume_p * ratio * (ftmo_contract_size / exness_contract_size)
    # TODO Phase 4: use exness_symbol_config (broker-side min/max/step) once
    # the Exness client populates ``symbol_config:exness:{sym}``.
    volume_s = _clamp_round(volume_s_raw, min_vol_lot, max_vol_lot, step_vol_lot)

    return {
        "volume_primary": round(volume_p, 4),
        "volume_secondary": round(volume_s, 4),
        "sl_pips": round(sl_pips, 2),
        "pip_value_usd_per_lot": round(pip_value_usd_per_lot, 6),
        "sl_usd_per_lot": round(sl_usd_per_lot, 4),
        "volume_primary_raw": volume_p_raw,
        "volume_secondary_raw": volume_s_raw,
    }


def _clamp_round(value: float, min_v: float, max_v: float, step: float) -> float:
    """Round DOWN to ``step``, then clamp into ``[min_v, max_v]``."""
    if step > 0:
        rounded = math.floor(value / step) * step
    else:
        rounded = value
    if rounded < min_v:
        return min_v
    if rounded > max_v:
        return max_v
    return rounded


def _safe_float(raw: object, *, default: float) -> float:
    """Best-effort float parse from a Redis hash value (always returned as str)."""
    if raw is None:
        return default
    try:
        return float(str(raw))
    except (TypeError, ValueError):
        return default
