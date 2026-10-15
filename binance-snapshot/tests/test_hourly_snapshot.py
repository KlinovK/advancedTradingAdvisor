"""Offline coverage for the opt-in hourly runner and launchd template."""

from __future__ import annotations

import plistlib
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, Mock

import hourly_snapshot
import pytest
import requests
from conftest import candle
from market_data import (
    BASE_URL,
    MARKET_DATA_BASE_URL,
    BinanceRequestError,
    DiagnosticCode,
    FreshnessMarketDataError,
)
from models import AssetMarketData, MarketDataError, MarketSnapshot, Ticker, Timeframe
from snapshot import SYMBOLS
from telegram_notifier import (
    TelegramConfig,
    TelegramConfigError,
    TelegramDeliveryError,
)
from telegram_notifier import send_message as send_telegram_message

CLOSE = datetime(2025, 1, 2, 8, tzinfo=UTC)
REFERENCE = CLOSE + timedelta(minutes=7)
CONFIG = TelegramConfig("123456:FAKE_test-token", "-1001234567890")


def series(symbol: str, timeframe: Timeframe, count: int = 60) -> tuple:
    return tuple(
        candle(symbol, timeframe, CLOSE - (count - index) * timeframe.duration)
        for index in range(count)
    )


def complete_snapshot() -> MarketSnapshot:
    assets = tuple(
        AssetMarketData(
            symbol,
            Ticker(symbol, Decimal(100), Decimal(110), Decimal(90), REFERENCE),
            series(symbol, Timeframe.H1),
            series(symbol, Timeframe.H4),
        )
        for symbol in SYMBOLS
    )
    return MarketSnapshot(REFERENCE, assets)


def replace_asset(snapshot: MarketSnapshot, index: int, asset: AssetMarketData) -> MarketSnapshot:
    assets = list(snapshot.assets)
    assets[index] = asset
    return replace(snapshot, assets=tuple(assets))


def runner_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    snapshot: MarketSnapshot | None = None,
    first: datetime = REFERENCE,
    final: datetime = REFERENCE,
) -> tuple[Mock, Mock, Mock, Mock]:
    session = MagicMock()
    session.__enter__.return_value = session
    monkeypatch.setattr(hourly_snapshot.requests, "Session", lambda: session)
    clocks = Mock(
        side_effect=[hourly_snapshot.ServerClock(first), hourly_snapshot.ServerClock(final)]
    )
    collector = Mock(return_value=snapshot or complete_snapshot())
    loader = Mock(return_value=CONFIG)
    sender = Mock()
    monkeypatch.setattr(hourly_snapshot, "fetch_server_clock", clocks)
    monkeypatch.setattr(hourly_snapshot, "collect_snapshot", collector)
    monkeypatch.setattr(hourly_snapshot, "load_config", loader)
    monkeypatch.setattr(hourly_snapshot, "send_message", sender)
    return clocks, collector, loader, sender


def assert_outcome(
    output: str,
    status: str,
    reason: str,
    *,
    phase: str | None = None,
    human: str = "",
) -> None:
    phase_field = f" phase={phase}" if phase is not None else ""
    assert output == f"{human}SNAPSHOT_OUTCOME status={status} reason={reason}{phase_field}\n"


def test_fresh_complete_snapshot_sends_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clocks, collector, loader, sender = runner_mocks(monkeypatch)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    assert clocks.call_count == 2
    assert collector.call_count == 1
    assert collector.call_args.args[1] == SYMBOLS
    assert collector.call_args.args[2] == REFERENCE
    loader.assert_called_once_with()
    sender.assert_called_once()
    message, config = sender.call_args.args
    assert config == CONFIG
    assert tuple(line for line in message.splitlines() if line in SYMBOLS) == SYMBOLS
    assert "Live prices are ticker values; indicators use closed candles." in message
    assert "1H closed 2025-01-02 08:00 UTC" in message
    assert "4H closed 2025-01-02 08:00 UTC" in message
    assert_outcome(
        capsys.readouterr().out,
        "DELIVERED",
        "telegram_accepted",
        human="Hourly Telegram snapshot delivered.\n",
    )


