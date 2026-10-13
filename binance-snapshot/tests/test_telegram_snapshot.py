"""Offline formatting and opt-in delivery tests over structured snapshots."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, Mock

import pytest
import telegram_snapshot
import telegram_snapshot_formatter
from conftest import candle, ticker
from models import AssetMarketData, MarketDataError, MarketSnapshot, Ticker, Timeframe
from snapshot import SYMBOLS
from telegram_notifier import TelegramConfig, TelegramConfigError, TelegramDeliveryError
from telegram_snapshot_formatter import TelegramSnapshotFormatError, format_telegram_snapshot

REFERENCE = datetime(2025, 1, 2, 8, 0, 31, tzinfo=UTC)
CUTOFF = datetime(2025, 1, 2, 8, tzinfo=UTC)


def series(symbol: str, timeframe: Timeframe, count: int = 60) -> tuple:
    return tuple(
        candle(symbol, timeframe, CUTOFF - (count - index) * timeframe.duration)
        for index in range(count)
    )


def full_snapshot(*, live_price: Decimal = Decimal(100)) -> MarketSnapshot:
    assets = tuple(
        AssetMarketData(
            symbol,
            Ticker(symbol, live_price, live_price + 10, live_price - 10, CUTOFF),
            series(symbol, Timeframe.H1),
            series(symbol, Timeframe.H4),
        )
        for symbol in SYMBOLS
    )
    return MarketSnapshot(REFERENCE, assets)


def test_formatter_is_deterministic_and_includes_exact_six_assets() -> None:
    snapshot = full_snapshot()
    first = format_telegram_snapshot(snapshot)
    assert first == format_telegram_snapshot(snapshot)
    assert tuple(line for line in first.splitlines() if line in SYMBOLS) == SYMBOLS
    assert first.startswith(
        "BINANCE SPOT SNAPSHOT\nReference: 2025-01-02 08:00 UTC\n"
        "Live prices are ticker values; indicators use closed candles."
    )
    assert len(first) <= 4096


def test_closed_indicators_remain_separate_from_live_price() -> None:
    message = format_telegram_snapshot(full_snapshot(live_price=Decimal(200)))
    btc = message.split("BTCUSDT\n", maxsplit=1)[1].split("\n\nETHUSDT", maxsplit=1)[0]
    assert "Live price: 200.00 USDT (ticker 2025-01-02 08:00 UTC)" in btc
    assert "1H closed 2025-01-02 08:00 UTC | C 100.00 USDT | return 0.00%" in btc
    assert "4H closed 2025-01-02 08:00 UTC | C 100.00 USDT | return 0.00%" in btc
    assert (
        btc.count("EMA20 100.00 | EMA50 100.00 | RSI14 50.00 | ATR14 10.00 USDT | RVOL20 1.00x")
        == 2
    )


def test_missing_assets_and_indicators_are_explicit() -> None:
    btc = AssetMarketData("BTCUSDT", ticker(), series("BTCUSDT", Timeframe.H1, 1), ())
    message = format_telegram_snapshot(MarketSnapshot(REFERENCE, (btc,)))
    assert "EMA20 unavailable | EMA50 unavailable | RSI14 unavailable" in message
    assert "ATR14 unavailable USDT | RVOL20 unavailable" in message
    assert "4H closed: unavailable (no candles)" in message
    for symbol in SYMBOLS[1:]:
        assert f"{symbol}\nMarket data: unavailable (asset missing)" in message


def test_existing_indicator_function_is_called_once_per_present_timeframe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = telegram_snapshot_formatter.indicators
    calls: list[int] = []

    def count_calls(candles: tuple) -> object:
        calls.append(len(candles))
        return original(candles)

    monkeypatch.setattr(telegram_snapshot_formatter, "indicators", count_calls)
    format_telegram_snapshot(full_snapshot())
    assert calls == [60] * 12


def test_unsupported_or_duplicate_assets_are_rejected() -> None:
    btc = full_snapshot().assets[0]
    with pytest.raises(TelegramSnapshotFormatError, match="duplicate"):
        format_telegram_snapshot(MarketSnapshot(REFERENCE, (btc, btc)))
    other = AssetMarketData("XRPUSDT", ticker("XRPUSDT"), (), ())
    with pytest.raises(TelegramSnapshotFormatError, match="unsupported"):
        format_telegram_snapshot(MarketSnapshot(REFERENCE, (other,)))


def test_oversized_message_is_rejected_without_truncation() -> None:
    giant = Decimal("9" * 4300)
    btc = AssetMarketData(
        "BTCUSDT",
        Ticker("BTCUSDT", giant, giant, giant, CUTOFF),
        series("BTCUSDT", Timeframe.H1, 1),
        series("BTCUSDT", Timeframe.H4, 1),
    )
    with pytest.raises(TelegramSnapshotFormatError, match="one-message limit"):
        format_telegram_snapshot(MarketSnapshot(REFERENCE, (btc,)))


def test_without_send_flag_no_collection_config_or_delivery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    forbidden = Mock(side_effect=AssertionError("must be opt-in"))
    monkeypatch.setattr(telegram_snapshot.requests, "Session", forbidden)
    monkeypatch.setattr(telegram_snapshot, "load_config", forbidden)
    monkeypatch.setattr(telegram_snapshot, "send_message", forbidden)
    assert telegram_snapshot.main([]) == 2
    assert "Pass --send" in capsys.readouterr().err
    forbidden.assert_not_called()


def test_send_flag_delivers_one_formatted_structured_snapshot(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(telegram_snapshot.requests, "Session", lambda: session)
    collected = Mock(return_value=full_snapshot())
    monkeypatch.setattr(telegram_snapshot, "collect_snapshot", collected)
    config = TelegramConfig("123456:FAKE_test-token", "-1001234567890")
    loaded = Mock(return_value=config)
    delivered = Mock()
    monkeypatch.setattr(telegram_snapshot, "load_config", loaded)
    monkeypatch.setattr(telegram_snapshot, "send_message", delivered)

    assert telegram_snapshot.main(["--send"]) == 0
    assert collected.call_count == 1
    assert collected.call_args.args[1] == SYMBOLS
    assert collected.call_args.args[2].utcoffset().total_seconds() == 0
    loaded.assert_called_once_with()
    delivered.assert_called_once_with(format_telegram_snapshot(full_snapshot()), config)
    assert capsys.readouterr().out == "Telegram snapshot delivered.\n"


@pytest.mark.parametrize(
    "failure",
    [MarketDataError("offline market failure"), TelegramConfigError("invalid configuration")],
)
def test_failure_never_delivers(
    failure: Exception, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(telegram_snapshot.requests, "Session", lambda: session)
    collector = Mock(return_value=full_snapshot())
    loader = Mock(return_value=TelegramConfig("123456:FAKE_test-token", "-1001234567890"))
    if isinstance(failure, MarketDataError):
        collector.side_effect = failure
    else:
        loader.side_effect = failure
    delivered = Mock()
    monkeypatch.setattr(telegram_snapshot, "collect_snapshot", collector)
    monkeypatch.setattr(telegram_snapshot, "load_config", loader)
    monkeypatch.setattr(telegram_snapshot, "send_message", delivered)
    assert telegram_snapshot.main(["--send"]) == 1
    delivered.assert_not_called()
    assert "Telegram snapshot unavailable" in capsys.readouterr().err


def test_delivery_error_does_not_print_credentials(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(telegram_snapshot.requests, "Session", lambda: session)
    monkeypatch.setattr(telegram_snapshot, "collect_snapshot", Mock(return_value=full_snapshot()))
    token = "123456:FAKE_test-token"
    monkeypatch.setattr(
        telegram_snapshot, "load_config", Mock(return_value=TelegramConfig(token, "123"))
    )
    monkeypatch.setattr(
        telegram_snapshot,
        "send_message",
        Mock(side_effect=TelegramDeliveryError("Telegram request timed out; delivery is unknown.")),
    )
    assert telegram_snapshot.main(["--send"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert token not in output.err
    assert "timed out" in output.err
