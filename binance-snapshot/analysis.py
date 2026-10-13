"""Pure, deterministic calculations over validated closed Binance candles."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from models import Candle


@dataclass(frozen=True, slots=True)
class Indicators:
    ema20: Decimal | None
    ema50: Decimal | None
    rsi14: Decimal | None
    atr14: Decimal | None
    relative_volume20: Decimal | None


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    """SMA-seeded EMA; at least `period` closed values are required."""
    if period <= 0:
        raise ValueError("EMA period must be positive.")
    if len(values) < period:
        return None
    result = sum(values[:period]) / Decimal(period)
    multiplier = Decimal(2) / Decimal(period + 1)
    for value in values[period:]:
        result += (value - result) * multiplier
    return result


def rsi(closes: Sequence[Decimal], period: int = 14) -> Decimal | None:
    """Wilder RSI; a flat series is neutral (50), not overbought (100)."""
    if period <= 0:
        raise ValueError("RSI period must be positive.")
    if len(closes) < period + 1:
        return None
    changes = [current - previous for previous, current in zip(closes, closes[1:], strict=False)]
    gains = [max(change, Decimal(0)) for change in changes]
    losses = [max(-change, Decimal(0)) for change in changes]
    avg_gain = sum(gains[:period]) / Decimal(period)
    avg_loss = sum(losses[:period]) / Decimal(period)
    for gain, loss in zip(gains[period:], losses[period:], strict=True):
        avg_gain = (avg_gain * (period - 1) + gain) / Decimal(period)
        avg_loss = (avg_loss * (period - 1) + loss) / Decimal(period)
    if avg_gain == 0 and avg_loss == 0:
        return Decimal(50)
    if avg_loss == 0:
        return Decimal(100)
    if avg_gain == 0:
        return Decimal(0)
    return Decimal(100) - Decimal(100) / (Decimal(1) + avg_gain / avg_loss)


def atr(candles: Sequence[Candle], period: int = 14) -> Decimal | None:
    """Wilder ATR; needs `period + 1` closed candles for previous closes."""
    if period <= 0:
        raise ValueError("ATR period must be positive.")
    if len(candles) < period + 1:
        return None
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(candles, candles[1:], strict=False)
    ]
    result = sum(ranges[:period]) / Decimal(period)
    for true_range in ranges[period:]:
        result = (result * (period - 1) + true_range) / Decimal(period)
    return result


def relative_volume(candles: Sequence[Candle], period: int = 20) -> Decimal | None:
    """Latest closed base volume divided by the preceding `period`-bar mean."""
    if period <= 0:
        raise ValueError("Relative-volume period must be positive.")
    if len(candles) < period + 1:
        return None
    baseline = sum(candle.base_volume for candle in candles[-period - 1 : -1]) / Decimal(period)
    return candles[-1].base_volume / baseline if baseline > 0 else None


def completed_return(candle: Candle) -> Decimal:
    """Close/open - 1 for exactly one completed candle, in percent."""
    return (candle.close / candle.open - Decimal(1)) * Decimal(100)


def indicators(candles: Sequence[Candle]) -> Indicators:
    closes = [candle.close for candle in candles]
    return Indicators(
        ema20=ema(closes, 20),
        ema50=ema(closes, 50),
        rsi14=rsi(closes),
        atr14=atr(candles),
        relative_volume20=relative_volume(candles),
    )