def test_check_validates_without_loading_config_or_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, loader, sender = runner_mocks(monkeypatch)
    assert hourly_snapshot.main(["--check"], lock_path=tmp_path / "hourly.lock") == 0
    loader.assert_not_called()
    sender.assert_not_called()
    assert_outcome(
        capsys.readouterr().out,
        "SKIPPED",
        "check_only",
        human="Hourly snapshot is eligible; no message sent.\n",
    )


@pytest.mark.parametrize("minute,eligible", [(1, False), (2, True), (15, True), (16, False)])
def test_eligibility_boundaries(minute: int, eligible: bool) -> None:
    clock = hourly_snapshot.ServerClock(CLOSE + timedelta(minutes=minute))
    if eligible:
        assert hourly_snapshot.eligible_close(clock) == CLOSE
    else:
        with pytest.raises(hourly_snapshot.OutsideWindow):
            hourly_snapshot.eligible_close(clock)


def test_clock_uncertainty_cannot_cross_fifteen_minute_boundary() -> None:
    clock = hourly_snapshot.ServerClock(
        CLOSE + timedelta(minutes=14, seconds=59), timedelta(seconds=2)
    )
    with pytest.raises(hourly_snapshot.OutsideWindow):
        hourly_snapshot.eligible_close(clock)


@pytest.mark.parametrize("minute", [1, 16])
def test_outside_window_has_no_collection_or_delivery(
    minute: int,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, collector, loader, sender = runner_mocks(
        monkeypatch, first=CLOSE + timedelta(minutes=minute)
    )
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    collector.assert_not_called()
    loader.assert_not_called()
    sender.assert_not_called()
    assert_outcome(
        capsys.readouterr().out,
        "SKIPPED",
        "outside_freshness_window",
        phase="initial_clock",
        human="Hourly snapshot skipped: No fresh hourly candle is eligible now.\n",
    )


def test_delayed_final_check_after_sleep_skips_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clocks, collector, _, sender = runner_mocks(monkeypatch, final=CLOSE + timedelta(minutes=31))
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    assert clocks.call_count == 2
    collector.assert_called_once()
    sender.assert_not_called()
    assert_outcome(
        capsys.readouterr().out,
        "SKIPPED",
        "outside_freshness_window",
        phase="validation",
        human="Hourly snapshot skipped: No fresh hourly candle is eligible now.\n",
    )


def test_missing_asset_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshot = complete_snapshot()
    snapshot = replace(snapshot, assets=snapshot.assets[:-1])
    _, _, _, sender = runner_mocks(monkeypatch, snapshot=snapshot)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    sender.assert_not_called()


def test_misaligned_hourly_candle_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshot = complete_snapshot()
    asset = snapshot.assets[2]
    prior = series(asset.symbol, Timeframe.H1, 60)
    prior = tuple(
        replace(
            row,
            open_time=row.open_time - timedelta(hours=1),
            close_time=row.close_time - timedelta(hours=1),
        )
        for row in prior
    )
    snapshot = replace_asset(snapshot, 2, replace(asset, hourly=prior))
    _, _, _, sender = runner_mocks(monkeypatch, snapshot=snapshot)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    sender.assert_not_called()


def test_incomplete_indicator_history_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = complete_snapshot()
    asset = snapshot.assets[0]
    snapshot = replace_asset(snapshot, 0, replace(asset, hourly=asset.hourly[-49:]))
    _, _, _, sender = runner_mocks(monkeypatch, snapshot=snapshot)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    sender.assert_not_called()


def test_gap_in_closed_candles_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshot = complete_snapshot()
    asset = snapshot.assets[0]
    snapshot = replace_asset(
        snapshot, 0, replace(asset, hourly=asset.hourly[:10] + asset.hourly[11:])
    )
    _, _, _, sender = runner_mocks(monkeypatch, snapshot=snapshot)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    sender.assert_not_called()


def test_invalid_reference_timestamp_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = replace(complete_snapshot(), reference_time=REFERENCE.replace(tzinfo=None))
    _, _, _, sender = runner_mocks(monkeypatch, snapshot=snapshot)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    sender.assert_not_called()


def test_stale_live_price_rejected_before_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = complete_snapshot()
    asset = snapshot.assets[0]
    snapshot = replace_asset(snapshot, 0, replace(asset, ticker=replace(asset.ticker, as_of=CLOSE)))
    _, _, _, sender = runner_mocks(monkeypatch, snapshot=snapshot)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    sender.assert_not_called()


def test_telegram_failure_is_one_attempt_and_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, _, sender = runner_mocks(monkeypatch)
    sender.side_effect = TelegramDeliveryError("secret response: " + CONFIG.bot_token)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    sender.assert_called_once()
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "telegram_delivery_failure")
    assert output.err == "Hourly snapshot unavailable; no further send attempt will be made.\n"
    assert CONFIG.bot_token not in output.out + output.err
    assert "secret response" not in output.out + output.err


