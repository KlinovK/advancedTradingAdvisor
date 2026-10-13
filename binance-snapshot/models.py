"""Immutable, validated Binance Spot market observations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class MarketDataError(ValueError):
    """The requested market snapshot cannot be trusted."""


class Timeframe(StrEnum):
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def duration(self) -> timedelta:
        return {
            Timeframe.H1: timedelta(hours=1),
            Timeframe.H4: timedelta(hours=4),
            Timeframe.D1: timedelta(days=1),
        }[self]


def _utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _finite(value: Decimal, *, positive: bool = False) -> bool:
    return (
        isinstance(value, Decimal) and value.is_finite() and (value > 0 if positive else value >= 0)
    )


@dataclass(frozen=True, slots=True)
class Candle:
    """A Binance kline; close_time is the exclusive UTC interval boundary."""

    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal
    taker_buy_base_volume: Decimal
    taker_buy_quote_volume: Decimal

    def __post_init__(self) -> None:
        if not self.symbol or not _utc(self.open_time) or not _utc(self.close_time):
            raise MarketDataError("Invalid candle identity or UTC timestamps.")
        if self.close_time != self.open_time + self.timeframe.duration:
            raise MarketDataError("Candle interval does not match its timeframe.")
        if (
            self.open_time.minute != 0
            or self.open_time.second != 0
            or self.open_time.microsecond != 0
            or self.open_time.hour
            % (24 if self.timeframe is Timeframe.D1 else self.timeframe.duration.seconds // 3600)
            != 0
        ):
            raise MarketDataError("Candle opening time is off the UTC timeframe grid.")
        if not all(
            _finite(value, positive=True) for value in (self.open, self.high, self.low, self.close)
        ):
            raise MarketDataError("Candle prices must be positive finite Decimals.")
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise MarketDataError("Candle OHLC prices are inconsistent.")
        volumes = (
            self.base_volume,
            self.quote_volume,
            self.taker_buy_base_volume,
            self.taker_buy_quote_volume,
        )
        if not all(_finite(value) for value in volumes):
            raise MarketDataError("Candle volumes must be nonnegative finite Decimals.")
        if (
            self.taker_buy_base_volume > self.base_volume
            or self.taker_buy_quote_volume > self.quote_volume
        ):
            raise MarketDataError("Taker-buy volume exceeds total volume.")


@dataclass(frozen=True, slots=True)
class Ticker:
    symbol: str
    last_price: Decimal
    high_24h: Decimal
    low_24h: Decimal
    as_of: datetime

    def __post_init__(self) -> None:
        if not self.symbol or not _utc(self.as_of):
            raise MarketDataError("Invalid ticker identity or UTC timestamp.")
        if not all(
            _finite(value, positive=True)
            for value in (self.last_price, self.high_24h, self.low_24h)
        ):
            raise MarketDataError("Ticker prices must be positive finite Decimals.")
        if not self.low_24h <= self.last_price <= self.high_24h:
            raise MarketDataError("Ticker price is outside its rolling 24-hour range.")


@dataclass(frozen=True, slots=True)
class AssetMarketData:
    symbol: str
    ticker: Ticker
    hourly: tuple[Candle, ...]
    four_hour: tuple[Candle, ...]
    daily: tuple[Candle, ...] = ()

    def candles(self, timeframe: Timeframe) -> tuple[Candle, ...]:
        return {
            Timeframe.H1: self.hourly,
            Timeframe.H4: self.four_hour,
            Timeframe.D1: self.daily,
        }[timeframe]


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    reference_time: datetime
    assets: tuple[AssetMarketData, ...]
