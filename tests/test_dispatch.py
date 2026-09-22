"""Finite dispatch tests use synthetic state, injected UTC clocks and no providers."""

import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from test_portfolio_risk import CUTOFF, NOW, state
from test_scheduler import Clock, Provider, prepared, scheduler

from advanced_trading_advisor.cli import dispatch
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.delivery.models import DeliveryStatus
from advanced_trading_advisor.domain.persistence import FailureCategory
from advanced_trading_advisor.market.errors import MalformedMarketDataError
from advanced_trading_advisor.scheduler.models import ScheduledExecution, ScheduledStatus
from advanced_trading_advisor.scheduler.service import Scheduler
from advanced_trading_advisor.scheduler.settings import SchedulerSettings
from advanced_trading_advisor.storage.repositories import SQLiteAnalysisRunRepository
from advanced_trading_advisor.storage.scheduler import SQLiteSchedulerRepository


def cloud_environment():
    return {
        "ADVISOR_STORAGE_BACKEND": "postgres",
        "ADVISOR_DATABASE_URL": "postgresql://synthetic:sentinel@db.invalid/test",
        "COINGECKO_API_KEY": "offline-test-only",
        "COINGECKO_API_TIER": "demo",
        "OPENAI_ENABLED": "false",
        "TELEGRAM_ENABLED": "false",
        "SCHEDULER_ENABLED": "true",
    }


@pytest.mark.parametrize(
    "time,eligible",
    [
        ("08:09:00", False),
        ("08:10:00", True),
        ("08:17:00", True),
        ("08:40:00", True),
        ("08:40:00.000001", False),
        ("11:59:00", False),
    ],
)
def test_dispatch_uses_single_bounded_slot(time, eligible):
    at = datetime.fromisoformat(f"2026-09-22T{time}+00:00")
    subject = Scheduler(SchedulerSettings(), None, None, None, now=lambda: at)

    async def once(scheduled_for, *, owner_token):
        assert scheduled_for == at.replace(hour=8, minute=10, second=0, microsecond=0)
        assert (
            subject.policy.expected_cutoff(scheduled_for).isoformat() == "2026-09-22T08:00:00+00:00"
        )
        return SimpleNamespace(owner_token=owner_token, status=ScheduledStatus.COMPLETED)

    subject.once = AsyncMock(side_effect=once)
    assert asyncio.run(subject.dispatch()) == ("completed" if eligible else "no_eligible_slot")
    assert subject.once.await_count == int(eligible)


def test_dispatch_respects_configured_catchup():
    at = NOW.replace(minute=17)
    subject = Scheduler(
        SchedulerSettings(max_startup_catchup_age_minutes=5), None, None, None, now=lambda: at
    )
    subject.once = AsyncMock()
    assert asyncio.run(subject.dispatch()) == "no_eligible_slot"
    subject.once.assert_not_awaited()


def test_dispatch_completion_retry_and_manual_duplicate(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "synthetic.sqlite")
        provider = Provider()
        subject = scheduler(db, Clock(), provider)
        delivered = AsyncMock()
        subject.on_completed = delivered
        assert await subject.dispatch() == "completed"
        assert await subject.dispatch() == "already_terminal"
        assert (await subject.once()).status is ScheduledStatus.COMPLETED
        assert provider.calls == 1 and delivered.await_count == 1
        assert len(await SQLiteAnalysisRunRepository(db).list_all_recent()) == 1
        assert (await SQLiteSchedulerRepository(db).latest()).expected_cutoff == CUTOFF

    asyncio.run(scenario())


def test_dispatch_failure_is_not_retried(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "synthetic.sqlite")
        provider = Provider(outcomes=[MalformedMarketDataError("sentinel")])
        subject = scheduler(db, Clock(), provider)
        assert await subject.dispatch() == "failed"
        assert await subject.dispatch() == "already_terminal"
        assert provider.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("stale", [False, True])
def test_dispatch_active_and_stale_lease_never_replays(tmp_path, stale):
    async def scenario():
        db, _ = await prepared(
            tmp_path / "synthetic.sqlite",
            snapshot=replace(state(), confirmed_at=NOW - timedelta(days=1)),
        )
        repo = SQLiteSchedulerRepository(db)
        slot = NOW.replace(minute=10)
        record = ScheduledExecution(slot, CUTOFF, uuid4(), slot)
        assert await repo.claim(record)
        clock = Clock(slot + timedelta(minutes=16 if stale else 1))
        provider = Provider(clock)
        assert await scheduler(db, clock, provider).dispatch() == (
            "already_terminal" if stale else "already_claimed"
        )
        saved = await repo.get(slot)
        assert saved.owner_token == record.owner_token
        assert saved.status is (ScheduledStatus.FAILED if stale else ScheduledStatus.RUNNING)
        if stale:
            assert saved.failure_category is FailureCategory.INTERRUPTED
        assert provider.calls == 0

    asyncio.run(scenario())


@pytest.fixture
def cloud_env(monkeypatch):
    for key, value in cloud_environment().items():
        monkeypatch.setenv(key, value)
    return cloud_environment()


def test_cloud_noop_ignores_dotenv_and_never_connects(cloud_env, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENAI_ENABLED=true\nOPENAI_MODEL=bad model\n")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Must not connect or load dotenv")

    monkeypatch.setattr(dispatch, "create_storage", forbidden)
    for module in ("ai", "market", "telegram", "scheduler", "storage"):
        monkeypatch.setattr(
            f"advanced_trading_advisor.{module}.settings.local_environment", forbidden
        )
    output = []
    assert (
        main(["scheduler", "dispatch"], now=lambda: NOW.replace(hour=11), output=output.append) == 0
    )
    assert output == ["Dispatch: no_eligible_slot"]


