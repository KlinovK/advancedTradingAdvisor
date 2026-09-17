from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "advisor-launchd"
LABEL = "com.konstantinklinov.advanced-trading-advisor.scheduler"


def test_launchd_script_has_valid_shell_syntax() -> None:
    result = subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_rendered_launch_agent_runs_existing_scheduler_without_secrets() -> None:
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "render"],
        check=False,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr.decode()
    payload = plistlib.loads(result.stdout)

    assert payload["Label"] == LABEL
    assert payload["ProgramArguments"] == [
        str(PROJECT_ROOT / ".venv" / "bin" / "advisor"),
        "scheduler",
        "run",
    ]
    assert payload["WorkingDirectory"] == str(PROJECT_ROOT)
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ThrottleInterval"] == 30
    assert payload["EnvironmentVariables"] == {"PYTHONUNBUFFERED": "1"}
    assert payload["StandardOutPath"] == str(
        PROJECT_ROOT / ".local" / "launchd" / "scheduler.stdout.log"
    )
    assert payload["StandardErrorPath"] == str(
        PROJECT_ROOT / ".local" / "launchd" / "scheduler.stderr.log"
    )
    assert "StartCalendarInterval" not in payload

    rendered = result.stdout.decode()
    for secret_name in (
        "COINGECKO_API_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "SCHEDULER_ENABLED",
    ):
        assert secret_name not in rendered


def test_launchd_local_state_is_git_ignored() -> None:
    ignore_rules = (PROJECT_ROOT / ".gitignore").read_text()

    assert ".local/" in ignore_rules.splitlines()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS launchd lifecycle integration")
def test_install_refuses_disabled_scheduler_before_writing_user_state(
    tmp_path: Path,
) -> None:
    environment = {**os.environ, "HOME": str(tmp_path), "SCHEDULER_ENABLED": "false"}

    result = subprocess.run(
        ["/bin/bash", str(SCRIPT), "install"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "Scheduler is disabled" in result.stderr
    assert not (tmp_path / "Library" / "LaunchAgents").exists()


def test_lifecycle_uses_modern_launchctl_commands() -> None:
    script = SCRIPT.read_text()

    for command in ("bootstrap", "bootout", "kickstart", "print"):
        assert f"/bin/launchctl {command}" in script


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS launchd lifecycle integration")
def test_install_refuses_macos_privacy_protected_project_location(
    tmp_path: Path,
) -> None:
    protected_project = tmp_path / "Desktop" / "Project With Spaces"
    protected_script = protected_project / "scripts" / "advisor-launchd"
    protected_script.parent.mkdir(parents=True)
    shutil.copy2(SCRIPT, protected_script)
    environment = {**os.environ, "HOME": str(tmp_path), "SCHEDULER_ENABLED": "true"}

    result = subprocess.run(
        ["/bin/bash", str(protected_script), "install"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode == 1
    assert "macOS privacy protection" in result.stderr
    assert "outside Desktop, Documents, and Downloads" in result.stderr
    assert not (tmp_path / "Library" / "LaunchAgents").exists()
