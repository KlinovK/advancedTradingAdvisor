"""Offline scheduler tests: UTC policy, durable slots, and the existing analysis pipeline."""

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from test_live_market import intraday_rows, rows
from test_portfolio_risk import CUTOFF, NOW, asset, market, state

from advanced_trading_advisor.analysis.active import ActiveManagementEngine, SetupStatus
from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.analysis.reasoning import ReasoningMode, ReasoningUnavailable
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.analysis.triggers import SwingTriggerEngine
from advanced_trading_advisor.cli.composition import compose_analysis
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.domain.persistence import ConfirmedUpdate, FailureCategory, UpdateType
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.market.errors import (
    MalformedMarketDataError,
    ProviderUnavailableError,
)
from advanced_trading_advisor.market.live import (
    SYMBOL_IDS,
    TRIGGER_OHLC_DAYS,
    CoinGeckoMarketDataProvider,
)
from advanced_trading_advisor.market.models import Candle, MarketSnapshot
from advanced_trading_advisor.market.settings import CoinGeckoSettings
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import TradingSide
from advanced_trading_advisor.scheduler.market import ScheduledMarketProvider
from advanced_trading_advisor.scheduler.models import ScheduledExecution, ScheduledStatus
from advanced_trading_advisor.scheduler.policy import SchedulerPolicy
from advanced_trading_advisor.scheduler.service import Scheduler
from advanced_trading_advisor.scheduler.settings import SchedulerSettings
from advanced_trading_advisor.storage.codecs import SnapshotCodec, UpdateCodec, utc_text
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.maintenance import inspect_database
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)
from advanced_trading_advisor.storage.scheduler import SQLiteSchedulerRepository
from advanced_trading_advisor.storage.schema import (
    DELIVERY_STATEMENTS,
    SCHEDULER_STATEMENTS,
    STATEMENTS,
)


def run(coro):
    return asyncio.run(coro)


class Clock:
    def __init__(self, at=NOW):
        self.at = at

    def now(self):
        return self.at

    async def sleep(self, seconds):
        self.at += timedelta(seconds=seconds)