def test_telegram_configuration_failure_has_one_safe_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, _, loader, sender = runner_mocks(monkeypatch)
    loader.side_effect = TelegramConfigError("secret configuration value")
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "telegram_config_failure")
    assert output.err == "Hourly snapshot unavailable; no further send attempt will be made.\n"
    assert "secret" not in output.out + output.err
    sender.assert_not_called()


@pytest.mark.parametrize("delivery_failure", ["rejected", "timeout"])
def test_telegram_rejection_or_ambiguous_timeout_is_failed_without_retry(
    delivery_failure: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner_mocks(monkeypatch)
    session = Mock()
    if delivery_failure == "timeout":
        session.post.side_effect = requests.Timeout("secret token and response body")
    else:
        session.post.return_value.status_code = 403
    monkeypatch.setattr(
        hourly_snapshot,
        "send_message",
        lambda message, config: send_telegram_message(message, config, session=session),
    )

    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "telegram_delivery_failure")
    assert output.err == "Hourly snapshot unavailable; no further send attempt will be made.\n"
    assert CONFIG.bot_token not in output.out + output.err
    assert CONFIG.chat_id not in output.out + output.err
    assert "secret" not in output.out + output.err
    session.post.assert_called_once()


def test_clock_failure_prevents_collection_and_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clocks, collector, loader, sender = runner_mocks(monkeypatch)
    clocks.side_effect = hourly_snapshot.HourlySnapshotError("clock unavailable")
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    collector.assert_not_called()
    loader.assert_not_called()
    sender.assert_not_called()


def test_final_clock_failure_prevents_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clocks, collector, _, sender = runner_mocks(monkeypatch)
    clocks.side_effect = [
        hourly_snapshot.ServerClock(REFERENCE),
        hourly_snapshot.HourlySnapshotError("clock unavailable"),
    ]
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    collector.assert_called_once()
    sender.assert_not_called()


@pytest.mark.parametrize(
    "payload,status,expected",
    [
        ({"serverTime": "bad"}, 200, "invalid"),
        ({"serverTime": -1}, 200, "invalid"),
        ({"serverTime": True}, 200, "invalid"),
        ({"serverTime": 1}, 429, "failed"),
    ],
)
def test_server_clock_rejects_bad_responses(payload: object, status: int, expected: str) -> None:
    session = Mock()
    session.get.return_value.status_code = status
    session.get.return_value.json.return_value = payload
    now = Mock(side_effect=[REFERENCE, REFERENCE])
    with pytest.raises(hourly_snapshot.HourlySnapshotError, match=expected):
        hourly_snapshot.fetch_server_clock(session, now=now)
    session.get.assert_called_once_with("https://api.binance.com/api/v3/time", timeout=(2.0, 3.0))


def test_server_clock_accepts_public_spot_time() -> None:
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = {"serverTime": int(REFERENCE.timestamp() * 1000)}
    now = Mock(side_effect=[REFERENCE, REFERENCE + timedelta(seconds=1)])
    assert hourly_snapshot.fetch_server_clock(session, now=now) == hourly_snapshot.ServerClock(
        REFERENCE, timedelta(seconds=1)
    )


