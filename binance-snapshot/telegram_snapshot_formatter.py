"""Deterministic plain-text Telegram view of a structured market snapshot."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from analysis import completed_return, indicators
from models import AssetMarketData, MarketSnapshot, Timeframe
from snapshot import SYMBOLS

MAX_MESSAGE_LENGTH = 4096


class TelegramSnapshotFormatError(ValueError):
    """The snapshot cannot be represented as one complete Telegram message."""


def _number(value: Decimal | None, *, suffix: str = "") -> str:
    return "unavailable" if value is None else f"{value:,.2f}{suffix}"


def _utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _timeframe_lines(asset: AssetMarketData, timeframe: Timeframe) -> tuple[str, ...]:
    series = asset.candles(timeframe)
    label = timeframe.value.upper()
    if not series:
        return (f"{label} closed: unavailable (no candles)",)
    latest = series[-1]
    values = indicators(series)
    return (
        f"{label} closed {_utc(latest.close_time)} | C {_number(latest.close)} USDT | "
        f"return {_number(completed_return(latest), suffix='%')}",
        f"  EMA20 {_number(values.ema20)} | EMA50 {_number(values.ema50)} | "
        f"RSI14 {_number(values.rsi14)} | ATR14 {_number(values.atr14)} USDT | "
        f"RVOL20 {_number(values.relative_volume20, suffix='x')}",
    )


def format_telegram_snapshot(snapshot: MarketSnapshot) -> str:
    """Render all six assets in a fixed order without modifying or fetching data."""
    if snapshot.reference_time.tzinfo is None or snapshot.reference_time.utcoffset() != timedelta(
        0
    ):
        raise TelegramSnapshotFormatError("Snapshot reference time must be UTC.")

    assets: dict[str, AssetMarketData] = {}
    for asset in snapshot.assets:
        if asset.symbol not in SYMBOLS:
            raise TelegramSnapshotFormatError("Snapshot contains an unsupported asset.")
        if asset.symbol in assets:
            raise TelegramSnapshotFormatError("Snapshot contains a duplicate asset.")
        assets[asset.symbol] = asset

    lines = [
        "BINANCE SPOT SNAPSHOT",
        f"Reference: {_utc(snapshot.reference_time)}",
        "Live prices are ticker values; indicators use closed candles.",
    ]
    for symbol in SYMBOLS:
        lines.append("")
        lines.append(symbol)
        asset = assets.get(symbol)
        if asset is None:
            lines.append("Market data: unavailable (asset missing)")
            continue
        lines.append(
            f"Live price: {_number(asset.ticker.last_price)} USDT "
            f"(ticker {_utc(asset.ticker.as_of)})"
        )
        for timeframe in (Timeframe.H1, Timeframe.H4):
            lines.extend(_timeframe_lines(asset, timeframe))

    message = "\n".join(lines)
    # Telegram's sendMessage limit is 4096 characters. UTF-16 units are a conservative
    # count for non-BMP symbols, and rejection avoids silently dropping any asset.
    if len(message.encode("utf-16-le")) // 2 > MAX_MESSAGE_LENGTH:
        raise TelegramSnapshotFormatError("Telegram snapshot exceeds one-message limit.")
    return message
