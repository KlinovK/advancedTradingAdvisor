"""Real disposable DB concurrency; market/AI/Telegram are never contacted."""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_dispatch import cloud_environment
from test_portfolio_risk import CUTOFF, NOW, state
from test_postgres_storage import isolated_postgres_schema
from test_scheduler import Clock, Provider

from advanced_trading_advisor.analysis.models import AnalysisRunStatus
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.cli import dispatch
from advanced_trading_advisor.cli.main import CLI
from advanced_trading_advisor.delivery.models import DeliveryStatus
from advanced_trading_advisor.domain.persistence import ConflictError
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.scheduler.models import ScheduledExecution, ScheduledStatus
from advanced_trading_advisor.scheduler.service import Scheduler
from advanced_trading_advisor.scheduler.settings import SchedulerSettings
from advanced_trading_advisor.storage.factory import create_storage
from advanced_trading_advisor.storage.settings import StorageSettings
from advanced_trading_advisor.telegram.settings import TelegramSettings

TEST_URL = os.environ.get("ADVISOR_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(not TEST_URL, reason="ADVISOR_TEST_POSTGRES_URL is not configured"),
]


def test_concurrent_cloud_dispatch_once_and_delivery_uniqueness(monkeypatch):
    async def scenario():
        environment = cloud_environment() | {
            "ADVISOR_DATABASE_URL": TEST_URL,
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": "123:" + "offline" * 4,
            "TELEGRAM_CHAT_ID": "12345",
        }
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        async with isolated_postgres_schema() as (_, schema):
            storage = create_storage(
                StorageSettings.from_environment(environment, sqlite_path=Path("unused.sqlite")),
                postgres_schema=schema,
            )
            await storage.initialize()
            confirmed = await ConfirmedStateService(storage.snapshots).initialize(state())
            monkeypatch.setattr(dispatch, "create_storage", lambda _settings: storage)
            clock, provider = Clock(), Provider()
            entered, release = asyncio.Event(), asyncio.Event()

            class HeldProvider:
                async def get_market_snapshot(self, *args, **kwargs):
                    entered.set()
                    await release.wait()
                    return await provider.get_market_snapshot(*args, **kwargs)

            class Sender:
                calls = 0

                async def send(self, _message):
                    slot = await storage.scheduler.latest()
                    assert slot.status is ScheduledStatus.COMPLETED
                    stored = await storage.analyses.get(slot.analysis_run_id)
                    assert stored.run.status is AnalysisRunStatus.COMPLETED
                    assert AnalysisResultCodec.decode(stored.result_json).market_cutoff == CUTOFF
                    self.calls += 1

            sender = Sender()
            output = []
            cli = CLI(
                now=clock.now,
                output=output.append,
                provider_factory=HeldProvider,
                telegram_sender_factory=lambda settings: sender,
            )
            first = asyncio.create_task(dispatch.run(cli))
            try:
                await asyncio.wait_for(entered.wait(), timeout=10)
                # Independent connections try the same slot while the first owner is active.
                await asyncio.wait_for(dispatch.run(cli), timeout=10)
                assert output[-1] == "Dispatch: already_claimed"
                manual = Scheduler(
                    SchedulerSettings(),
                    storage.scheduler,
                    storage.analyses,
                    None,
                    now=clock.now,
                    output=lambda _: None,
                )
                assert (await manual.once()).status is ScheduledStatus.RUNNING
            finally:
                release.set()
                await first
            await dispatch.run(cli)
            assert output[-1] == "Dispatch: already_terminal"
            assert provider.calls == sender.calls == 1
            assert len(await storage.analyses.list_all_recent()) == 1
            slot = await storage.scheduler.latest()
            assert slot.expected_cutoff == CUTOFF
            settings = TelegramSettings.from_environment(environment)
            saved = await storage.deliveries.get(slot.analysis_run_id, settings.destination_key)
            assert saved.status is DeliveryStatus.DELIVERED and saved.attempt_count == 1
            async with storage.database.connection() as c:
                assert await c.fetchval("SELECT count(*) FROM scheduler_executions") == 1
                assert await c.fetchval("SELECT count(*) FROM telegram_deliveries") == 1
            assert await storage.snapshots.get_current() == confirmed

    asyncio.run(scenario())


def test_dispatch_recovers_stale_owner_without_reclaim_or_analysis():
    async def scenario():
        environment = cloud_environment() | {"ADVISOR_DATABASE_URL": TEST_URL}
        async with isolated_postgres_schema() as (_, schema):
            storage = create_storage(
                StorageSettings.from_environment(environment, sqlite_path=Path("unused.sqlite")),
                postgres_schema=schema,
            )
            await storage.initialize()
            slot = NOW.replace(minute=10)
            original = ScheduledExecution(slot, CUTOFF, uuid4(), slot)
            assert await storage.scheduler.claim(original)
            at = slot + timedelta(minutes=16)
            subject = Scheduler(
                SchedulerSettings(),
                storage.scheduler,
                storage.analyses,
                None,
                now=lambda: at,
                output=lambda _: None,
            )
            assert await subject.dispatch() == "already_terminal"
            saved = await storage.scheduler.get(slot)
            assert saved.status is ScheduledStatus.FAILED
            assert saved.owner_token == original.owner_token
            with pytest.raises(ConflictError):
                await storage.scheduler.finish(original, at, ScheduledStatus.COMPLETED)
            assert await subject.dispatch() == "already_terminal"
            assert not await storage.analyses.list_all_recent()

    asyncio.run(scenario())
