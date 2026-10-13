"""One eligible hourly Binance Spot snapshot, with no delivery history."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import requests
from market_data import BASE_URL, BinanceClient, collect_snapshot, expected_cutoff
from models import MarketDataError, MarketSnapshot, Timeframe
from snapshot import SYMBOLS
from telegram_notifier import (
    TelegramConfigError,
    TelegramDeliveryError,
    load_config,
    send_message,
)
from telegram_snapshot_formatter import TelegramSnapshotFormatError, format_telegram_snapshot

MIN_AGE = timedelta(minutes=2)
MAX_AGE = timedelta(minutes=15)
MAX_TIME_REQUEST = timedelta(seconds=5)
MAX_LOCAL_SKEW = timedelta(seconds=30)
MAX_TICKER_AGE_AT_SEND = timedelta(minutes=5)
MAX_TICKER_FUTURE = timedelta(seconds=10)
MIN_INDICATOR_CANDLES = 50
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
DEFAULT_LOCK_PATH = (
    Path.home() / "Library" / "Application Support" / "binance-snapshot" / "hourly.lock"
)


class HourlySnapshotError(ValueError):
    """The collected snapshot cannot safely be delivered automatically."""


class OutsideWindow(HourlySnapshotError):
    """The current hourly candle is outside the automatic delivery window."""


@dataclass(frozen=True, slots=True)
class ServerClock:
    """Server timestamp and maximum age of that timestamp when received."""

    now: datetime
    uncertainty: timedelta = timedelta(0)

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.utcoffset() != timedelta(0):
            raise HourlySnapshotError("Binance clock must be UTC.")
        if self.uncertainty < timedelta(0) or self.uncertainty > MAX_TIME_REQUEST:
            raise HourlySnapshotError("Binance clock sample is too old.")


def fetch_server_clock(
    session: requests.Session, *, now: Callable[[], datetime] | None = None
) -> ServerClock:
    """Read public Binance Spot time once; fail closed on delay or clock skew."""
    clock = now or (lambda: datetime.now(UTC))
    started = clock()
    try:
        response = session.get(f"{BASE_URL}/api/v3/time", timeout=(2.0, 3.0))
    except requests.RequestException:
        raise HourlySnapshotError("Binance server time is unavailable.") from None
    finished = clock()
    elapsed = finished - started
    if elapsed < timedelta(0) or elapsed > MAX_TIME_REQUEST:
        raise HourlySnapshotError("Binance server time response was delayed.")
    if response.status_code != 200:
        raise HourlySnapshotError("Binance server time request failed.")
    try:
        payload = response.json()
    except ValueError:
        raise HourlySnapshotError("Binance server time response is invalid.") from None
    if (
        not isinstance(payload, dict)
        or type(payload.get("serverTime")) is not int
        or payload["serverTime"] < 0
    ):
        raise HourlySnapshotError("Binance server time response is invalid.")
    try:
        server_time = EPOCH + timedelta(milliseconds=payload["serverTime"])
    except (OverflowError, ValueError):
        raise HourlySnapshotError("Binance server time response is invalid.") from None
    if server_time < started - MAX_LOCAL_SKEW or server_time > finished + MAX_LOCAL_SKEW:
        raise HourlySnapshotError("Local clock differs from Binance server time.")
    return ServerClock(server_time, elapsed)


def eligible_close(clock: ServerClock) -> datetime:
    """Return the sole eligible closed 1H boundary, or skip this invocation."""
    close = clock.now.replace(minute=0, second=0, microsecond=0)
    if clock.now - close < MIN_AGE or clock.now + clock.uncertainty - close > MAX_AGE:
        raise OutsideWindow("No fresh hourly candle is eligible now.")
    return close


def validate_snapshot(snapshot: MarketSnapshot, clock: ServerClock) -> datetime:
    """Require complete, aligned closed candles and fresh live tickers before send."""
    close = eligible_close(clock)
    reference = snapshot.reference_time
    if reference.tzinfo is None or reference.utcoffset() != timedelta(0):
        raise HourlySnapshotError("Snapshot reference time is invalid.")
    if reference > clock.now + clock.uncertainty + MAX_TICKER_FUTURE:
        raise HourlySnapshotError("Snapshot reference time is in the future.")
    if len(snapshot.assets) != len(SYMBOLS) or {asset.symbol for asset in snapshot.assets} != set(
        SYMBOLS
    ):
        raise HourlySnapshotError("Snapshot does not contain the six required assets.")

    for asset in snapshot.assets:
        if asset.ticker.symbol != asset.symbol:
            raise HourlySnapshotError("Snapshot ticker symbol does not match its asset.")
        if clock.now + clock.uncertainty - asset.ticker.as_of > MAX_TICKER_AGE_AT_SEND:
            raise HourlySnapshotError("Snapshot live price is stale.")
        if asset.ticker.as_of > clock.now + clock.uncertainty + MAX_TICKER_FUTURE:
            raise HourlySnapshotError("Snapshot live price timestamp is in the future.")
        for timeframe in (Timeframe.H1, Timeframe.H4):
            series = asset.candles(timeframe)
            if len(series) < MIN_INDICATOR_CANDLES:
                raise HourlySnapshotError("Snapshot lacks enough closed candles.")
            if any(row.symbol != asset.symbol or row.timeframe is not timeframe for row in series):
                raise HourlySnapshotError("Snapshot candle identity is invalid.")
            if any(
                current.open_time != previous.close_time
                for previous, current in zip(series, series[1:], strict=False)
            ):
                raise HourlySnapshotError("Snapshot candles are not consecutive.")
            if series[-1].close_time != expected_cutoff(reference, timeframe):
                raise HourlySnapshotError("Snapshot closed-candle cutoff is invalid.")
            if timeframe is Timeframe.H1 and series[-1].close_time != close:
                raise OutsideWindow("Snapshot hourly candle is no longer current.")
    return close


@contextmanager
def hourly_lock(path: Path) -> Iterator[bool]:
    """Hold an OS-managed nonblocking process lock, without deleting its file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
        else:
            try:
                yield True
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def main(argv: Sequence[str] | None = None, *, lock_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one eligible hourly Binance snapshot.")
    parser.add_argument("--check", action="store_true", help="validate without sending Telegram")
    arguments = parser.parse_args(argv)
    try:
        with hourly_lock(lock_path or DEFAULT_LOCK_PATH) as acquired:
            if not acquired:
                print("Hourly snapshot skipped: another run is active.")
                return 0
            with requests.Session() as session:
                starting_clock = fetch_server_clock(session)
                eligible_close(starting_clock)
                snapshot = collect_snapshot(BinanceClient(session), SYMBOLS, starting_clock.now)
                message = format_telegram_snapshot(snapshot)
                config = None if arguments.check else load_config()
                final_clock = fetch_server_clock(session)
                validate_snapshot(snapshot, final_clock)
            if arguments.check:
                print("Hourly snapshot is eligible; no message sent.")
                return 0
            assert config is not None
            send_message(message, config)
    except OutsideWindow as error:
        print(f"Hourly snapshot skipped: {error}")
        return 0
    except (
        HourlySnapshotError,
        MarketDataError,
        TelegramSnapshotFormatError,
        TelegramConfigError,
        TelegramDeliveryError,
        OSError,
    ):
        print("Hourly snapshot unavailable; no further send attempt will be made.", file=sys.stderr)
        return 1
    print("Hourly Telegram snapshot delivered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
