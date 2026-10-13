"""Fixed numerical vectors for the existing closed-candle indicators."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from analysis import atr, ema, indicators, relative_volume, rsi
from conftest import candle


@pytest.mark.parametrize("period,expected", [(20, "90.5"), (50, "75.5")])
def test_ema_linear_golden_vector(period: int, expected: str) -> None:
    closes = [Decimal(value) for value in range(1, 101)]
    assert abs(ema(closes, period) - Decimal(expected)) < Decimal("1e-20")


def test_ema_requires_full_seed_window() -> None:
    assert ema([Decimal(100)] * 19, 20) is None
    assert ema([Decimal(100)] * 49, 50) is None
    assert ema([Decimal(100)] * 50, 50) == Decimal(100)


@pytest.mark.parametrize("direction,expected", [(1, "100"), (-1, "0"), (0, "50")])
def test_rsi_wilder_edge_vectors(direction: int, expected: str) -> None:
    closes = [Decimal(100 + index * direction) for index in range(15)]
    assert rsi(closes) == Decimal(expected)
    assert rsi(closes[:14]) is None


def test_rsi_mixed_wilder_golden_vector() -> None:
    closes = [
        Decimal(value)
        for value in (
            "44.34",
            "44.09",
            "44.15",
            "43.61",
            "44.33",
            "44.83",
            "45.10",
            "45.42",
            "45.84",
            "46.08",
            "45.89",
            "46.03",
            "45.61",
            "46.28",
            "46.28",
        )
    ]
    assert abs(rsi(closes) - Decimal("70.46413502109705")) < Decimal("0.000001")


def test_atr_fixed_true_range_golden_vector() -> None:
    start = datetime(2025, 1, 1, 0, tzinfo=UTC)
    candles = tuple(candle(open_time=start + timedelta(hours=index)) for index in range(16))
    assert atr(candles) == Decimal(10)
    assert atr(candles[:14]) is None


def test_rvol_uses_prior_twenty_bars_not_current_bar() -> None:
    start = datetime(2025, 1, 1, 0, tzinfo=UTC)
    baseline = tuple(candle(open_time=start + timedelta(hours=index)) for index in range(20))
    latest = candle(open_time=start + timedelta(hours=20), volume=Decimal(25))
    assert relative_volume((*baseline, latest)) == Decimal("2.5")
    assert relative_volume(baseline) is None
    zero_baseline = tuple(
        candle(open_time=start + timedelta(hours=index), volume=Decimal(0)) for index in range(20)
    )
    assert relative_volume((*zero_baseline, latest)) is None


def test_indicator_bundle_matches_individual_calculations() -> None:
    start = datetime(2025, 1, 1, 0, tzinfo=UTC)
    candles = tuple(
        candle(open_time=start + timedelta(hours=index), close=Decimal(100 + index))
        for index in range(60)
    )
    result = indicators(candles)
    assert result.ema20 == ema([item.close for item in candles], 20)
    assert result.ema50 == ema([item.close for item in candles], 50)
    assert result.rsi14 == rsi([item.close for item in candles], 14)
    assert result.atr14 == atr(candles, 14)
    assert result.relative_volume20 == Decimal(1)
