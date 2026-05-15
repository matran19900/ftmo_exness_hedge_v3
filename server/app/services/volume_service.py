"""Phase 4 secondary leg volume calculation (step 4.7a).

Spec: docs/phase-4-design.md §1.D. Formula locked — changes require a
D-XXX decision.

    raw_lots = primary_volume_lots
             * (ftmo_units_per_lot / exness_contract_size)
             * risk_ratio
    secondary_volume_lots = floor(raw_lots / volume_step) * volume_step

Rounding is asymmetric (DOWN, never up): rounding UP could exceed the
broker's max_volume; rounding DOWN may fall below min_volume and raise
``SecondaryVolumeTooSmall``. Explicit failure beats silent mis-sizing.

E1/E2 errors propagate up to ``OrderService.create_order``, which maps
them to 400 ``secondary_volume_too_small`` / ``secondary_volume_too_large``
BEFORE the primary FTMO command is pushed — so a hedge that cannot be
sized correctly never opens a single leg.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SecondaryVolumeTooSmall(Exception):
    """Raised when the rounded secondary volume is below broker min_volume."""

    primary: float
    computed: float
    rounded: float
    exness_volume_min: float

    def __str__(self) -> str:
        return (
            f"secondary volume {self.rounded} < min {self.exness_volume_min} "
            f"(primary {self.primary} -> raw {self.computed})"
        )


@dataclass
class SecondaryVolumeTooLarge(Exception):
    """Raised when the rounded secondary volume exceeds broker max_volume."""

    primary: float
    computed: float
    rounded: float
    exness_volume_max: float

    def __str__(self) -> str:
        return (
            f"secondary volume {self.rounded} > max {self.exness_volume_max} "
            f"(primary {self.primary} -> raw {self.computed})"
        )


def round_to_step(value: float, step: float) -> float:
    """Round ``value`` DOWN to the nearest multiple of ``step``.

    Asymmetric on purpose — see module docstring.
    """
    if step <= 0:
        raise ValueError(f"volume_step must be positive, got {step}")
    # Use math.floor on the ratio to handle float-quirky cases like
    # 0.30000000000000004 / 0.01 = 30.000000000000004.
    return math.floor(value / step + 1e-9) * step


def compute_secondary_volume(
    primary_volume_lots: float,
    ftmo_units_per_lot: float,
    exness_contract_size: float,
    risk_ratio: float,
    exness_volume_step: float,
    exness_volume_min: float,
    exness_volume_max: float,
) -> float:
    """Compute the Exness leg's lot size from the FTMO primary.

    ``ftmo_units_per_lot`` and ``exness_contract_size`` are taken from the
    FTMO whitelist and the Exness mapping cache respectively (both pulled
    via ``MappingService.get_pair_mapping``). ``risk_ratio`` comes from
    ``pair.ratio`` (1.0 = same notional both sides).

    Raises ``ValueError`` on non-positive inputs; ``SecondaryVolumeTooSmall``
    / ``SecondaryVolumeTooLarge`` on bound violations.
    """
    if primary_volume_lots <= 0:
        raise ValueError(
            f"primary_volume_lots must be positive, got {primary_volume_lots}"
        )
    if ftmo_units_per_lot <= 0:
        raise ValueError(
            f"ftmo_units_per_lot must be positive, got {ftmo_units_per_lot}"
        )
    if exness_contract_size <= 0:
        raise ValueError(
            f"exness_contract_size must be positive, got {exness_contract_size}"
        )
    if risk_ratio <= 0:
        raise ValueError(f"risk_ratio must be positive, got {risk_ratio}")

    raw = (
        primary_volume_lots
        * (ftmo_units_per_lot / exness_contract_size)
        * risk_ratio
    )
    rounded = round_to_step(raw, exness_volume_step)

    if rounded < exness_volume_min:
        raise SecondaryVolumeTooSmall(
            primary=primary_volume_lots,
            computed=raw,
            rounded=rounded,
            exness_volume_min=exness_volume_min,
        )
    if rounded > exness_volume_max:
        raise SecondaryVolumeTooLarge(
            primary=primary_volume_lots,
            computed=raw,
            rounded=rounded,
            exness_volume_max=exness_volume_max,
        )
    return rounded
