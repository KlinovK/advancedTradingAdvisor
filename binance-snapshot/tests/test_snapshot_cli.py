"""CLI contract tests without network calls or credentials."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
import snapshot
from conftest import candle, ticker
from models import AssetMarketData, MarketDataError, MarketSnapshot, Timeframe

EXPECTED_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "LINKUSDT",
    "AAVEUSDT",
    "ONDOUSDT",
)


def sample_snapshot() -> MarketSnapshot:
    asset = AssetMarketData(
        "BTCUSDT",
        ticker(),
        (candle(),),
        (candle(timeframe=Timeframe.H4, open_time=datetime(2025, 1, 2, 4, tzinfo=UTC)),),
    )
    return MarketSnapshot(datetime(2025, 1, 2, 8, 0, 31, tzinfo=UTC), (asset,))


def test_exact_six_symbols_are_configured() -> None:
    assert snapshot.SYMBOLS == EXPECTED_SYMBOLS


def test_render_format_is_stable() -> None:
    expected = "\n".join(
        (
            "BINANCE SPOT SNAPSHOT",
            "Reference: 2025-01-02 08:00 UTC",
            "",
            "BTCUSDT",
            "Live price: 100.00 USDT (ticker 2025-01-02 08:00 UTC)",
            "Rolling 24h high/low: 110.00/90.00 USDT",
            "1H closed 2025-01-02 08:00 UTC | O/H/L/C 100.00/105.00/95.00/100.00 USDT | "
            "return 0.00% | base volume 10.0000",
            "  EMA20 unavailable | EMA50 unavailable | RSI14 unavailable | "
            "ATR14 unavailable USDT | RVOL20 unavailable",
            "4H closed 2025-01-02 08:00 UTC | O/H/L/C 100.00/105.00/95.00/100.00 USDT | "
            "return 0.00% | base volume 10.0000",
            "  EMA20 unavailable | EMA50 unavailable | RSI14 unavailable | "
            "ATR14 unavailable USDT | RVOL20 unavailable",
            "Data quality: validated closed candles; aligned UTC cutoffs",
        )
    )
    assert snapshot.render(sample_snapshot()) == expected


def test_main_prints_report_and_uses_exact_symbols(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(snapshot.requests, "Session", lambda: session)
    monkeypatch.setattr(snapshot, "BinanceClient", lambda _session: Mock())
    captured_symbols: list[tuple[str, ...]] = []

    def collect(_client: object, symbols: tuple[str, ...], _reference: datetime) -> MarketSnapshot:
        captured_symbols.append(symbols)
        return sample_snapshot()

    monkeypatch.setattr(snapshot, "collect_snapshot", collect)
    assert snapshot.main() == 0
    assert captured_symbols == [EXPECTED_SYMBOLS]
    output = capsys.readouterr()
    assert output.out == snapshot.render(sample_snapshot()) + "\n"
    assert output.err == ""


def test_main_reports_market_data_failure_without_partial_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(snapshot.requests, "Session", lambda: session)
    monkeypatch.setattr(snapshot, "BinanceClient", lambda _session: Mock())

    def fail(_client: object, _symbols: tuple[str, ...], _reference: datetime) -> MarketSnapshot:
        raise MarketDataError("offline fixture")

    monkeypatch.setattr(snapshot, "collect_snapshot", fail)
    assert snapshot.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == "Snapshot unavailable: offline fixture\n"
