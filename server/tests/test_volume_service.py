"""Step 4.7a — secondary-leg volume formula tests (volume_service.py).

Locked formula per docs/phase-4-design.md §1.D:

    raw    = primary_lots * (ftmo_units_per_lot / exness_contract_size) * ratio
    rounded = floor(raw / step) * step   # asymmetric DOWN

Failure modes:
    - rounded < min  -> SecondaryVolumeTooSmall (E1)
    - rounded > max  -> SecondaryVolumeTooLarge (E2)
    - any non-positive input -> ValueError

Worked example (FTMO Standard -> Exness Standard, both 100k contract):
    primary=0.10 lots, units_per_lot=100_000, contract_size=100_000,
    ratio=1.0, step=0.01, min=0.01, max=200.0
    raw = 0.10 * (100_000 / 100_000) * 1.0 = 0.10
    rounded = 0.10 (already a multiple of step)
"""

from __future__ import annotations

import pytest
from app.services.volume_service import (
    SecondaryVolumeTooLarge,
    SecondaryVolumeTooSmall,
    compute_secondary_volume,
    round_to_step,
)

# ---------- happy path ----------


def test_compute_secondary_volume_worked_example_one_to_one() -> None:
    """Standard FTMO to Standard Exness, 0.10 -> 0.10 lots."""
    result = compute_secondary_volume(
        primary_volume_lots=0.10,
        ftmo_units_per_lot=100_000,
        exness_contract_size=100_000,
        risk_ratio=1.0,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    assert result == pytest.approx(0.10)


def test_compute_secondary_volume_cent_account_100x_scaling() -> None:
    """Cent broker (contract_size=1000) → 100× more lots for same notional."""
    result = compute_secondary_volume(
        primary_volume_lots=0.10,
        ftmo_units_per_lot=100_000,
        exness_contract_size=1_000,
        risk_ratio=1.0,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    # 0.10 * (100_000 / 1_000) = 10.0
    assert result == pytest.approx(10.0)


def test_compute_secondary_volume_risk_ratio_double() -> None:
    """risk_ratio=2.0 doubles secondary notional."""
    result = compute_secondary_volume(
        primary_volume_lots=0.10,
        ftmo_units_per_lot=100_000,
        exness_contract_size=100_000,
        risk_ratio=2.0,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    assert result == pytest.approx(0.20)


def test_compute_secondary_volume_risk_ratio_half() -> None:
    """risk_ratio=0.5 halves secondary notional."""
    result = compute_secondary_volume(
        primary_volume_lots=0.10,
        ftmo_units_per_lot=100_000,
        exness_contract_size=100_000,
        risk_ratio=0.5,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    assert result == pytest.approx(0.05)


def test_compute_secondary_volume_rounds_down_to_step() -> None:
    """raw=0.103, step=0.01 -> rounded=0.10 (DOWN, not 0.11)."""
    result = compute_secondary_volume(
        primary_volume_lots=0.103,
        ftmo_units_per_lot=100_000,
        exness_contract_size=100_000,
        risk_ratio=1.0,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    assert result == pytest.approx(0.10)


# ---------- E1 / E2 boundaries ----------


def test_compute_secondary_volume_too_small_raises_E1() -> None:
    """raw rounded to 0.0 < min=0.01 -> SecondaryVolumeTooSmall."""
    with pytest.raises(SecondaryVolumeTooSmall) as exc:
        compute_secondary_volume(
            primary_volume_lots=0.001,  # 0.001 * 1 * 1 = 0.001 -> floor 0.0
            ftmo_units_per_lot=100_000,
            exness_contract_size=100_000,
            risk_ratio=1.0,
            exness_volume_step=0.01,
            exness_volume_min=0.01,
            exness_volume_max=200.0,
        )
    assert exc.value.rounded < exc.value.exness_volume_min


def test_compute_secondary_volume_too_large_raises_E2() -> None:
    """raw exceeds max -> SecondaryVolumeTooLarge."""
    with pytest.raises(SecondaryVolumeTooLarge) as exc:
        compute_secondary_volume(
            primary_volume_lots=500.0,
            ftmo_units_per_lot=100_000,
            exness_contract_size=100_000,
            risk_ratio=1.0,
            exness_volume_step=0.01,
            exness_volume_min=0.01,
            exness_volume_max=200.0,
        )
    assert exc.value.rounded > exc.value.exness_volume_max


def test_compute_secondary_volume_exactly_min_passes() -> None:
    """Boundary: rounded == min is acceptable."""
    result = compute_secondary_volume(
        primary_volume_lots=0.01,
        ftmo_units_per_lot=100_000,
        exness_contract_size=100_000,
        risk_ratio=1.0,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    assert result == pytest.approx(0.01)


def test_compute_secondary_volume_exactly_max_passes() -> None:
    """Boundary: rounded == max is acceptable."""
    result = compute_secondary_volume(
        primary_volume_lots=200.0,
        ftmo_units_per_lot=100_000,
        exness_contract_size=100_000,
        risk_ratio=1.0,
        exness_volume_step=0.01,
        exness_volume_min=0.01,
        exness_volume_max=200.0,
    )
    assert result == pytest.approx(200.0)


# ---------- validation errors ----------


def test_compute_secondary_volume_zero_contract_size_value_error() -> None:
    with pytest.raises(ValueError, match="exness_contract_size"):
        compute_secondary_volume(
            primary_volume_lots=0.1,
            ftmo_units_per_lot=100_000,
            exness_contract_size=0,
            risk_ratio=1.0,
            exness_volume_step=0.01,
            exness_volume_min=0.01,
            exness_volume_max=200.0,
        )


def test_compute_secondary_volume_zero_risk_ratio_value_error() -> None:
    with pytest.raises(ValueError, match="risk_ratio"):
        compute_secondary_volume(
            primary_volume_lots=0.1,
            ftmo_units_per_lot=100_000,
            exness_contract_size=100_000,
            risk_ratio=0,
            exness_volume_step=0.01,
            exness_volume_min=0.01,
            exness_volume_max=200.0,
        )


def test_compute_secondary_volume_zero_primary_value_error() -> None:
    with pytest.raises(ValueError, match="primary_volume_lots"):
        compute_secondary_volume(
            primary_volume_lots=0,
            ftmo_units_per_lot=100_000,
            exness_contract_size=100_000,
            risk_ratio=1.0,
            exness_volume_step=0.01,
            exness_volume_min=0.01,
            exness_volume_max=200.0,
        )


# ---------- round_to_step direct ----------


def test_round_to_step_floors_to_multiple() -> None:
    assert round_to_step(0.103, 0.01) == pytest.approx(0.10)
    assert round_to_step(0.109, 0.01) == pytest.approx(0.10)
    assert round_to_step(0.100, 0.01) == pytest.approx(0.10)


def test_round_to_step_handles_float_quirk() -> None:
    """0.3 / 0.01 = 30.000000000000004 — must still round to 0.30."""
    assert round_to_step(0.30, 0.01) == pytest.approx(0.30)


def test_round_to_step_zero_step_rejects() -> None:
    with pytest.raises(ValueError, match="volume_step"):
        round_to_step(1.0, 0)