class Provider:
    def __init__(self, clock=None, outcomes=()):
        self.clock = clock or Clock()
        self.outcomes = list(outcomes)
        self.calls = 0

    async def get_market_snapshot(self, symbols, *, authoritative_cutoff=None):
        self.calls += 1
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        at = self.clock.now()
        cutoff = at.replace(hour=at.hour // 4 * 4, minute=0, second=0, microsecond=0)
        return MarketSnapshot(
            at,
            tuple(
                replace(
                    asset(symbol, "1" if symbol in {"USDC", "USDT"} else "100"),
                    quoted_at=at,
                    latest_closed_candle_at=cutoff,
                )
                for symbol in symbols
            ),
            is_live=True,
        )


async def prepared(path, *, snapshot=None):
    db = Database(path)
    await db.initialize()
    snapshots = SQLiteSnapshotRepository(db)
    confirmed = await ConfirmedStateService(snapshots).initialize(snapshot or state())
    return db, confirmed


def scheduler(db, clock, provider, *, enabled=False, sleep=None, reasoner_factory=None):
    runs = SQLiteAnalysisRunRepository(db)
    return Scheduler(
        SchedulerSettings(enabled=enabled),
        SQLiteSchedulerRepository(db),
        runs,
        lambda record: compose_analysis(
            db,
            now=clock.now,
            provider_factory=lambda: provider,
            reasoner_factory=reasoner_factory or (lambda settings: None),
            ai_override=False if reasoner_factory is None else None,
            scheduled=record,
            sleep=sleep or clock.sleep,
        ),
        now=clock.now,
        sleep=sleep or clock.sleep,
        output=lambda message: None,
    )


@pytest.mark.parametrize(
    "at,boundary,previous,next_run",
    [
        ("2026-09-09T07:30:00+00:00", "04:00", "04:10", "08:10"),
        ("2026-09-09T08:00:00+00:00", "04:00", "04:10", "08:10"),
        ("2026-09-09T08:05:00+00:00", "04:00", "04:10", "08:10"),
        ("2026-09-09T08:10:00+00:00", "08:00", "08:10", "12:10"),
        ("2026-09-09T08:15:00+00:00", "08:00", "08:10", "12:10"),
        ("2026-09-09T00:05:00+00:00", "20:00", "20:10", "00:10"),
        ("2027-01-01T00:05:00+00:00", "20:00", "20:10", "00:10"),
    ],
)
def test_utc_four_hour_windows(at, boundary, previous, next_run):
    policy = SchedulerPolicy(SchedulerSettings())
    value = policy.window(datetime.fromisoformat(at))
    assert value.boundary.strftime("%H:%M") == boundary
    assert value.previous_run.strftime("%H:%M") == previous
    assert value.next_run.strftime("%H:%M") == next_run
    assert policy.expected_cutoff(value.previous_run) == value.boundary
    if at.startswith("2027-01-01"):
        assert value.boundary.date().isoformat() == "2026-12-31"


def test_scheduler_config_and_utc_validation():
    assert SchedulerSettings.from_environment({}) == SchedulerSettings()
    assert SchedulerSettings.from_environment({"SCHEDULER_ENABLED": "true"}).enabled
    with pytest.raises(DomainValidationError):
        SchedulerSettings(interval_hours=2)
    with pytest.raises(DomainValidationError):
        SchedulerSettings(candle_delay_minutes=0)
    with pytest.raises(DomainValidationError):
        SchedulerSettings(max_startup_catchup_age_minutes=61)
    with pytest.raises(DomainValidationError):
        SchedulerSettings.from_environment({"SCHEDULER_ENABLED": "yes"})
    policy = SchedulerPolicy(SchedulerSettings())
    for value in (datetime(2026, 9, 9, 12), NOW.astimezone().replace(tzinfo=None)):
        with pytest.raises(DomainValidationError):
            policy.window(value)
    with pytest.raises(DomainValidationError):
        policy.expected_cutoff(NOW)


def test_catchup_only_current_recent_slot():
    policy = SchedulerPolicy(SchedulerSettings())
    slot = datetime(2026, 9, 9, 8, 10, tzinfo=UTC)
    assert policy.catchup(slot + timedelta(minutes=7)) == slot
    assert policy.catchup(slot + timedelta(minutes=30)) == slot
    assert policy.catchup(slot + timedelta(minutes=31)) is None


def test_schema_v1_migrates_atomically_to_current(tmp_path):
    path = tmp_path / "old.sqlite"
    snapshot = state()
    update = ConfirmedUpdate(uuid4(), UpdateType.INITIAL_STATE, None, snapshot.id, NOW)
    with sqlite3.connect(path) as connection:
        for statement in STATEMENTS[: -len(SCHEDULER_STATEMENTS) - len(DELIVERY_STATEMENTS)]:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_metadata VALUES (1,1)")
        connection.execute(
            "INSERT INTO portfolio_snapshots VALUES (?,?,?,?,?)",
            (str(snapshot.id), utc_text(NOW), SnapshotCodec.encode(snapshot), 1, utc_text(NOW)),
        )
        connection.execute(
            "INSERT INTO confirmed_updates VALUES (?,?,?,?,?,?,?)",
            (
                str(update.update_id),
                update.update_type.value,
                None,
                str(snapshot.id),
                utc_text(NOW),
                UpdateCodec.encode(update),
                1,
            ),
        )
        connection.execute(
            "INSERT INTO current_portfolio VALUES (?,?,?,?)",
            (1, str(snapshot.id), 1, utc_text(NOW)),
        )

    async def scenario():
        db = Database(path)
        await db.initialize()
        await db.initialize()
        async with db.connection() as connection:
            row = await (await connection.execute("SELECT version FROM schema_metadata")).fetchone()
            assert row[0] == 3
            row = await (
                await connection.execute("SELECT COUNT(*) FROM scheduler_executions")
            ).fetchone()
            assert row[0] == 0
        assert (await SQLiteSnapshotRepository(db).get_current()).snapshot == snapshot

    run(scenario())


def test_once_persists_result_and_never_repeats_same_slot(tmp_path):
    async def scenario():
        db, confirmed = await prepared(tmp_path / "advisor.sqlite")
        clock = Clock()
        provider = Provider(clock)
        first = await scheduler(db, clock, provider).once()
        assert first.status is ScheduledStatus.COMPLETED
        assert first.expected_cutoff == CUTOFF
        assert first.analysis_run_id == first.owner_token
        assert provider.calls == 1
        again = await scheduler(db, clock, provider).once()
        assert again == first and provider.calls == 1
        assert (await SQLiteSnapshotRepository(db).get_current()) == confirmed
        stored = await SQLiteAnalysisRunRepository(db).get(first.analysis_run_id)
        assert stored.run.status is AnalysisRunStatus.COMPLETED
        assert stored.run.market_cutoff == CUTOFF
        assert not (await inspect_database(db.path, now=NOW)).failed

    run(scenario())


def test_concurrent_claim_running_failed_and_recovery_policy(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        repo = SQLiteSchedulerRepository(db)
        slot = NOW.replace(minute=10)
        record = ScheduledExecution(slot, CUTOFF, uuid4(), NOW)
        attempts = await asyncio.gather(
            repo.claim(record), repo.claim(replace(record, owner_token=uuid4()))
        )
        assert sorted(attempts) == [False, True]
        assert (await repo.get(slot)).status is ScheduledStatus.RUNNING
        await repo.recover(NOW + timedelta(minutes=16))
        failed = await repo.get(slot)
        assert failed.status is ScheduledStatus.FAILED
        assert failed.failure_category is FailureCategory.INTERRUPTED
        assert failed.analysis_run_id is None
        assert not await repo.claim(replace(record, owner_token=uuid4()))
        assert await repo.get(slot) == failed

    run(scenario())


def test_manual_analysis_lease_skips_scheduler_without_market_call(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        runs = SQLiteAnalysisRunRepository(db)
        snapshot = (await SQLiteSnapshotRepository(db).get_current()).snapshot
        await runs.acquire(AnalysisRun.start(snapshot.id, NOW))
        provider = Provider()
        outcome = await scheduler(db, Clock(), provider).once()
        assert outcome.status is ScheduledStatus.SKIPPED
        assert outcome.failure_category is FailureCategory.ANALYSIS_BUSY
        assert outcome.analysis_run_id is None and provider.calls == 0
        assert not await SQLiteSchedulerRepository(db).claim(
            replace(
                outcome,
                owner_token=uuid4(),
                status=ScheduledStatus.RUNNING,
                finished_at=None,
                failure_category=None,
            )
        )

    run(scenario())


def test_market_retry_is_bounded_before_analysis_and_ai(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        fresh = market(asset(), asset("USDC", "1"))
        stale = replace(
            fresh,
            assets=tuple(
                replace(value, latest_closed_candle_at=CUTOFF - timedelta(hours=4))
                for value in fresh.assets
            ),
        )
        clock = Clock()
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        provider = Provider(outcomes=[stale, fresh])
        done = await scheduler(db, clock, provider, sleep=fake_sleep).once()
        assert done.status is ScheduledStatus.COMPLETED
        assert provider.calls == 2 and sleeps == [30]
        clock.at += timedelta(hours=4)
        provider = Provider(outcomes=[ProviderUnavailableError("private"), fresh])
        # The second slot expects 16:00; the old 12:00 fixture must be refused.
        failed = await scheduler(db, clock, provider, sleep=fake_sleep).once()
        assert failed.status is ScheduledStatus.FAILED
        assert failed.failure_category is FailureCategory.MARKET_VALIDATION
        assert provider.calls == 2 and sleeps == [30, 30]
        assert (
            await SQLiteAnalysisRunRepository(db).get(failed.analysis_run_id)
        ).run.status is AnalysisRunStatus.FAILED

    run(scenario())


def test_permanent_market_error_is_not_retried(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        provider = Provider(outcomes=[MalformedMarketDataError("private")])
        outcome = await scheduler(db, Clock(), provider).once()
        assert outcome.status is ScheduledStatus.FAILED
        assert outcome.failure_category is FailureCategory.MARKET_VALIDATION
        assert provider.calls == 1

    run(scenario())


def test_exhausted_stale_market_never_calls_ai(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-offline-test-only")

    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        stale = replace(
            market(asset(), asset("USDC", "1")),
            assets=tuple(
                replace(value, latest_closed_candle_at=CUTOFF - timedelta(hours=4))
                for value in (asset(), asset("USDC", "1"))
            ),
        )

        class Reasoner:
            model_name = "offline-test-model"
            calls = 0

            async def propose(
                self, snapshot, evidence, risk, policy, active=None, baseline_actions=()
            ):
                self.calls += 1
                return ()

        reasoner = Reasoner()
        provider = Provider(outcomes=[stale, stale])
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        outcome = await scheduler(
            db,
            Clock(),
            provider,
            sleep=fake_sleep,
            reasoner_factory=lambda settings: reasoner,
        ).once()
        assert outcome.status is ScheduledStatus.FAILED
        assert outcome.failure_category is FailureCategory.MARKET_VALIDATION
        assert provider.calls == 2 and waits == [30]
        assert reasoner.calls == 0
        assert not await SQLiteSchedulerRepository(db).claim(
            ScheduledExecution(outcome.scheduled_for, outcome.expected_cutoff, uuid4(), NOW)
        )

    run(scenario())


def test_ai_assisted_and_fallback_both_complete_without_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-offline-test-only")

    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        clock = Clock()
        provider = Provider(clock)

        class Reasoner:
            model_name = "offline-test-model"
            calls = 0

            async def propose(
                self, snapshot, evidence, risk, policy, active=None, baseline_actions=()
            ):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("synthetic provider failure")
                return ActiveManagementEngine().evaluate(snapshot, evidence, risk)[1]

        reasoner = Reasoner()

        def make():
            return scheduler(db, clock, provider, reasoner_factory=lambda settings: reasoner)

        first = await make().once()
        assert first.status is ScheduledStatus.COMPLETED
        first_run = await SQLiteAnalysisRunRepository(db).get(first.analysis_run_id)

        assert (
            AnalysisResultCodec.decode(first_run.result_json).reasoning_mode
            is ReasoningMode.AI_ASSISTED
        )
        clock.at += timedelta(hours=4)
        second = await make().once()
        second_run = await SQLiteAnalysisRunRepository(db).get(second.analysis_run_id)
        assert second.status is ScheduledStatus.COMPLETED
        assert (
            AnalysisResultCodec.decode(second_run.result_json).reasoning_mode
            is ReasoningMode.AI_FALLBACK
        )
        assert reasoner.calls == 2 and provider.calls == 2

    run(scenario())


@pytest.mark.parametrize(
    "category",
    [
        FailureCategory.AI_AUTHENTICATION_PERMISSION,
        FailureCategory.AI_RATE_LIMIT,
        FailureCategory.AI_PROVIDER_SERVER,
        FailureCategory.AI_TRANSPORT,
        FailureCategory.AI_REFUSAL,
        FailureCategory.AI_INCOMPLETE,
        FailureCategory.AI_PROVIDER,
    ],
)
def test_scheduler_provider_fallback_categories_complete_without_retry(
    tmp_path, monkeypatch, category
):
    monkeypatch.setenv("OPENAI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-offline-test-only")

    async def scenario():
        db, _ = await prepared(tmp_path / f"{category.value}.sqlite")
        clock = Clock()
        provider = Provider(clock)

        class Reasoner:
            model_name = "offline-test-model"
            calls = 0

            async def propose(self, *args):
                self.calls += 1
                raise ReasoningUnavailable(category)

        reasoner = Reasoner()
        outcome = await scheduler(
            db, clock, provider, reasoner_factory=lambda settings: reasoner
        ).once()
        stored = await SQLiteAnalysisRunRepository(db).get(outcome.analysis_run_id)
        result = AnalysisResultCodec.decode(stored.result_json)

        assert outcome.status is ScheduledStatus.COMPLETED
        assert result.reasoning_mode is ReasoningMode.AI_FALLBACK
        assert result.configuration.ai_failure_category == category.value
        assert reasoner.calls == 1 and provider.calls == 1

    run(scenario())


def test_two_scheduler_instances_claim_one_slot(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        provider = Provider()
        first, second = await asyncio.gather(
            scheduler(db, Clock(), provider).once(),
            scheduler(db, Clock(), provider).once(),
        )
        assert first.owner_token == second.owner_token
        assert (await SQLiteSchedulerRepository(db).latest()).status is ScheduledStatus.COMPLETED
        assert provider.calls == 1
        assert len(await SQLiteAnalysisRunRepository(db).list_all_recent()) == 1

    run(scenario())


def test_scheduler_market_adapter_does_not_retry_permanent_errors():
    async def scenario():
        provider = Provider(outcomes=[MalformedMarketDataError("invalid schema")])
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        adapter = ScheduledMarketProvider(provider, CUTOFF, sleep=fake_sleep)
        with pytest.raises(MalformedMarketDataError):
            await adapter.get_market_snapshot(("BTC",))
        assert provider.calls == 1 and not waits

    run(scenario())


def test_scheduler_cutoff_excludes_post_cutoff_confirmation_until_later_slot():
    candles = (
        Candle(
            CUTOFF - timedelta(hours=5), Decimal("80"), Decimal("90"), Decimal("79"), Decimal("89")
        ),
        Candle(
            CUTOFF - timedelta(hours=4), Decimal("97"), Decimal("101"), Decimal("96"), Decimal("99")
        ),
        Candle(
            CUTOFF - timedelta(hours=3), Decimal("98"), Decimal("99"), Decimal("93"), Decimal("94")
        ),
        Candle(
            CUTOFF - timedelta(hours=2), Decimal("94"), Decimal("96"), Decimal("93"), Decimal("95")
        ),
    )

    class CutoffProvider:
        def __init__(self):
            self.cutoffs = []

        async def get_market_snapshot(self, symbols, *, authoritative_cutoff=None):
            self.cutoffs.append(authoritative_cutoff)
            return MarketSnapshot(
                NOW,
                (
                    replace(
                        asset("BTC"),
                        latest_closed_candle_at=authoritative_cutoff,
                        closed_1h_candles=tuple(
                            candle for candle in candles if candle.closed_at <= authoritative_cutoff
                        ),
                    ),
                ),
                is_live=True,
            )

    async def scenario():
        provider = CutoffProvider()
        early_cutoff = CUTOFF - timedelta(hours=4)
        early_market = await ScheduledMarketProvider(provider, early_cutoff).get_market_snapshot(
            ("BTC",)
        )
        trigger = SwingTriggerEngine()
        context = trigger.initial_context(level=Decimal("100"), atr=Decimal("10"))
        status, context = trigger.evaluate(
            status=SetupStatus.CANDIDATE,
            context=context,
            direction=TradingSide.SELL,
            candles=early_market.assets[0].closed_1h_candles,
            latest_4h_close=Decimal("100"),
            latest_4h_cutoff=early_cutoff,
            invalidation_price=Decimal("110"),
        )
        assert status is SetupStatus.ZONE_TOUCHED
        assert all(
            candle.closed_at <= early_cutoff for candle in early_market.assets[0].closed_1h_candles
        )

        later_market = await ScheduledMarketProvider(provider, CUTOFF).get_market_snapshot(("BTC",))
        status, context = trigger.evaluate(
            status=status,
            context=context,
            direction=TradingSide.SELL,
            candles=later_market.assets[0].closed_1h_candles,
            latest_4h_close=Decimal("100"),
            latest_4h_cutoff=CUTOFF,
            invalidation_price=Decimal("110"),
        )
        assert status is SetupStatus.CONFIRMED
        assert context.confirmation_at == CUTOFF - timedelta(hours=3)
        assert provider.cutoffs == [early_cutoff, CUTOFF]

    run(scenario())


def test_delayed_scheduler_with_realistic_coingecko_mock_caps_market_and_preserves_portfolio(
    tmp_path,
):
    execution_time = CUTOFF + timedelta(hours=2, minutes=54)
    seen_days = []

    def handler(request):
        if request.url.path.endswith("/simple/price"):
            return httpx.Response(
                200,
                json={
                    identifier: {
                        "usd": 1 if identifier == "usd-coin" else 100,
                        "last_updated_at": int(execution_time.timestamp()),
                    }
                    for identifier in SYMBOL_IDS.values()
                    if identifier in {"bitcoin", "usd-coin"}
                },
            )
        days = request.url.params["days"]
        seen_days.append(days)
        if days == TRIGGER_OHLC_DAYS:
            return httpx.Response(
                200,
                json=intraday_rows(cutoff=execution_time.replace(minute=30)),
            )
        assert days == "30"
        return httpx.Response(200, json=rows(cutoff=CUTOFF))

    async def scenario():
        db, confirmed = await prepared(tmp_path / "advisor.sqlite")
        clock = Clock(execution_time)
        provider = CoinGeckoMarketDataProvider(
            CoinGeckoSettings("offline-test-key"),
            transport=httpx.MockTransport(handler),
            now=clock.now,
        )

        completed = await scheduler(db, clock, provider).once()

        assert completed.status is ScheduledStatus.COMPLETED
        assert completed.expected_cutoff == CUTOFF
        assert await SQLiteSnapshotRepository(db).get_current() == confirmed
        stored = await SQLiteAnalysisRunRepository(db).get(completed.analysis_run_id)
        result = AnalysisResultCodec.decode(stored.result_json)
        assert result.market.effective_market_cutoff == CUTOFF
        assert all(
            candle.closed_at <= CUTOFF
            for value in result.market.assets
            for candle in value.closed_1h_candles
        )
        assert result.reasoning_mode is ReasoningMode.DETERMINISTIC
        assert seen_days.count("1") == 1
        assert "2" not in seen_days

    run(scenario())


def test_completed_analysis_is_reconciled_after_slot_crash(tmp_path):
    async def scenario():
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        repo = SQLiteSchedulerRepository(db)
        record = ScheduledExecution(NOW.replace(minute=10), CUTOFF, uuid4(), NOW)
        assert await repo.claim(record)
        result = await compose_analysis(
            db,
            now=lambda: NOW,
            provider_factory=Provider,
            reasoner_factory=lambda settings: None,
            ai_override=False,
            scheduled=record,
            sleep=lambda seconds: None,
        ).execute()
        assert result.run_id == record.owner_token
        assert (await repo.get(record.scheduled_for)).status is ScheduledStatus.RUNNING
        await repo.recover(NOW + timedelta(minutes=16))
        recovered = await repo.get(record.scheduled_for)
        assert recovered.status is ScheduledStatus.COMPLETED
        assert recovered.analysis_run_id == result.run_id
        assert not await repo.claim(replace(record, owner_token=uuid4()))

    run(scenario())


def test_cancellation_during_analysis_never_completes_slot(tmp_path):
    async def scenario():
        db, confirmed = await prepared(tmp_path / "advisor.sqlite")

        class CancelProvider:
            async def get_market_snapshot(self, symbols, *, authoritative_cutoff=None):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await scheduler(db, Clock(), CancelProvider()).once()
        slot = await SQLiteSchedulerRepository(db).latest()
        assert slot.status is ScheduledStatus.FAILED
        assert slot.failure_category is FailureCategory.INTERRUPTED
        assert slot.analysis_run_id is not None
        assert (
            await SQLiteAnalysisRunRepository(db).get(slot.analysis_run_id)
        ).run.status is AnalysisRunStatus.FAILED
        assert await SQLiteSnapshotRepository(db).get_current() == confirmed

    run(scenario())


def test_foreground_catchup_then_clean_cancellation(tmp_path):
    async def scenario():
        clock = Clock(NOW.replace(minute=17))
        db, _ = await prepared(
            tmp_path / "advisor.sqlite",
            snapshot=replace(state(), confirmed_at=NOW - timedelta(days=1)),
        )
        provider = Provider(clock)

        async def cancel_sleep(seconds):
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await scheduler(db, clock, provider, enabled=True, sleep=cancel_sleep).run()
        assert provider.calls == 1
        assert (await SQLiteSchedulerRepository(db).latest()).status is ScheduledStatus.COMPLETED
        assert len(await SQLiteAnalysisRunRepository(db).list_all_recent()) == 1

    run(scenario())


def test_foreground_old_slot_waits_for_next_boundary_without_backfill(tmp_path):
    async def scenario():
        clock = Clock(NOW.replace(hour=13))
        db, _ = await prepared(tmp_path / "advisor.sqlite")
        provider = Provider(clock)

        async def wait_until_next(seconds):
            await clock.sleep(seconds)
            if clock.now() > NOW.replace(hour=16, minute=10):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await scheduler(db, clock, provider, enabled=True, sleep=wait_until_next).run()
        records = await SQLiteAnalysisRunRepository(db).list_all_recent()
        assert len(records) == 1 and provider.calls == 1
        latest = await SQLiteSchedulerRepository(db).latest()
        assert latest.scheduled_for.hour == 16 and latest.status is ScheduledStatus.COMPLETED

    run(scenario())


def test_cli_status_read_only_once_and_duplicate(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    db_path = tmp_path / "advisor.sqlite"
    output = []
    args = ["--db", str(db_path), "scheduler", "status"]
    assert main(args, output=output.append, now=lambda: NOW) == 0
    assert not db_path.exists()
    assert "Enabled: no" in output[0] and "Next planned run:" in output[0]
    run(prepared(db_path))
    provider = Provider()
    args[-1] = "once"
    assert main(args, output=output.append, now=lambda: NOW, provider_factory=lambda: provider) == 0
    assert main(args, output=output.append, now=lambda: NOW, provider_factory=lambda: provider) == 0
    assert provider.calls == 1
    args[-1] = "status"
    assert main(args, output=output.append, now=lambda: NOW) == 0
    assert "Previous status: COMPLETED" in output[-1]
    args[-1] = "run"
    assert main(args, output=output.append, now=lambda: NOW) == 1
    assert "Scheduler disabled" in output[-1]