@pytest.mark.parametrize(
    "failure",
    [requests.Timeout(), requests.ConnectionError(), ValueError("invalid JSON")],
)
def test_server_clock_network_and_json_failures_are_safe(failure: Exception) -> None:
    session = Mock()
    if isinstance(failure, ValueError):
        session.get.return_value.status_code = 200
        session.get.return_value.json.side_effect = failure
    else:
        session.get.side_effect = failure
    with pytest.raises(hourly_snapshot.HourlySnapshotError):
        hourly_snapshot.fetch_server_clock(session, now=lambda: REFERENCE)


def test_server_clock_rejects_delayed_response_and_skew() -> None:
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = {"serverTime": int(REFERENCE.timestamp() * 1000)}
    with pytest.raises(hourly_snapshot.HourlySnapshotError, match="delayed"):
        hourly_snapshot.fetch_server_clock(
            session, now=Mock(side_effect=[REFERENCE, REFERENCE + timedelta(seconds=6)])
        )
    session.get.return_value.json.return_value = {
        "serverTime": int((REFERENCE - timedelta(minutes=1)).timestamp() * 1000)
    }
    with pytest.raises(hourly_snapshot.HourlySnapshotError, match="clock differs"):
        hourly_snapshot.fetch_server_clock(session, now=lambda: REFERENCE)


def test_interprocess_lock_skips_concurrent_run_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, collector, _, sender = runner_mocks(monkeypatch)
    lock = tmp_path / "hourly.lock"
    with hourly_snapshot.hourly_lock(lock) as acquired:
        assert acquired
        assert hourly_snapshot.main([], lock_path=lock) == 0
        assert_outcome(
            capsys.readouterr().out,
            "SKIPPED",
            "lock_contended",
            phase="lock",
            human="Hourly snapshot skipped: another run is active.\n",
        )
        collector.assert_not_called()
        sender.assert_not_called()
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl, os, sys; fd=os.open(sys.argv[1], os.O_RDWR); "
                "\ntry: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)"
                "\nexcept BlockingIOError: sys.exit(7)"
                "\nelse: sys.exit(0)",
                str(lock),
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
        assert child.returncode == 7
    with hourly_snapshot.hourly_lock(lock) as acquired:
        assert acquired


def test_launchd_template_is_quiet_and_absolute() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "launchd"
        / "com.konstantinklinov.binance-snapshot.hourly.plist"
    )
    plist = plistlib.loads(path.read_bytes())
    assert plist["StartCalendarInterval"] == {"Minute": 7}
    assert "RunAtLoad" not in plist
    assert "KeepAlive" not in plist
    assert plist["ProgramArguments"] == [
        "/Users/konstantinklinov/Developer/advancedTradingAdvisor/binance-snapshot/.venv/bin/python",
        "/Users/konstantinklinov/Developer/advancedTradingAdvisor/binance-snapshot/hourly_snapshot.py",
    ]
    assert plist["StandardOutPath"].startswith("/Users/konstantinklinov/Library/Logs/")
    assert plist["StandardErrorPath"].startswith("/Users/konstantinklinov/Library/Logs/")


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, DiagnosticCode.HTTP_403),
        (451, DiagnosticCode.HTTP_451),
        (429, DiagnosticCode.HTTP_429),
        (418, DiagnosticCode.HTTP_OTHER),
    ],
)
def test_server_clock_http_diagnostics(status: int, code: DiagnosticCode) -> None:
    session = Mock()
    session.get.return_value.status_code = status
    with pytest.raises(hourly_snapshot.HourlySnapshotError) as caught:
        hourly_snapshot.fetch_server_clock(session, now=lambda: REFERENCE)
    assert caught.value.code is code
    assert caught.value.status_code == status
    session.get.assert_called_once()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (requests.Timeout("secret"), DiagnosticCode.TIMEOUT),
        (requests.ConnectionError("secret"), DiagnosticCode.CONNECTION_ERROR),
    ],
)
def test_server_clock_transport_diagnostics(
    failure: requests.RequestException, code: DiagnosticCode
) -> None:
    session = Mock()
    session.get.side_effect = failure
    with pytest.raises(hourly_snapshot.HourlySnapshotError) as caught:
        hourly_snapshot.fetch_server_clock(session, now=lambda: REFERENCE)
    assert caught.value.code is code
    session.get.assert_called_once()


