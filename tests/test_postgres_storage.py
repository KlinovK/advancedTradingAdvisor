import asyncio
import importlib
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.delivery.models import DeliveryRecord, DeliveryStatus
from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    FailureCategory,
    PersistenceError,
    UpdateType,
)
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import (
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    FinancialSettings,
    Portfolio,
)
from advanced_trading_advisor.scheduler.models import ScheduledExecution, ScheduledStatus
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.factory import create_storage
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_repositories import (
    ANALYSIS_LOCK,
    DELIVERY_LOCK,
    PORTFOLIO_LOCK,
    SCHEDULER_LOCK,
)
from advanced_trading_advisor.storage.postgres_schema import (
    POSTGRES_COLUMNS,
    POSTGRES_MIGRATIONS,
    POSTGRES_SCHEMA_VERSION,
)
from advanced_trading_advisor.storage.settings import StorageBackend, StorageSettings

NOW = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)


def pg_snapshot(at=NOW):
    return ConfirmedPortfolioSnapshot(
        uuid4(),
        at,
        Portfolio(
            (
                AssetPosition("BTC", Decimal("0.019800000000000000")),
                AssetPosition("USDC", Decimal("3450.00")),
            )
        ),
        FinancialSettings(Decimal("250"), Decimal("12")),
    )


def test_storage_configuration_defaults_validation_and_secret_safe_repr(tmp_path):
    default = StorageSettings.from_environment({}, sqlite_path=tmp_path / "advisor.sqlite")
    assert default.backend is StorageBackend.SQLITE
    assert default.database_url is None
    assert isinstance(create_storage(default).database, Database)

    secret_url = "postgresql://sentinel-user:sentinel-password@db.invalid/advisor"
    postgres = StorageSettings.from_environment(
        {
            "ADVISOR_STORAGE_BACKEND": "POSTGRES",
            "ADVISOR_DATABASE_URL": secret_url,
        },
        sqlite_path=tmp_path / "ignored.sqlite",
    )
    assert postgres.backend is StorageBackend.POSTGRES
    assert postgres.database_url == secret_url
    assert secret_url not in repr(postgres)
    assert "sentinel-password" not in repr(postgres)
    assert "sentinel-password" not in repr(create_storage(postgres).database)

    with pytest.raises(ValueError, match="requires ADVISOR_DATABASE_URL"):
        StorageSettings.from_environment(
            {"ADVISOR_STORAGE_BACKEND": "postgres"},
            sqlite_path=tmp_path / "ignored.sqlite",
        )
    with pytest.raises(ValueError, match="sqlite or postgres"):
        StorageSettings.from_environment(
            {"ADVISOR_STORAGE_BACKEND": "other"},
            sqlite_path=tmp_path / "ignored.sqlite",
        )


def test_process_environment_overrides_local_storage_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "ADVISOR_STORAGE_BACKEND=postgres\n"
        "ADVISOR_DATABASE_URL=postgresql://placeholder.invalid/advisor\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ADVISOR_STORAGE_BACKEND", "sqlite")
    settings = StorageSettings.from_environment(sqlite_path=tmp_path / "advisor.sqlite")
    assert settings.backend is StorageBackend.SQLITE


def test_postgres_schema_is_versioned_native_and_guarded():
    assert POSTGRES_SCHEMA_VERSION == 3
    assert set(POSTGRES_COLUMNS) == {
        "schema_metadata",
        "portfolio_snapshots",
        "current_portfolio",
        "confirmed_updates",
        "analysis_runs",
        "scheduler_executions",
        "telegram_deliveries",
    }
    schema = "\n".join(
        statement for migration in POSTGRES_MIGRATIONS.values() for statement in migration
    )
    for required in (
        "UUID PRIMARY KEY",
        "TIMESTAMPTZ NOT NULL",
        "payload_json TEXT NOT NULL",
        "one_running_analysis",
        "snapshots_no_update",
        "updates_no_delete",
        "runs_terminal_no_update",
        "scheduler_terminal_no_update",
        "scheduler_fence_analysis",
        "telegram_only_completed",
        "telegram_transition_guard",
        "telegram_no_delete",
    ):
        assert required in schema
    assert len({PORTFOLIO_LOCK, ANALYSIS_LOCK, SCHEDULER_LOCK, DELIVERY_LOCK}) == 4


