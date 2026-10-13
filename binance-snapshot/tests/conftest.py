"""Deterministic, offline fixtures for the standalone snapshot script."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from models import Candle, Ticker, Timeframe


@pytest.fixture
def reference_time() -> datetime:
    return datetime(2025, 1, 2, 8, 0, 31, tzinfo=UTC)


def candle(
    symbol: str = "BTCUSDT",
    timeframe: Timeframe = Timeframe.H1,
    open_time: datetime = datetime(2025, 1, 2, 7, tzinfo=UTC),
    *,
    close: Decimal = Decimal(100),
    volume: Decimal = Decimal(10),
) -> Candle:
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        close_time=open_time + timeframe.duration,
        open=close,
        high=close + 5,
        low=close - 5,
        close=close,
        base_volume=volume,
        quote_volume=volume * close,
        taker_buy_base_volume=volume / 2,
        taker_buy_quote_volume=volume * close / 2,
    )


def kline_row(open_time: datetime, timeframe: Timeframe, *, close: str = "100") -> list[object]:
    closing_ms = int(
        ((open_time + timeframe.duration) - timedelta(milliseconds=1)).timestamp() * 1000
    )
    return [
        int(open_time.timestamp() * 1000),
        close,
        "105",
        "95",
        close,
        "10",
        closing_ms,
        "1000",
        0,
        "5",
        "500",
    ]


def ticker(symbol: str = "BTCUSDT") -> Ticker:
    return Ticker(
        symbol,
        Decimal(100),
        Decimal(110),
        Decimal(90),
        datetime(2025, 1, 2, 8, tzinfo=UTC),
    )