@pytest.mark.parametrize(
    "payload", [{"serverTime": "bad"}, {"serverTime": -1}, {"serverTime": True}]
)
def test_server_clock_invalid_payload_diagnostic(payload: object) -> None:
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = payload
    with pytest.raises(hourly_snapshot.HourlySnapshotError) as caught:
        hourly_snapshot.fetch_server_clock(session, now=lambda: REFERENCE)
    assert caught.value.code is DiagnosticCode.SERVER_TIME_INVALID


def test_server_clock_skew_diagnostic() -> None:
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = {
        "serverTime": int((REFERENCE - timedelta(minutes=1)).timestamp() * 1000)
    }
    with pytest.raises(hourly_snapshot.HourlySnapshotError) as caught:
        hourly_snapshot.fetch_server_clock(session, now=lambda: REFERENCE)
    assert caught.value.code is DiagnosticCode.SERVER_TIME_INVALID


@pytest.mark.parametrize(
    ("position", "phase"),
    [(0, "initial_clock"), (1, "pre_send_clock")],
)
def test_clock_failure_reports_phase_without_secrets_or_send(
    position: int,
    phase: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    clocks, collector, _, sender = runner_mocks(monkeypatch)
    values: list[object] = [hourly_snapshot.ServerClock(REFERENCE)] * 2
    values[position] = hourly_snapshot.HourlySnapshotError(
        "secret token and response body", DiagnosticCode.HTTP_451, 451
    )
    clocks.side_effect = values
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "http_451", phase=phase)
    assert output.err == (
        f"Hourly snapshot unavailable (phase={phase}, reason=http_451); "
        "no further send attempt will be made.\n"
    )
    assert collector.call_count == position
    sender.assert_not_called()


@pytest.mark.parametrize(
    ("error", "reason", "status"),
    [
        (BinanceRequestError("secret URL", DiagnosticCode.HTTP_403, 403), "http_403", ""),
        (BinanceRequestError("secret URL", DiagnosticCode.HTTP_429, 429), "http_429", ""),
        (
            BinanceRequestError("secret URL", DiagnosticCode.HTTP_OTHER, 503),
            "http_other",
            " status=503",
        ),
        (
            BinanceRequestError("secret URL", DiagnosticCode.CONNECTION_ERROR),
            "connection_error",
            "",
        ),
        (BinanceRequestError("secret URL", DiagnosticCode.TIMEOUT), "timeout", ""),
        (FreshnessMarketDataError("secret URL"), "freshness_failure", ""),
        (MarketDataError("secret URL"), "invalid_market_data", ""),
    ],
)
def test_collection_diagnostics_are_allowlisted_and_prevent_delivery(
    error: Exception,
    reason: str,
    status: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, collector, _, sender = runner_mocks(monkeypatch)
    collector.side_effect = error
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", reason, phase="collection")
    assert output.err == (
        f"Hourly snapshot unavailable (phase=collection, reason={reason}{status}); "
        "no further send attempt will be made.\n"
    )
    assert "secret" not in output.err
    sender.assert_not_called()


def test_other_http_status_must_be_validated_before_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    hourly_snapshot._report_failure("collection", DiagnosticCode.HTTP_OTHER, 700)
    assert "status=" not in capsys.readouterr().err


def test_validation_freshness_diagnostic_prevents_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = complete_snapshot()
    asset = snapshot.assets[0]
    snapshot = replace_asset(snapshot, 0, replace(asset, ticker=replace(asset.ticker, as_of=CLOSE)))
    _, _, _, sender = runner_mocks(monkeypatch, snapshot=snapshot)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "freshness_failure", phase="validation")
    assert "phase=validation, reason=freshness_failure" in output.err
    sender.assert_not_called()


def test_local_lock_io_error_is_safe_and_prevents_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, collector, _, sender = runner_mocks(monkeypatch)
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory")
    assert hourly_snapshot.main([], lock_path=occupied / "hourly.lock") == 1
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "local_io_error", phase="lock")
    assert output.err == (
        "Hourly snapshot unavailable (phase=lock, reason=local_io_error); "
        "no further send attempt will be made.\n"
    )
    collector.assert_not_called()
    sender.assert_not_called()


