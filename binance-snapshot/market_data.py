"""Read-only Binance Spot fetching and fail-closed market-data validation."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime

import requests
from models import AssetMarketData, Candle, MarketDataError, MarketSnapshot, Ticker, Timeframe

BASE_URL = "https://api.binance.com"
KLINE_LIMIT = 100
BOUNDARY_GRACE = timedelta(seconds=30)
MAX_TICKER_AGE = timedelta(minutes=5)
MAX_CLOCK_SKEW = timedelta(seconds=10)
RUN_DEADLINE_SECONDS = 90.0
MAX_ATTEMPTS = 2
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _from_milliseconds(value: object) -> datetime:
    if type(value) is not int or value < 0:
        raise MarketDataError("Invalid Binance millisecond timestamp.")
    try:
        return EPOCH + timedelta(milliseconds=value)
    except OverflowError as error:
        raise MarketDataError("Binance timestamp is out of range.") from error


def _decimal(value: object) -> Decimal:
    if not isinstance(value, str):
        raise MarketDataError("Expected a decimal string in Binance market data.")
    try:
        return Decimal(value)
    except InvalidOperation as error:
        raise MarketDataError("Invalid decimal string in Binance market data.") from error


def expected_cutoff(reference_time: datetime, timeframe: Timeframe) -> datetime:
    """Latest UTC boundary safely past the publication grace at snapshot start."""
    if reference_time.tzinfo is None or reference_time.utcoffset() != timedelta(0):
        raise MarketDataError("Snapshot reference time must be UTC.")
    elapsed = reference_time - BOUNDARY_GRACE - EPOCH
    return EPOCH + (elapsed // timeframe.duration) * timeframe.duration


def parse_ticker(payload: object, symbol: str, received_at: datetime) -> Ticker:
    if not isinstance(payload, dict) or payload.get("symbol") != symbol:
        raise MarketDataError("Binance ticker symbol or response shape is invalid.")
    try:
        ticker = Ticker(
            symbol=symbol,
            last_price=_decimal(payload["lastPrice"]),
            high_24h=_decimal(payload["highPrice"]),
            low_24h=_decimal(payload["lowPrice"]),
            as_of=_from_milliseconds(payload["closeTime"]),
        )
    except KeyError as error:
        raise MarketDataError("Binance ticker is missing a required field.") from error
    if received_at.tzinfo is None or received_at.utcoffset() != timedelta(0):
        raise MarketDataError("Ticker receipt time must be UTC.")
    if ticker.as_of > received_at + MAX_CLOCK_SKEW:
        raise MarketDataError(f"{symbol} ticker timestamp is in the future.")
    if received_at - ticker.as_of > MAX_TICKER_AGE:
        raise MarketDataError(f"{symbol} ticker is stale.")
    return ticker


def parse_kline(row: object, symbol: str, timeframe: Timeframe) -> Candle:
    if not isinstance(row, (list, tuple)) or len(row) < 11:
        raise MarketDataError("Binance kline response shape is invalid.")
    open_time = _from_milliseconds(row[0])
    close_time_inclusive = _from_milliseconds(row[6])
    close_time = open_time + timeframe.duration
    if close_time_inclusive != close_time - timedelta(milliseconds=1):
        raise MarketDataError("Binance kline close time does not match its interval.")
    return Candle(
        symbol=symbol,
        timeframe=timeframe,
        open_time=open_time,
        close_time=close_time,
        open=_decimal(row[1]),
        high=_decimal(row[2]),
        low=_decimal(row[3]),
        close=_decimal(row[4]),
        base_volume=_decimal(row[5]),
        quote_volume=_decimal(row[7]),
        taker_buy_base_volume=_decimal(row[9]),
        taker_buy_quote_volume=_decimal(row[10]),
    )


def parse_closed_klines(
    payload: object,
    symbol: str,
    timeframe: Timeframe,
    reference_time: datetime,
) -> tuple[Candle, ...]:
    if not isinstance(payload, list) or not payload:
        raise MarketDataError(f"{symbol} {timeframe} kline response is empty or malformed.")
    rows = tuple(parse_kline(row, symbol, timeframe) for row in payload)
    for previous, current in zip(rows, rows[1:], strict=False):
        if current.open_time != previous.close_time:
            raise MarketDataError(f"{symbol} {timeframe} klines have a gap or duplicate.")
    if any(row.open_time > reference_time + MAX_CLOCK_SKEW for row in rows):
        raise MarketDataError(f"{symbol} {timeframe} contains a future candle.")
    cutoff = expected_cutoff(reference_time, timeframe)
    closed = tuple(row for row in rows if row.close_time <= cutoff)
    if not closed or closed[-1].close_time != cutoff:
        raise MarketDataError(f"{symbol} {timeframe} latest closed candle is stale or absent.")
    return closed


def _retry_after_seconds(value: str | None, now: datetime) -> float | None:
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0.0, (parsed - now).total_seconds())


class BinanceClient:
    def __init__(
        self,
        session: requests.Session,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._monotonic = monotonic
        self._sleep = sleep
        self._now = now or (lambda: datetime.now(UTC))

    def deadline(self) -> float:
        return self._monotonic() + RUN_DEADLINE_SECONDS

    def _request_json(self, path: str, params: dict[str, str | int], deadline: float) -> object:
        for attempt in range(MAX_ATTEMPTS):
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise MarketDataError("Binance snapshot exceeded its overall time budget.")
            try:
                response = self._session.get(
                    f"{BASE_URL}{path}",
                    params=params,
                    timeout=(min(3.0, remaining), min(8.0, remaining)),
                )
            except requests.RequestException as error:
                if attempt + 1 == MAX_ATTEMPTS:
                    raise MarketDataError("Binance transport failed.") from error
                delay = 0.25
            else:
                if self._monotonic() >= deadline:
                    raise MarketDataError("Binance snapshot exceeded its overall time budget.")
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt + 1 == MAX_ATTEMPTS:
                        raise MarketDataError(f"Binance HTTP {response.status_code} after retry.")
                    retry_after = _retry_after_seconds(
                        response.headers.get("Retry-After"), self._now()
                    )
                    delay = retry_after if retry_after is not None else 0.25
                elif response.status_code >= 400:
                    raise MarketDataError(f"Binance HTTP {response.status_code}.")
                else:
                    try:
                        result = response.json()
                    except ValueError as error:
                        raise MarketDataError("Binance returned malformed JSON.") from error
                    if self._monotonic() >= deadline:
                        raise MarketDataError("Binance snapshot exceeded its overall time budget.")
                    return result
            if delay > deadline - self._monotonic():
                raise MarketDataError("Binance retry exceeds the overall time budget.")
            self._sleep(delay)
        raise MarketDataError("Binance request failed.")

    def fetch_ticker(self, symbol: str, deadline: float) -> Ticker:
        payload = self._request_json("/api/v3/ticker/24hr", {"symbol": symbol}, deadline)
        return parse_ticker(payload, symbol, self._now())

    def fetch_klines(
        self, symbol: str, timeframe: Timeframe, reference_time: datetime, deadline: float
    ) -> tuple[Candle, ...]:
        payload = self._request_json(
            "/api/v3/klines",
            {"symbol": symbol, "interval": timeframe.value, "limit": KLINE_LIMIT},
            deadline,
        )
        return parse_closed_klines(payload, symbol, timeframe, reference_time)


def collect_snapshot(
    client: BinanceClient,
    symbols: Iterable[str],
    reference_time: datetime,
    *,
    include_daily: bool = False,
) -> MarketSnapshot:
    """Collect all assets before returning any reportable result."""
    deadline = client.deadline()
    assets = []
    for symbol in symbols:
        ticker = client.fetch_ticker(symbol, deadline)
        hourly = client.fetch_klines(symbol, Timeframe.H1, reference_time, deadline)
        four_hour = client.fetch_klines(symbol, Timeframe.H4, reference_time, deadline)
        daily = (
            client.fetch_klines(symbol, Timeframe.D1, reference_time, deadline)
            if include_daily
            else ()
        )
        assets.append(AssetMarketData(symbol, ticker, hourly, four_hour, daily))
    if not assets:
        raise MarketDataError("No symbols were configured.")
    timeframes = (
        (Timeframe.H1, Timeframe.H4, Timeframe.D1)
        if include_daily
        else (
            Timeframe.H1,
            Timeframe.H4,
        )
    )
    for timeframe in timeframes:
        cutoffs = {asset.candles(timeframe)[-1].close_time for asset in assets}
        if len(cutoffs) != 1:
            raise MarketDataError(f"{timeframe} closed-candle cutoffs differ across symbols.")
    if client._monotonic() >= deadline:
        raise MarketDataError("Binance snapshot exceeded its overall time budget.")
    return MarketSnapshot(reference_time, tuple(assets))