def test_postgres_connection_failure_is_sanitized():
    sentinel = "postgresql://user:secret-password@secret-host/advisor"

    class DriverFailure(Exception):
        pass

    class FakeDriver:
        PostgresError = DriverFailure

        @staticmethod
        async def connect(**_options):
            raise DriverFailure(f"could not connect to {sentinel}")

    database = PostgresDatabase(sentinel, driver=FakeDriver)
    with pytest.raises(PersistenceError) as captured:
        asyncio.run(database.initialize())
    assert str(captured.value) == "PostgreSQL storage unavailable."
    assert sentinel not in str(captured.value)
    assert "secret-password" not in repr(database)


def test_postgres_operation_failure_is_sanitized():
    sentinel = "postgresql://user:secret-password@secret-host/advisor"

    class DriverFailure(Exception):
        pass

    class FakeConnection:
        async def execute(self, *_args):
            raise DriverFailure(f"query failed against {sentinel}")

        async def close(self):
            return None

    class FakeDriver:
        PostgresError = DriverFailure

        @staticmethod
        async def connect(**_options):
            return FakeConnection()

    async def fail():
        database = PostgresDatabase(sentinel, driver=FakeDriver)
        async with database.connection() as connection:
            await connection.execute("SELECT 1")

    with pytest.raises(PersistenceError) as captured:
        asyncio.run(fail())
    assert str(captured.value) == "PostgreSQL storage operation failed."
    assert sentinel not in str(captured.value)


