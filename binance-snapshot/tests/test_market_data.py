"""Regression coverage for candle boundaries and fail-closed Binance fetching."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
import requests
from conftest import candle, kline_row, ticker
from market_data import (
    BinanceClient,
    BinanceRequestError,
    DiagnosticCode,
    FreshnessMarketDataError,
    collect_snapshot,
    expected_cutoff,
    parse_closed_klines,
    parse_ticker,
)
from models import MarketDataError, Timeframe


@pytest.mark.parametrize("timeframe", [Timeframe.H1, Timeframe.H4, Timeframe.D1])
def test_only_fully_closed_candles_are_retained(timeframe: Timeframe) -> None:
    boundary = datetime(2025, 1, 2, 0 if timeframe is Timeframe.D1 else 8, tzinfo=UTC)
    reference = boundary + timedelta(seconds=31)
    previous = boundary - timeframe.duration
    rows = [kline_row(previous, timeframe), kline_row(boundary, timeframe)]
    closed = parse_closed_klines(rows, "BTCUSDT", timeframe, reference)
    assert len(closed) == 1
    assert closed[-1].close_time == expected_cutoff(reference, timeframe)
    assert closed[-1].close_time == boundary


def test_boundary_grace_excludes_newly_closed_candle() -> None:
    boundary = datetime(2025, 1, 2, 8, tzinfo=UTC)
    rows = [
        kline_row(boundary - timedelta(hours=2), Timeframe.H1),
        kline_row(boundary - timedelta(hours=1), Timeframe.H1),
    ]
    closed = parse_closed_klines(rows, "BTCUSDT", Timeframe.H1, boundary + timedelta(seconds=29))
    assert len(closed) == 1
    assert closed[-1].close_time == boundary - timedelta(hours=1)


def test_missing_latest_closed_candle_fails() -> None:
    reference = datetime(2025, 1, 2, 8, 0, 31, tzinfo=UTC)
    rows = [kline_row(datetime(2025, 1, 2, 6, tzinfo=UTC), Timeframe.H1)]
    with pytest.raises(MarketDataError, match="stale or absent"):
        parse_closed_klines(rows, "BTCUSDT", Timeframe.H1, reference)


def test_gap_or_duplicate_candles_fail() -> None:
    reference = datetime(2025, 1, 2, 8, 0, 31, tzinfo=UTC)
    rows = [
        kline_row(datetime(2025, 1, 2, 5, tzinfo=UTC), Timeframe.H1),
        kline_row(datetime(2025, 1, 2, 7, tzinfo=UTC), Timeframe.H1),
    ]
    with pytest.raises(MarketDataError, match="gap or duplicate"):
        parse_closed_klines(rows, "BTCUSDT", Timeframe.H1, reference)


@pytest.mark.parametrize("mismatch", [Timeframe.H1, Timeframe.H4])
def test_cross_symbol_cutoff_mismatch_fails(mismatch: Timeframe, reference_time: datetime) -> None:
    client = Mock()
    client.deadline.return_value = 90.0
    client._monotonic.return_value = 0.0
    client.fetch_ticker.side_effect = lambda symbol, _deadline: ticker(symbol)

    def fetch(symbol: str, timeframe: Timeframe, _reference: datetime, _deadline: float) -> tuple:
        end = datetime(2025, 1, 2, 8, tzinfo=UTC)
        if timeframe is Timeframe.H4:
            end = datetime(2025, 1, 2, 8, tzinfo=UTC)
        if symbol == "ETHUSDT" and timeframe is mismatch:
            end -= timeframe.duration
        return (candle(symbol, timeframe, end - timeframe.duration),)

    client.fetch_klines.side_effect = fetch
    with pytest.raises(MarketDataError, match="cutoffs differ across symbols"):
        collect_snapshot(client, ("BTCUSDT", "ETHUSDT"), reference_time)


def test_matching_cutoffs_return_complete_snapshot(reference_time: datetime) -> None:
    client = Mock()
    client.deadline.return_value = 90.0
    client._monotonic.return_value = 0.0
    client.fetch_ticker.side_effect = lambda symbol, _deadline: ticker(symbol)
    client.fetch_klines.side_effect = lambda symbol, timeframe, _reference, _deadline: (
        candle(symbol, timeframe, datetime(2025, 1, 2, 8, tzinfo=UTC) - timeframe.duration),
    )
    symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "LINKUSDT", "AAVEUSDT", "ONDOUSDT")
    result = collect_snapshot(client, symbols, reference_time)
    assert tuple(asset.symbol for asset in result.assets) == symbols
    assert client.fetch_ticker.call_count == 6
    assert client.fetch_klines.call_count == 12


def response(status: int, payload: object = None, headers: dict[str, str] | None = None) -> Mock:
    item = Mock()
    item.status_code = status
    item.headers = headers or {}
    item.json.return_value = payload
    return item


def test_http_success_uses_public_spot_endpoint_and_bounded_timeout() -> None:
    session = Mock(spec=requests.Session)
    session.get.return_value = response(200, {"ok": True})
    client = BinanceClient(session, monotonic=lambda: 0.0)
    assert client._request_json("/api/v3/klines", {"symbol": "BTCUSDT"}, 10.0) == {"ok": True}
    session.get.assert_called_once_with(
        "https://api.binance.com/api/v3/klines",
        params={"symbol": "BTCUSDT"},
        timeout=(3.0, 8.0),
    )


def test_retryable_http_error_retries_once() -> None:
    session = Mock(spec=requests.Session)
    session.get.side_effect = [response(503), response(200, ["ok"])]
    sleep = Mock()
    client = BinanceClient(session, monotonic=lambda: 0.0, sleep=sleep)
    assert client._request_json("/api/v3/klines", {}, 10.0) == ["ok"]
    assert session.get.call_count == 2
    sleep.assert_called_once_with(0.25)


@pytest.mark.parametrize("status", [400, 403, 404])
def test_non_retryable_http_error_fails_fast(status: int) -> None:
    session = Mock(spec=requests.Session)
    session.get.return_value = response(status)
    client = BinanceClient(session, monotonic=lambda: 0.0)
    with pytest.raises(MarketDataError, match=f"HTTP {status}"):
        client._request_json("/api/v3/klines", {}, 10.0)
    session.get.assert_called_once()


def test_retry_after_beyond_deadline_fails_without_sleep() -> None:
    session = Mock(spec=requests.Session)
    session.get.return_value = response(429, headers={"Retry-After": "30"})
    sleep = Mock()
    client = BinanceClient(session, monotonic=lambda: 0.0, sleep=sleep)
    with pytest.raises(MarketDataError, match="retry exceeds"):
        client._request_json("/api/v3/klines", {}, 10.0)
    session.get.assert_called_once()
    sleep.assert_not_called()


def test_transport_error_retries_then_fails() -> None:
    session = Mock(spec=requests.Session)
    session.get.side_effect = requests.Timeout("offline fixture")
    sleep = Mock()
    client = BinanceClient(session, monotonic=lambda: 0.0, sleep=sleep)
    with pytest.raises(MarketDataError, match="transport failed"):
        client._request_json("/api/v3/klines", {}, 10.0)
    assert session.get.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_malformed_json_fails_without_retry() -> None:
    session = Mock(spec=requests.Session)
    item = response(200)
    item.json.side_effect = ValueError("offline fixture")
    session.get.return_value = item
    client = BinanceClient(session, monotonic=lambda: 0.0)
    with pytest.raises(MarketDataError, match="malformed JSON"):
        client._request_json("/api/v3/klines", {}, 10.0)
    session.get.assert_called_once()


@pytest.mark.parametrize(
    ("status", "code", "attempts"),
    [
        (403, DiagnosticCode.HTTP_403, 1),
        (451, DiagnosticCode.HTTP_451, 1),
        (429, DiagnosticCode.HTTP_429, 2),
        (400, DiagnosticCode.HTTP_OTHER, 1),
        (503, DiagnosticCode.HTTP_OTHER, 2),
    ],
)
def test_http_diagnostic_categories_preserve_retry_policy(
    status: int, code: DiagnosticCode, attempts: int
) -> None:
    session = Mock(spec=requests.Session)
    session.get.return_value = response(status)
    sleep = Mock()
    client = BinanceClient(session, monotonic=lambda: 0.0, sleep=sleep)
    with pytest.raises(BinanceRequestError) as caught:
        client._request_json("/api/v3/klines", {}, 10.0)
    assert caught.value.code is code
    assert caught.value.status_code == status
    assert session.get.call_count == attempts
    assert sleep.call_count == attempts - 1


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (requests.Timeout("secret URL"), DiagnosticCode.TIMEOUT),
        (requests.ConnectionError("secret URL"), DiagnosticCode.CONNECTION_ERROR),
    ],
)
def test_transport_diagnostics_preserve_retry_policy(
    failure: requests.RequestException, code: DiagnosticCode
) -> None:
    session = Mock(spec=requests.Session)
    session.get.side_effect = failure
    sleep = Mock()
    client = BinanceClient(session, monotonic=lambda: 0.0, sleep=sleep)
    with pytest.raises(BinanceRequestError) as caught:
        client._request_json("/api/v3/klines", {}, 10.0)
    assert caught.value.code is code
    assert caught.value.status_code is None
    assert session.get.call_count == 2
    sleep.assert_called_once_with(0.25)


def test_retry_after_budget_keeps_http_category() -> None:
    session = Mock(spec=requests.Session)
    session.get.return_value = response(429, headers={"Retry-After": "30"})
    client = BinanceClient(session, monotonic=lambda: 0.0, sleep=Mock())
    with pytest.raises(BinanceRequestError) as caught:
        client._request_json("/api/v3/klines", {}, 10.0)
    assert caught.value.code is DiagnosticCode.HTTP_429
    assert caught.value.status_code == 429


def test_malformed_json_is_market_data_failure() -> None:
    session = Mock(spec=requests.Session)
    item = response(200)
    item.json.side_effect = ValueError("response secret")
    session.get.return_value = item
    client = BinanceClient(session, monotonic=lambda: 0.0)
    with pytest.raises(MarketDataError) as caught:
        client._request_json("/api/v3/klines", {}, 10.0)
    assert not isinstance(caught.value, BinanceRequestError)
    session.get.assert_called_once()


def test_stale_ticker_has_freshness_type() -> None:
    reference = datetime(2025, 1, 2, 8, tzinfo=UTC)
    payload = {
        "symbol": "BTCUSDT",
        "lastPrice": "1",
        "highPrice": "2",
        "lowPrice": "1",
        "closeTime": int((reference - timedelta(minutes=6)).timestamp() * 1000),
    }
    with pytest.raises(FreshnessMarketDataError):
        parse_ticker(payload, "BTCUSDT", reference)
