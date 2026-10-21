"""Protect the delivery workflows' independent concurrency policies."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


def _load(name: str) -> dict[str, Any]:
    # BaseLoader treats GitHub's `on` key as a string, unlike YAML 1.1 bool loaders.
    result: dict[str, Any] = yaml.load(
        (WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader
    )
    return result


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [item for child in value.values() for item in _strings(child)]
    if isinstance(value, list):
        return [item for child in value for item in _strings(child)]
    return []


def test_on_demand_concurrency_cannot_displace_scheduled_delivery() -> None:
    scheduled = _load("binance-hourly-scheduled.yml")
    manual = _load("binance-hourly-snapshot.yml")
    on_demand = _load("binance-on-demand-snapshot.yml")

    assert scheduled["jobs"]["deliver"]["concurrency"] == {
        "group": "binance-snapshot-telegram-delivery",
        "cancel-in-progress": "false",
    }
    assert manual["jobs"]["deliver"]["concurrency"] == {
        "group": "binance-snapshot-telegram-delivery",
        "cancel-in-progress": "false",
    }
    assert on_demand["jobs"]["deliver"]["concurrency"] == {
        "group": "binance-snapshot-telegram-on-demand",
        "cancel-in-progress": "false",
    }
    assert "concurrency" not in on_demand


def test_on_demand_workflow_is_manual_main_only_and_keeps_secrets_step_scoped() -> None:
    workflow = _load("binance-on-demand-snapshot.yml")
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert set(workflow["on"]["workflow_dispatch"]["inputs"]) == {"requested_at_utc"}
    job = workflow["jobs"]["deliver"]
    assert job["if"] == (
        "${{ github.event_name == 'workflow_dispatch' && github.ref == 'refs/heads/main' }}"
    )
    delivery_steps = [
        step
        for step in job["steps"]
        if step.get("run") == "python hourly_snapshot.py --mode on-demand"
    ]
    assert len(delivery_steps) == 1
    delivery = delivery_steps[0]
    assert delivery["if"] == "${{ steps.request_guard.outputs.should_run == 'true' }}"
    assert delivery["env"] == {
        "TELEGRAM_BOT_TOKEN": "${{ secrets.BINANCE_SNAPSHOT_TELEGRAM_BOT_TOKEN }}",
        "TELEGRAM_CHAT_ID": "${{ secrets.BINANCE_SNAPSHOT_TELEGRAM_CHAT_ID }}",
    }
    secret_references = sorted(value for value in _strings(workflow) if "secrets." in value)
    assert secret_references == sorted(delivery["env"].values())