@pytest.mark.parametrize(
    "key,value",
    [
        ("ADVISOR_STORAGE_BACKEND", "sqlite"),
        ("ADVISOR_DATABASE_URL", ""),
        ("COINGECKO_API_KEY", ""),
        ("COINGECKO_API_TIER", "pro"),
        ("MARKET_DATA_TIMEOUT_SECONDS", "invalid"),
        ("OPENAI_ENABLED", "invalid"),
        ("OPENAI_ENABLED", "true"),
        ("TELEGRAM_ENABLED", "true"),
        ("SCHEDULER_ENABLED", "false"),
        ("SCHEDULER_INTERVAL_HOURS", "1"),
        ("SCHEDULER_CANDLE_DELAY_MINUTES", "0"),
        ("SCHEDULER_MAX_STARTUP_CATCHUP_AGE_MINUTES", "61"),
    ],
)
def test_cloud_preflight_fails_before_storage(cloud_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)

    def forbidden(*_args):
        raise AssertionError("Preflight must run before storage")

    monkeypatch.setattr(dispatch, "create_storage", forbidden)
    output = []
    assert main(["scheduler", "dispatch"], now=lambda: NOW, output=output.append) == 1
    assert output == ["Error: Cloud dispatch configuration invalid; check required settings."]


@pytest.mark.parametrize(
    "outcome,code",
    [
        ("completed", 0),
        ("already_terminal", 0),
        ("already_claimed", 0),
        ("no_eligible_slot", 0),
        ("skipped", 0),
        ("failed", 1),
        ("exception", 1),
        ("missing_portfolio", 1),
        ("delivery_success", 0),
        ("delivery_failure", 1),
        ("delivery_exception", 1),
    ],
)
def test_cloud_cli_exits_safely_and_readiness_is_readonly(cloud_env, monkeypatch, outcome, code):
    @asynccontextmanager
    async def transaction(*, readonly):
        assert readonly is True
        yield

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(transaction=transaction)

    storage = SimpleNamespace(
        database=SimpleNamespace(connection=connection, _validate_schema=AsyncMock()),
        snapshots=SimpleNamespace(get_current=AsyncMock(return_value=object())),
        scheduler=None,
        analyses=None,
    )
    monkeypatch.setattr(dispatch, "create_storage", lambda _settings: storage)
    if outcome == "missing_portfolio":
        storage.snapshots.get_current.return_value = None
    if outcome.startswith("delivery_"):
        monkeypatch.setenv("TELEGRAM_ENABLED", "true")
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:" + "offline" * 4)
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        delivery = AsyncMock(
            return_value=SimpleNamespace(
                status=(
                    DeliveryStatus.DELIVERED
                    if outcome == "delivery_success"
                    else DeliveryStatus.FAILED
                )
            )
        )
        if outcome == "delivery_exception":
            delivery.side_effect = RuntimeError("sentinel raw response")
        monkeypatch.setattr(
            dispatch,
            "compose_delivery",
            lambda *_args, **_kwargs: SimpleNamespace(deliver=delivery),
        )

    async def execute(_self):
        assert outcome != "missing_portfolio", "Must fail readiness before dispatch"
        if outcome == "exception":
            raise RuntimeError("sentinel secret raw response")
        if outcome.startswith("delivery_"):
            # once intentionally retains durable completion even if its delivery hook raises.
            try:
                await _self.on_completed(uuid4())
            except Exception:
                pass
            return "completed"
        return outcome

    monkeypatch.setattr(Scheduler, "dispatch", execute)
    output = []
    assert main(["scheduler", "dispatch"], now=lambda: NOW, output=output.append) == code
    assert not any("sentinel" in text or "raw response" in text for text in output)
    storage.database._validate_schema.assert_awaited_once()


def test_workflow_is_manual_only_and_secret_scoped():
    text = (Path(__file__).parents[1] / ".github/workflows/advisor-scheduler.yml").read_text()
    assert "  workflow_dispatch:" in text
    assert "schedule:" not in text
    assert "contents: read" in text and "cancel-in-progress: false" in text
    assert 'python-version: "3.13"' in text
    assert "run: .venv/bin/advisor scheduler dispatch" in text
    assert "scheduler once" not in text and "scheduler run" not in text
    for secret in (
        "ADVISOR_DATABASE_URL",
        "COINGECKO_API_KEY",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ):
        assert f"{secret}: ${{{{ secrets.{secret} }}}}" in text
    assert ".env" not in text and "upload-artifact" not in text
    # Validate the committed ordinary settings, without a YAML runtime dependency or secrets.
    environment = {
        key: value.strip('"')
        for key, value in re.findall(r"^          ([A-Z_]+): (.+)$", text, re.M)
        if not value.startswith("${{")
    }
    environment.update(
        {
            "ADVISOR_DATABASE_URL": cloud_environment()["ADVISOR_DATABASE_URL"],
            "COINGECKO_API_KEY": "offline-test-only",
            "OPENAI_API_KEY": "offline-test-only",
            "TELEGRAM_BOT_TOKEN": "123:" + "offline" * 4,
            "TELEGRAM_CHAT_ID": "12345",
        }
    )
    dispatch.preflight(environment)
