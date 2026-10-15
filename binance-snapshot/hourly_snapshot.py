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
from enum import StrEnum
from pathlib import Path
from typing import Literal

import requests
from market_data import (
    BinanceClient,
    BinanceRequestError,
    DiagnosticCode,
    FreshnessMarketDataError,
    collect_snapshot,
    expected_cutoff,
    http_diagnostic_code,
    resolve_binance_base_url,
)
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

    def __init__(
        self,
        message: str,
        code: DiagnosticCode = DiagnosticCode.SERVER_TIME_INVALID,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


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
    session: requests.Session,
    *,
    base_url: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> ServerClock:
    """Read public Binance Spot time once; fail closed on delay or clock skew."""
    clock = now or (lambda: datetime.now(UTC))
    started = clock()
    selected_base_url = resolve_binance_base_url(base_url)
    try:
        response = session.get(f"{selected_base_url}/api/v3/time", timeout=(2.0, 3.0))
    except requests.Timeout:
        raise HourlySnapshotError(
            "Binance server time is unavailable.", DiagnosticCode.TIMEOUT
        ) from None
    except requests.RequestException:
        raise HourlySnapshotError(
            "Binance server time is unavailable.", DiagnosticCode.CONNECTION_ERROR
        ) from None
    finished = clock()
    elapsed = finished - started
    if elapsed < timedelta(0) or elapsed > MAX_TIME_REQUEST:
        raise HourlySnapshotError("Binance server time response was delayed.")
    if response.status_code != 200:
        raise HourlySnapshotError(
            "Binance server time request failed.",
            http_diagnostic_code(response.status_code),
            response.status_code,
        )
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
        raise HourlySnapshotError(
            "Snapshot reference time is invalid.", DiagnosticCode.INVALID_MARKET_DATA
        )
    if reference > clock.now + clock.uncertainty + MAX_TICKER_FUTURE:
        raise HourlySnapshotError(
            "Snapshot reference time is in the future.", DiagnosticCode.FRESHNESS_FAILURE
        )
    if len(snapshot.assets) != len(SYMBOLS) or {asset.symbol for asset in snapshot.assets} != set(
        SYMBOLS
    ):
        raise HourlySnapshotError(
            "Snapshot does not contain the six required assets.", DiagnosticCode.INVALID_MARKET_DATA
        )

    for asset in snapshot.assets:
        if asset.ticker.symbol != asset.symbol:
            raise HourlySnapshotError(
                "Snapshot ticker symbol does not match its asset.",
                DiagnosticCode.INVALID_MARKET_DATA,
            )
        if clock.now + clock.uncertainty - asset.ticker.as_of > MAX_TICKER_AGE_AT_SEND:
            raise HourlySnapshotError(
                "Snapshot live price is stale.", DiagnosticCode.FRESHNESS_FAILURE
            )
        if asset.ticker.as_of > clock.now + clock.uncertainty + MAX_TICKER_FUTURE:
            raise HourlySnapshotError(
                "Snapshot live price timestamp is in the future.",
                DiagnosticCode.FRESHNESS_FAILURE,
            )
        for timeframe in (Timeframe.H1, Timeframe.H4):
            series = asset.candles(timeframe)
            if len(series) < MIN_INDICATOR_CANDLES:
                raise HourlySnapshotError(
                    "Snapshot lacks enough closed candles.", DiagnosticCode.INVALID_MARKET_DATA
                )
            if any(row.symbol != asset.symbol or row.timeframe is not timeframe for row in series):
                raise HourlySnapshotError(
                    "Snapshot candle identity is invalid.", DiagnosticCode.INVALID_MARKET_DATA
                )
            if any(
                current.open_time != previous.close_time
                for previous, current in zip(series, series[1:], strict=False)
            ):
                raise HourlySnapshotError(
                    "Snapshot candles are not consecutive.", DiagnosticCode.FRESHNESS_FAILURE
                )
            if series[-1].close_time != expected_cutoff(reference, timeframe):
                raise HourlySnapshotError(
                    "Snapshot closed-candle cutoff is invalid.", DiagnosticCode.FRESHNESS_FAILURE
                )
            if timeframe is Timeframe.H1 and series[-1].close_time != close:
                raise OutsideWindow("Snapshot hourly candle is no longer current.")
    return close


Phase = Literal["lock", "initial_clock", "collection", "pre_send_clock", "validation"]


class OutcomeStatus(StrEnum):
    DELIVERED = "DELIVERED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


class OutcomeReason(StrEnum):
    TELEGRAM_ACCEPTED = "telegram_accepted"
    CHECK_ONLY = "check_only"
    LOCK_CONTENDED = "lock_contended"
    OUTSIDE_FRESHNESS_WINDOW = "outside_freshness_window"
    TELEGRAM_CONFIG_FAILURE = "telegram_config_failure"
    TELEGRAM_DELIVERY_FAILURE = "telegram_delivery_failure"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    status: OutcomeStatus
    reason: OutcomeReason | DiagnosticCode
    phase: Phase | None = None

    @property
    def exit_code(self) -> int:
        return 1 if self.status is OutcomeStatus.FAILED else 0


def _report_outcome(outcome: RunOutcome) -> None:
    """Emit one allowlisted, machine-readable line without exception content."""
    phase = f" phase={outcome.phase}" if outcome.phase is not None else ""
    print(f"SNAPSHOT_OUTCOME status={outcome.status.value} reason={outcome.reason.value}{phase}")


def _report_failure(phase: Phase, code: DiagnosticCode, status_code: int | None = None) -> None:
    """Write only controlled fields; never interpolate an exception or response."""
    status = (
        f" status={status_code}"
        if code is DiagnosticCode.HTTP_OTHER
        and type(status_code) is int
        and 100 <= status_code <= 599
        else ""
    )
    print(
        f"Hourly snapshot unavailable (phase={phase}, reason={code.value}{status}); "
        "no further send attempt will be made.",
        file=sys.stderr,
    )


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


def _run(*, check: bool, lock_path: Path | None) -> RunOutcome:
    phase: Phase = "lock"
    try:
        with hourly_lock(lock_path or DEFAULT_LOCK_PATH) as acquired:
            if not acquired:
                print("Hourly snapshot skipped: another run is active.")
                return RunOutcome(OutcomeStatus.SKIPPED, OutcomeReason.LOCK_CONTENDED, phase)
            with requests.Session() as session:
                phase = "initial_clock"
                base_url = resolve_binance_base_url()
                starting_clock = fetch_server_clock(session, base_url=base_url)
                eligible_close(starting_clock)
                phase = "collection"
                snapshot = collect_snapshot(
                    BinanceClient(session, base_url=base_url), SYMBOLS, starting_clock.now
                )
                message = format_telegram_snapshot(snapshot)
                config = None if check else load_config()
                phase = "pre_send_clock"
                final_clock = fetch_server_clock(session, base_url=base_url)
                phase = "validation"
                validate_snapshot(snapshot, final_clock)
            if check:
                print("Hourly snapshot is eligible; no message sent.")
                return RunOutcome(OutcomeStatus.SKIPPED, OutcomeReason.CHECK_ONLY)
            assert config is not None
            send_message(message, config)
    except OutsideWindow as error:
        message = (
            "Snapshot hourly candle is no longer current."
            if str(error) == "Snapshot hourly candle is no longer current."
            else "No fresh hourly candle is eligible now."
        )
        print(f"Hourly snapshot skipped: {message}")
        return RunOutcome(OutcomeStatus.SKIPPED, OutcomeReason.OUTSIDE_FRESHNESS_WINDOW, phase)
    except HourlySnapshotError as error:
        _report_failure(phase, error.code, error.status_code)
        return RunOutcome(OutcomeStatus.FAILED, error.code, phase)
    except BinanceRequestError as error:
        _report_failure(phase, error.code, error.status_code)
        return RunOutcome(OutcomeStatus.FAILED, error.code, phase)
    except FreshnessMarketDataError:
        _report_failure(phase, DiagnosticCode.FRESHNESS_FAILURE)
        return RunOutcome(OutcomeStatus.FAILED, DiagnosticCode.FRESHNESS_FAILURE, phase)
    except (MarketDataError, TelegramSnapshotFormatError):
        _report_failure(phase, DiagnosticCode.INVALID_MARKET_DATA)
        return RunOutcome(OutcomeStatus.FAILED, DiagnosticCode.INVALID_MARKET_DATA, phase)
    except OSError:
        _report_failure(phase, DiagnosticCode.LOCAL_IO_ERROR)
        return RunOutcome(OutcomeStatus.FAILED, DiagnosticCode.LOCAL_IO_ERROR, phase)
    except TelegramConfigError:
        print("Hourly snapshot unavailable; no further send attempt will be made.", file=sys.stderr)
        return RunOutcome(OutcomeStatus.FAILED, OutcomeReason.TELEGRAM_CONFIG_FAILURE)
    except TelegramDeliveryError:
        print("Hourly snapshot unavailable; no further send attempt will be made.", file=sys.stderr)
        return RunOutcome(OutcomeStatus.FAILED, OutcomeReason.TELEGRAM_DELIVERY_FAILURE)
    print("Hourly Telegram snapshot delivered.")
    return RunOutcome(OutcomeStatus.DELIVERED, OutcomeReason.TELEGRAM_ACCEPTED)


def main(argv: Sequence[str] | None = None, *, lock_path: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one eligible hourly Binance snapshot.")
    parser.add_argument("--check", action="store_true", help="validate without sending Telegram")
    arguments = parser.parse_args(argv)
    outcome = _run(check=arguments.check, lock_path=lock_path)
    _report_outcome(outcome)
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
