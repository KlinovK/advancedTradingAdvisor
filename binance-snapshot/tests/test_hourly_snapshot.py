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
from models import AssetMarketData, MarketSnapshot, Ticker, Timeframe
from snapshot import SYMBOLS
from telegram_notifier import TelegramConfig, TelegramDeliveryError

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
    assert capsys.readouterr().out == "Hourly Telegram snapshot delivered.\n"


def test_check_validates_without_loading_config_or_sending(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, _, loader, sender = runner_mocks(monkeypatch)
    assert hourly_snapshot.main(["--check"], lock_path=tmp_path / "hourly.lock") == 0
    loader.assert_not_called()
    sender.assert_not_called()


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
    minute: int, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, collector, loader, sender = runner_mocks(
        monkeypatch, first=CLOSE + timedelta(minutes=minute)
    )
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    collector.assert_not_called()
    loader.assert_not_called()
    sender.assert_not_called()


def test_delayed_final_check_after_sleep_skips_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clocks, collector, _, sender = runner_mocks(monkeypatch, final=CLOSE + timedelta(minutes=31))
    assert hourly_snapshot.main([], lock_path=tmp_path / "hourly.lock") == 0
    assert clocks.call_count == 2
    collector.assert_called_once()
    sender.assert_not_called()


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
    assert CONFIG.bot_token not in output.out + output.err
    assert "secret response" not in output.out + output.err


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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, collector, _, sender = runner_mocks(monkeypatch)
    lock = tmp_path / "hourly.lock"
    with hourly_snapshot.hourly_lock(lock) as acquired:
        assert acquired
        assert hourly_snapshot.main([], lock_path=lock) == 0
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
