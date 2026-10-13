"""Manual, read-only Binance Spot market snapshot. Run with `python snapshot.py`."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal

import requests
from analysis import completed_return, indicators
from market_data import BinanceClient, collect_snapshot
from models import Candle, MarketDataError, MarketSnapshot, Timeframe

SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "LINKUSDT",
    "AAVEUSDT",
    "ONDOUSDT",
)


def number(value: Decimal | None, places: int = 2, suffix: str = "") -> str:
    if value is None:
        return "unavailable"
    return f"{value:,.{places}f}{suffix}"


def utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def candle_line(candle: Candle) -> str:
    data = indicators_for_line(candle)
    return (
        f"{candle.timeframe.value.upper()} closed {utc(candle.close_time)} | "
        f"O/H/L/C {number(candle.open)}/{number(candle.high)}/"
        f"{number(candle.low)}/{number(candle.close)} USDT | "
        f"return {number(completed_return(candle), suffix='%')} | {data}"
    )


def indicators_for_line(candle: Candle) -> str:
    return f"base volume {number(candle.base_volume, 4)}"


def render(snapshot: MarketSnapshot) -> str:
    lines = ["BINANCE SPOT SNAPSHOT", f"Reference: {utc(snapshot.reference_time)}", ""]
    for asset in snapshot.assets:
        ticker = asset.ticker
        lines.extend(
            (
                asset.symbol,
                f"Live price: {number(ticker.last_price)} USDT (ticker {utc(ticker.as_of)})",
                f"Rolling 24h high/low: {number(ticker.high_24h)}/{number(ticker.low_24h)} USDT",
            )
        )
        for timeframe in (Timeframe.H1, Timeframe.H4):
            series = asset.candles(timeframe)
            values = indicators(series)
            lines.append(candle_line(series[-1]))
            lines.append(
                f"  EMA20 {number(values.ema20)} | EMA50 {number(values.ema50)} | "
                f"RSI14 {number(values.rsi14)} | ATR14 {number(values.atr14)} USDT | "
                f"RVOL20 {number(values.relative_volume20, suffix='x')}"
            )
        lines.append("Data quality: validated closed candles; aligned UTC cutoffs")
        lines.append("")
    return "\n".join(lines).rstrip()


def main() -> int:
    try:
        with requests.Session() as session:
            client = BinanceClient(session)
            reference_time = datetime.now(UTC)
            result = collect_snapshot(client, SYMBOLS, reference_time)
        report = render(result)
    except MarketDataError as error:
        print(f"Snapshot unavailable: {error}", file=sys.stderr)
        return 1
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
