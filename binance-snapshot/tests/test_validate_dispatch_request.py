"""Offline tests for the GitHub on-demand dispatch age gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import validate_dispatch_request as guard

NOW = datetime(2025, 1, 2, 8, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "accepted", "reason"),
    [
        ("2025-01-02T08:29:59Z", True, "recent_request"),
        ("2025-01-02T08:20:00Z", True, "recent_request"),
        ("2025-01-02T08:19:59Z", False, "stale_request"),
        ("2025-01-02T08:30:30Z", True, "recent_request"),
        ("2025-01-02T08:30:31Z", False, "future_request"),
        ("2025-01-02T08:30:00+00:00", False, "invalid_timestamp"),
        ("2025-01-02T08:30:00+03:00", False, "invalid_timestamp"),
        ("2025-02-29T08:30:00Z", False, "invalid_timestamp"),
        ("2025-1-02T08:30:00Z", False, "invalid_timestamp"),
        ("2025-01-02T08:30:60Z", False, "invalid_timestamp"),
        ("2025-01-02T08:30:00Z; echo injected", False, "invalid_timestamp"),
        ("$(echo injected)", False, "invalid_timestamp"),
        ("", False, "missing_timestamp"),
        (None, False, "missing_timestamp"),
    ],
)
def test_classify_request(value: str | None, accepted: bool, reason: str) -> None:
    assert guard.classify_request(value, NOW) == (accepted, reason)


@pytest.mark.parametrize(
    ("value", "expected_line", "expected_output"),
    [
        (
            "2025-01-02T08:20:00Z",
            "SNAPSHOT_REQUEST status=ACCEPTED reason=recent_request\n",
            "should_run=true\n",
        ),
        (
            "2025-01-02T08:19:59Z",
            "SNAPSHOT_REQUEST status=SKIPPED reason=stale_request\n",
            "should_run=false\n",
        ),
        (
            "$(echo injected)",
            "SNAPSHOT_REQUEST status=SKIPPED reason=invalid_timestamp\n",
            "should_run=false\n",
        ),
    ],
)
def test_main_logs_only_safe_result_and_sets_runner_output(
    value: str,
    expected_line: str,
    expected_output: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.setenv("REQUESTED_AT_UTC", value)
    assert guard.main(now=NOW) == 0
    assert capsys.readouterr().out == expected_line
    assert output_path.read_text(encoding="utf-8") == expected_output


def test_missing_timestamp_cannot_enable_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output_path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_path))
    monkeypatch.delenv("REQUESTED_AT_UTC", raising=False)
    assert guard.main(now=NOW) == 0
    assert capsys.readouterr().out == "SNAPSHOT_REQUEST status=SKIPPED reason=missing_timestamp\n"
    assert output_path.read_text(encoding="utf-8") == "should_run=false\n"


def test_output_failure_fails_closed_without_input_leakage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "missing" / "output"))
    monkeypatch.setenv("REQUESTED_AT_UTC", "$(echo injected)")
    assert guard.main(now=NOW) == 1
    output = capsys.readouterr()
    assert output.out == "SNAPSHOT_REQUEST status=FAILED reason=output_unavailable\n"
    assert "injected" not in output.out + output.err


def test_fractional_age_boundary() -> None:
    assert guard.classify_request("2025-01-02T08:20:00Z", NOW + timedelta(microseconds=1)) == (
        False,
        "stale_request",
    )