def test_sqlite_only_operations_fail_before_postgres_connection(monkeypatch):
    sentinel = "postgresql://user:secret-password@secret-host/advisor"
    monkeypatch.setenv("ADVISOR_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("ADVISOR_DATABASE_URL", sentinel)
    output = []
    assert main(["backup"], output=output.append) == 1
    rendered = "\n".join(output)
    assert "available only for SQLite storage" in rendered
    assert sentinel not in rendered
    assert "secret-password" not in rendered


TEST_POSTGRES_URL = os.environ.get("ADVISOR_TEST_POSTGRES_URL")


@pytest.mark.postgres_integration
@pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="ADVISOR_TEST_POSTGRES_URL is not configured",
)
def test_postgres_repository_contracts_in_isolated_schema():
    async def scenario():
        try:
            driver = importlib.import_module("asyncpg")
            admin = await driver.connect(dsn=TEST_POSTGRES_URL, timeout=10)
        except Exception:
            pytest.fail("Dedicated PostgreSQL test database is unavailable.", pytrace=False)
        schema = f"advisor_test_{uuid4().hex}"
        try:
            await admin.execute(f'CREATE SCHEMA "{schema}"')
            storage = create_storage(
                StorageSettings(StorageBackend.POSTGRES, Path("ignored.sqlite"), TEST_POSTGRES_URL),
                postgres_schema=schema,
            )
            database = storage.database
            await storage.initialize()

            service = ConfirmedStateService(storage.snapshots)
            first = pg_snapshot()
            current = await service.initialize(first)
            assert current.revision == 1
            assert await storage.snapshots.get(first.id) == first
            assert len(await storage.confirmed_updates.list_recent()) == 1

            candidates = [
                replace(first, id=uuid4(), confirmed_at=NOW + timedelta(seconds=1))
                for _ in range(2)
            ]
            outcomes = await asyncio.gather(
                *(
                    ConfirmedStateService(storage.snapshots).confirm(
                        candidate,
                        expected_revision=1,
                        previous_snapshot_id=first.id,
                        update_type=UpdateType.BALANCE_CORRECTION,
                    )
                    for candidate in candidates
                ),
                return_exceptions=True,
            )
            assert sum(isinstance(value, ConflictError) for value in outcomes) == 1
            current = await storage.snapshots.get_current()
            assert current.revision == 2
            assert len(await storage.confirmed_updates.list_recent()) == 2

            run = AnalysisRun.start(current.snapshot.id, NOW + timedelta(seconds=2))
            contender = AnalysisRun.start(current.snapshot.id, NOW + timedelta(seconds=3))
            await storage.analyses.acquire(run)
            with pytest.raises(ConflictError):
                await storage.analyses.acquire(contender)
            completed = await storage.analyses.mark_completed(
                run.run_id,
                completed_at=NOW + timedelta(seconds=4),
                result_json='{"schema_version":1,"data":{}}',
            )
            assert completed.run.status is AnalysisRunStatus.COMPLETED
            with pytest.raises(ConflictError):
                await storage.analyses.mark_failed(
                    run.run_id,
                    completed_at=NOW + timedelta(seconds=5),
                    failure_category=FailureCategory.TIMEOUT,
                )

            stale = AnalysisRun.start(current.snapshot.id, NOW + timedelta(seconds=10))
            replacement = AnalysisRun.start(current.snapshot.id, NOW + timedelta(minutes=16))
            await storage.analyses.acquire(stale)
            await storage.analyses.acquire(replacement)
            recovered = await storage.analyses.get(stale.run_id)
            assert recovered.run.status is AnalysisRunStatus.FAILED
            assert recovered.failure_category is FailureCategory.INTERRUPTED
            with pytest.raises(ConflictError):
                await storage.analyses.mark_completed(
                    stale.run_id,
                    completed_at=NOW + timedelta(minutes=16, seconds=1),
                    result_json='{"schema_version":1,"data":{}}',
                )
            await storage.analyses.mark_failed(
                replacement.run_id,
                completed_at=NOW + timedelta(minutes=16, seconds=1),
                failure_category=FailureCategory.TIMEOUT,
            )

            cutoff = datetime(2026, 9, 17, 16, tzinfo=UTC)
            slot = ScheduledExecution(
                cutoff + timedelta(minutes=10),
                cutoff,
                uuid4(),
                cutoff + timedelta(minutes=10),
            )
            assert await storage.scheduler.claim(slot)
            assert not await storage.scheduler.claim(replace(slot, owner_token=uuid4()))
            failed = await storage.scheduler.finish(
                slot,
                slot.started_at + timedelta(seconds=1),
                ScheduledStatus.FAILED,
                FailureCategory.TIMEOUT,
            )
            assert failed.status is ScheduledStatus.FAILED
            with pytest.raises(ConflictError):
                await storage.scheduler.finish(
                    slot,
                    slot.started_at + timedelta(seconds=2),
                    ScheduledStatus.FAILED,
                    FailureCategory.TIMEOUT,
                )
            with pytest.raises(PersistenceError):
                await storage.analyses.create(
                    AnalysisRun(slot.owner_token, current.snapshot.id, slot.started_at)
                )

            stale_slot = ScheduledExecution(
                cutoff + timedelta(hours=4, minutes=10),
                cutoff + timedelta(hours=4),
                uuid4(),
                cutoff + timedelta(hours=4, minutes=10),
            )
            assert await storage.scheduler.claim(stale_slot)
            await storage.scheduler.recover(stale_slot.started_at + timedelta(minutes=16))
            recovered_slot = await storage.scheduler.get(stale_slot.scheduled_for)
            assert recovered_slot.status is ScheduledStatus.FAILED
            assert recovered_slot.failure_category is FailureCategory.INTERRUPTED

            delivery = DeliveryRecord(
                uuid4(), completed.run.run_id, "a" * 64, NOW + timedelta(seconds=5)
            )
            sending, active = await storage.deliveries.claim(delivery)
            assert active and sending.status is DeliveryStatus.SENDING
            sending = await storage.deliveries.start_attempt(sending.delivery_id)
            delivered = await storage.deliveries.finish(
                sending.delivery_id,
                NOW + timedelta(seconds=6),
                DeliveryStatus.DELIVERED,
            )
            assert delivered.status is DeliveryStatus.DELIVERED
            duplicate, active = await storage.deliveries.claim(delivery)
            assert not active and duplicate == delivered

            retryable = DeliveryRecord(
                uuid4(), completed.run.run_id, "b" * 64, NOW + timedelta(seconds=7)
            )
            sending, active = await storage.deliveries.claim(retryable)
            assert active
            await storage.deliveries.recover_stale(sending.started_at + timedelta(minutes=16))
            pending = await storage.deliveries.get(completed.run.run_id, retryable.destination_key)
            assert pending.status is DeliveryStatus.PENDING
            sending, active = await storage.deliveries.claim(retryable)
            assert active
            sending = await storage.deliveries.start_attempt(sending.delivery_id)
            await storage.deliveries.recover_stale(sending.started_at + timedelta(minutes=16))
            ambiguous = await storage.deliveries.get(
                completed.run.run_id, retryable.destination_key
            )
            assert ambiguous.status is DeliveryStatus.AMBIGUOUS
            _, active = await storage.deliveries.claim(retryable)
            assert not active

            with pytest.raises(PersistenceError):
                async with database.transaction() as connection:
                    await connection.execute(
                        "UPDATE portfolio_snapshots SET payload_json='{}' WHERE snapshot_id=$1",
                        first.id,
                    )
        finally:
            try:
                await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            finally:
                await admin.close()

    asyncio.run(scenario())