def test_expected_skip_output_is_preserved(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, collector, _, sender = runner_mocks(monkeypatch, first=CLOSE + timedelta(minutes=1))
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    assert_outcome(
        capsys.readouterr().out,
        "SKIPPED",
        "outside_freshness_window",
        phase="initial_clock",
        human="Hourly snapshot skipped: No fresh hourly candle is eligible now.\n",
    )
    collector.assert_not_called()
    sender.assert_not_called()


def test_skip_never_prints_an_unexpected_exception_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, collector, _, sender = runner_mocks(monkeypatch)
    monkeypatch.setattr(
        hourly_snapshot,
        "eligible_close",
        Mock(side_effect=hourly_snapshot.OutsideWindow("secret token and response body")),
    )
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    output = capsys.readouterr()
    assert_outcome(
        output.out,
        "SKIPPED",
        "outside_freshness_window",
        phase="initial_clock",
        human="Hourly snapshot skipped: No fresh hourly candle is eligible now.\n",
    )
    assert "secret" not in output.out + output.err
    collector.assert_not_called()
    sender.assert_not_called()


def test_clock_uses_official_market_data_endpoint() -> None:
    session = Mock()
    session.get.return_value.status_code = 200
    session.get.return_value.json.return_value = {"serverTime": int(REFERENCE.timestamp() * 1000)}
    clock = hourly_snapshot.fetch_server_clock(
        session, base_url=MARKET_DATA_BASE_URL, now=lambda: REFERENCE
    )
    assert clock.now == REFERENCE
    session.get.assert_called_once_with(f"{MARKET_DATA_BASE_URL}/api/v3/time", timeout=(2.0, 3.0))


def test_runner_pins_one_endpoint_for_both_clocks_and_collection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BINANCE_API_BASE_URL", MARKET_DATA_BASE_URL)
    clocks, collector, _, sender = runner_mocks(monkeypatch)
    calls = 0

    def clock(_session: object, *, base_url: str) -> hourly_snapshot.ServerClock:
        nonlocal calls
        calls += 1
        assert base_url == MARKET_DATA_BASE_URL
        if calls == 1:
            monkeypatch.setenv("BINANCE_API_BASE_URL", BASE_URL)
        return hourly_snapshot.ServerClock(REFERENCE)

    clocks.side_effect = clock
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    assert calls == 2
    assert collector.call_args.args[0]._base_url == MARKET_DATA_BASE_URL
    sender.assert_called_once()


def test_invalid_endpoint_prevents_all_requests_and_telegram_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BINANCE_API_BASE_URL", "https://user:secret@api.binance.com")
    clocks, collector, loader, sender = runner_mocks(monkeypatch)
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "invalid_market_data", phase="initial_clock")
    assert output.err == (
        "Hourly snapshot unavailable (phase=initial_clock, reason=invalid_market_data); "
        "no further send attempt will be made.\n"
    )
    assert "secret" not in output.err
    clocks.assert_not_called()
    collector.assert_not_called()
    loader.assert_not_called()
    sender.assert_not_called()


def test_http_451_from_selected_clock_endpoint_prevents_telegram_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("BINANCE_API_BASE_URL", MARKET_DATA_BASE_URL)
    real_fetch = hourly_snapshot.fetch_server_clock
    _, collector, loader, sender = runner_mocks(monkeypatch)
    monkeypatch.setattr(hourly_snapshot, "fetch_server_clock", real_fetch)
    session = hourly_snapshot.requests.Session()
    session.get.return_value.status_code = 451
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 1
    session.get.assert_called_once_with(f"{MARKET_DATA_BASE_URL}/api/v3/time", timeout=(2.0, 3.0))
    output = capsys.readouterr()
    assert_outcome(output.out, "FAILED", "http_451", phase="initial_clock")
    assert output.err == (
        "Hourly snapshot unavailable (phase=initial_clock, reason=http_451); "
        "no further send attempt will be made.\n"
    )
    collector.assert_not_called()
    loader.assert_not_called()
    sender.assert_not_called()
