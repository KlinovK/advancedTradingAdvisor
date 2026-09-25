import asyncio
import importlib
import os
from contextlib import asynccontextmanager
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
    ConfirmedUpdate,
    ConflictError,
    CurrentPortfolio,
    FailureCategory,
    PersistenceError,
    RevisionConflictError,
    UpdateType,
)
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import (
    DEFAULT_MANUAL_ACCOUNT_ID,
    DEFAULT_MANUAL_ACCOUNT_NAME,
    Account,
    AccountType,
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    FinancialSettings,
    Portfolio,
)
from advanced_trading_advisor.scheduler.models import ScheduledExecution, ScheduledStatus
from advanced_trading_advisor.storage import postgres_repositories
from advanced_trading_advisor.storage.codecs import PAYLOAD_VERSION, SnapshotCodec, UpdateCodec
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.factory import create_storage
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_repositories import (
    ANALYSIS_LOCK,
    DELIVERY_LOCK,
    PORTFOLIO_LOCK,
    SCHEDULER_LOCK,
    PostgresSnapshotRepository,
)
from advanced_trading_advisor.storage.postgres_schema import (
    POSTGRES_COLUMNS,
    POSTGRES_MIGRATIONS,
    POSTGRES_SCHEMA_VERSION,
)
from advanced_trading_advisor.storage.settings import StorageBackend, StorageSettings

NOW = datetime(2026, 9, 17, 12, 30, tzinfo=UTC)


@pytest.mark.parametrize("stale_at", ["precheck", "pointer_update"])
def test_postgres_confirmed_state_cas_uses_revision_conflict_offline(monkeypatch, stale_at):
    current = pg_snapshot()
    candidate = replace(current, id=uuid4(), confirmed_at=NOW + timedelta(seconds=1))
    update = ConfirmedUpdate(
        uuid4(), UpdateType.BALANCE_CORRECTION, current.id, candidate.id, candidate.confirmed_at
    )
    events = []

    class FakeConnection:
        async def execute(self, statement, *args):
            events.append("pointer_update")
            assert statement.startswith("UPDATE current_portfolio")
            return "UPDATE 0"

    class FakeDatabase:
        @asynccontextmanager
        async def transaction(self):
            events.append("transaction_started")
            yield FakeConnection()

    async def fake_lock(connection, key):
        events.append("lock")

    async def fake_current(connection, *, lock=False):
        assert lock is True
        return CurrentPortfolio(current, 1)

    async def fake_save(connection, snapshot):
        events.append("save")

    async def fake_append(connection, confirmed_update):
        events.append("audit")

    monkeypatch.setattr(postgres_repositories, "advisory_lock", fake_lock)
    monkeypatch.setattr(postgres_repositories, "pg_current", fake_current)
    monkeypatch.setattr(postgres_repositories, "pg_save_snapshot", fake_save)
    monkeypatch.setattr(postgres_repositories, "pg_append_update", fake_append)

    async def scenario():
        with pytest.raises(RevisionConflictError):
            await PostgresSnapshotRepository(FakeDatabase()).set_current(
                candidate,
                expected_revision=0 if stale_at == "precheck" else 1,
                update=update,
            )

    asyncio.run(scenario())
    if stale_at == "precheck":
        assert events == ["transaction_started", "lock"]
    else:
        assert events == ["transaction_started", "lock", "save", "audit", "pointer_update"]


def pg_snapshot(at=NOW):
    return ConfirmedPortfolioSnapshot(
        uuid4(),
        at,
        Portfolio(
            (
                Account(
                    DEFAULT_MANUAL_ACCOUNT_ID,
                    DEFAULT_MANUAL_ACCOUNT_NAME,
                    AccountType.MANUAL,
                    (
                        AssetPosition("BTC", Decimal("0.019800000000000000")),
                        AssetPosition("USDC", Decimal("3450.00")),
                    ),
                ),
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
    postgres_database = create_storage(postgres).database
    assert postgres_database.schema == "public"
    assert "sentinel-password" not in repr(postgres_database)
    with pytest.raises(ValueError, match="Invalid PostgreSQL schema name"):
        create_storage(postgres, postgres_schema="")

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


@pytest.mark.parametrize("activation_failure", [False, True])
def test_postgres_schema_isolation_failure_closes_and_is_sanitized(activation_failure):
    sentinel = "postgresql://sentinel-user:sentinel-password@sentinel-host/advisor"

    class DriverFailure(Exception):
        pass

    class FakeConnection:
        closed = False

        async def fetchval(self, query, *_args):
            if activation_failure:
                raise DriverFailure(f"schema activation failed for {sentinel}")
            if "current_schema" in query:
                return "public"
            return "advisor_test_expected"

        async def close(self):
            self.closed = True

    connection = FakeConnection()

    class FakeDriver:
        PostgresError = DriverFailure

        @staticmethod
        async def connect(**options):
            assert options["server_settings"] == {"search_path": "advisor_test_expected"}
            return connection

    async def fail():
        database = PostgresDatabase(
            sentinel,
            schema="advisor_test_expected",
            driver=FakeDriver,
        )
        async with database.connection():
            raise AssertionError("A schema-mismatched connection must never be yielded.")

    with pytest.raises(PersistenceError) as captured:
        asyncio.run(fail())
    assert str(captured.value) == "PostgreSQL schema isolation unavailable."
    assert connection.closed
    rendered = str(captured.value)
    assert sentinel not in rendered
    assert "sentinel-password" not in rendered
    assert "sentinel-host" not in rendered
    assert "schema activation failed" not in rendered


def test_postgres_operation_failure_is_sanitized():
    sentinel = "postgresql://user:secret-password@secret-host/advisor"

    class DriverFailure(Exception):
        pass

    class FakeConnection:
        async def fetchval(self, query, *_args):
            if "current_schema" in query:
                return "public"
            return "public"

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


@asynccontextmanager
async def isolated_postgres_schema():
    """Create test-only state under the dedicated integration URL and always remove it."""
    try:
        driver = importlib.import_module("asyncpg")
        admin = await driver.connect(dsn=TEST_POSTGRES_URL, timeout=10)
    except Exception as error:
        pytest.fail(
            f"Dedicated PostgreSQL test database is unavailable ({type(error).__name__}).",
            pytrace=False,
        )
    schema = f"advisor_test_{uuid4().hex}"
    try:
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        database = PostgresDatabase(TEST_POSTGRES_URL, schema=schema, driver=driver)
        async with database.connection() as scoped:
            if await scoped.fetchval("SELECT current_schema()") != schema:
                pytest.fail("Isolated PostgreSQL schema activation failed.", pytrace=False)
            if await scoped.fetchval("SELECT to_regclass('schema_metadata')") is not None:
                pytest.fail("Isolated PostgreSQL schema is not fresh.", pytrace=False)
            yield scoped, schema
    finally:
        try:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            await admin.close()


async def postgres_connection_namespace(database):
    async with database.connection() as connection:
        effective_schema = await connection.fetchval("SELECT current_schema()")
        relation_schema = await connection.fetchval(
            """SELECT namespace.nspname
               FROM pg_class relation
               JOIN pg_namespace namespace ON namespace.oid=relation.relnamespace
               WHERE relation.oid=to_regclass('current_portfolio')"""
        )
        current_count = await connection.fetchval("SELECT count(*) FROM current_portfolio")
        return effective_schema, relation_schema, current_count


@pytest.mark.parametrize("failure", [None, "close", "schema", "freshness"])
def test_isolated_postgres_schema_checks_and_cleanup(monkeypatch, failure):
    statements = []
    events = []
    monkeypatch.setitem(
        globals(),
        "TEST_POSTGRES_URL",
        "postgresql://test-user:test-password@test.invalid/advisor-test",
    )

    class AdminConnection:
        closed = False

        async def execute(self, statement):
            statements.append(statement)
            events.append("drop" if statement.startswith("DROP") else "create")

        async def close(self):
            self.closed = True
            events.append("admin_close")

    class ScopedConnection:
        schema = None
        schema_checks = 0

        async def fetchval(self, query, *args):
            if "set_config" in query:
                self.schema = args[0]
                return self.schema
            if "current_schema" in query:
                self.schema_checks += 1
                # Let the production connection boundary pass, then test the fixture guard.
                if failure == "schema" and self.schema_checks == 2:
                    return "public"
                return self.schema
            if "to_regclass" in query:
                return "schema_metadata" if failure == "freshness" else None
            raise AssertionError("Unexpected fixture query.")

        async def close(self):
            events.append("scoped_close")
            if failure == "close":
                raise RuntimeError("scoped close failed")

    admin = AdminConnection()
    scoped = ScopedConnection()

    class FakeDriver:
        calls = 0
        PostgresError = ()
        InterfaceError = ()

        @classmethod
        async def connect(cls, **options):
            cls.calls += 1
            if cls.calls == 2:
                assert options["server_settings"]["search_path"].startswith("advisor_test_")
            return admin if cls.calls == 1 else scoped

    monkeypatch.setattr(importlib, "import_module", lambda _name: FakeDriver)

    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            assert failure not in {"schema", "freshness"}, "Invalid fixture was yielded."
            assert connection is scoped
            assert scoped.schema == schema
            assert scoped.schema_checks == 2
            events.append("yield")

    if failure == "close":
        with pytest.raises(PersistenceError, match="PostgreSQL storage close failed"):
            asyncio.run(scenario())
    elif failure in {"schema", "freshness"}:
        message = "activation failed" if failure == "schema" else "not fresh"
        with pytest.raises(pytest.fail.Exception, match=message):
            asyncio.run(scenario())
    else:
        asyncio.run(scenario())
    assert statements[0].startswith('CREATE SCHEMA "advisor_test_')
    assert statements[1].startswith('DROP SCHEMA IF EXISTS "advisor_test_')
    assert statements[1].endswith('" CASCADE')
    assert statements[1] == statements[0].replace("CREATE SCHEMA", "DROP SCHEMA IF EXISTS") + (
        " CASCADE"
    )
    expected_events = ["create"]
    if failure not in {"schema", "freshness"}:
        expected_events.append("yield")
    assert events == [*expected_events, "scoped_close", "drop", "admin_close"]
    assert admin.closed


@pytest.mark.postgres_integration
@pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="ADVISOR_TEST_POSTGRES_URL is not configured",
)
def test_postgres_v1_migrates_to_v3_and_preserves_canonical_state():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            assert await connection.fetchval("SELECT current_schema()") == schema
            assert await connection.fetchval("SELECT to_regclass('schema_metadata')") is None
            async with connection.transaction():
                for statement in POSTGRES_MIGRATIONS[1]:
                    await connection.execute(statement)
                await connection.execute(
                    "INSERT INTO schema_metadata(singleton_id,version) VALUES (1,1)"
                )
                first = pg_snapshot()
                snapshot_payload = SnapshotCodec.encode(first)
                update = ConfirmedUpdate(
                    uuid4(), UpdateType.INITIAL_STATE, None, first.id, first.confirmed_at
                )
                update_payload = UpdateCodec.encode(update)
                await connection.execute(
                    """INSERT INTO portfolio_snapshots
                       (snapshot_id,confirmed_at,payload_json,schema_version,created_at)
                       VALUES ($1,$2,$3,$4,$5)""",
                    first.id,
                    first.confirmed_at,
                    snapshot_payload,
                    PAYLOAD_VERSION,
                    first.confirmed_at,
                )
                await connection.execute(
                    """INSERT INTO current_portfolio
                       (singleton_id,snapshot_id,revision,updated_at) VALUES (1,$1,1,$2)""",
                    first.id,
                    first.confirmed_at,
                )
                await connection.execute(
                    """INSERT INTO confirmed_updates
                       (update_id,update_type,previous_snapshot_id,new_snapshot_id,
                        confirmed_at,payload_json,schema_version)
                       VALUES ($1,$2,NULL,$3,$4,$5,$6)""",
                    update.update_id,
                    update.update_type.value,
                    update.new_snapshot_id,
                    update.confirmed_at,
                    update_payload,
                    PAYLOAD_VERSION,
                )

            storage = create_storage(
                StorageSettings(StorageBackend.POSTGRES, Path("ignored.sqlite"), TEST_POSTGRES_URL),
                postgres_schema=schema,
            )
            await storage.initialize()
            assert (
                await connection.fetchval(
                    "SELECT version FROM schema_metadata WHERE singleton_id=1"
                )
                == 3
            )
            tables = {
                row["table_name"]
                for row in await connection.fetch(
                    """SELECT table_name FROM information_schema.tables
                       WHERE table_schema=current_schema() AND table_type='BASE TABLE'"""
                )
            }
            assert tables == set(POSTGRES_COLUMNS)
            assert (
                await connection.fetchval(
                    "SELECT payload_json FROM portfolio_snapshots WHERE snapshot_id=$1", first.id
                )
                == snapshot_payload
            )
            assert (
                await connection.fetchval(
                    "SELECT payload_json FROM confirmed_updates WHERE update_id=$1",
                    update.update_id,
                )
                == update_payload
            )
            assert (await storage.snapshots.get_current()).snapshot == first
            assert await storage.confirmed_updates.list_recent() == (update,)

    asyncio.run(scenario())


@pytest.mark.postgres_integration
@pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="ADVISOR_TEST_POSTGRES_URL is not configured",
)
def test_postgres_repository_contracts_in_isolated_schema():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            storage = create_storage(
                StorageSettings(StorageBackend.POSTGRES, Path("ignored.sqlite"), TEST_POSTGRES_URL),
                postgres_schema=schema,
            )
            database = storage.database
            await storage.initialize()

            assert await postgres_connection_namespace(database) == (schema, schema, 0)
            sequential_namespaces = [
                await postgres_connection_namespace(database) for _ in range(3)
            ]
            assert sequential_namespaces == [(schema, schema, 0)] * 3
            concurrent_namespaces = await asyncio.gather(
                *(postgres_connection_namespace(database) for _ in range(4))
            )
            assert concurrent_namespaces == [(schema, schema, 0)] * 4
            assert (
                await asyncio.gather(*(storage.snapshots.get_current() for _ in range(4)))
                == [None] * 4
            )

            service = ConfirmedStateService(storage.snapshots)
            first = pg_snapshot()
            current = await service.initialize(first)
            assert current.revision == 1
            assert await storage.snapshots.get(first.id) == first
            assert len(await storage.confirmed_updates.list_recent()) == 1
            assert await connection.fetchval(
                "SELECT payload_json FROM portfolio_snapshots WHERE snapshot_id=$1", first.id
            ) == SnapshotCodec.encode(first)

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
            loser = candidates[
                next(
                    index
                    for index, value in enumerate(outcomes)
                    if isinstance(value, ConflictError)
                )
            ]
            assert await storage.snapshots.get(loser.id) is None

            runs = (
                AnalysisRun.start(current.snapshot.id, NOW + timedelta(seconds=2)),
                AnalysisRun.start(current.snapshot.id, NOW + timedelta(seconds=3)),
            )
            acquisitions = await asyncio.gather(
                *(storage.analyses.acquire(run) for run in runs),
                return_exceptions=True,
            )
            assert sum(value is None for value in acquisitions) == 1
            assert sum(isinstance(value, ConflictError) for value in acquisitions) == 1
            run = runs[next(index for index, value in enumerate(acquisitions) if value is None)]
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
            slots = (
                ScheduledExecution(
                    cutoff + timedelta(minutes=10),
                    cutoff,
                    uuid4(),
                    cutoff + timedelta(minutes=10),
                ),
                ScheduledExecution(
                    cutoff + timedelta(minutes=10),
                    cutoff,
                    uuid4(),
                    cutoff + timedelta(minutes=10),
                ),
            )
            claims = await asyncio.gather(*(storage.scheduler.claim(slot) for slot in slots))
            assert sorted(claims) == [False, True]
            slot = slots[claims.index(True)]
            assert not await storage.scheduler.claim(slot)
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
            delivery_claims = await asyncio.gather(
                storage.deliveries.claim(delivery),
                storage.deliveries.claim(replace(delivery, delivery_id=uuid4())),
            )
            assert sum(active for _, active in delivery_claims) == 1
            sending = next(record for record, active in delivery_claims if active)
            assert sending.status is DeliveryStatus.SENDING
            sending = await storage.deliveries.start_attempt(sending.delivery_id)
            assert sending.attempt_count == 1
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
            with pytest.raises(PersistenceError):
                async with database.transaction() as immutable:
                    await immutable.execute(
                        "DELETE FROM confirmed_updates WHERE new_snapshot_id=$1", first.id
                    )

    asyncio.run(scenario())


@pytest.mark.postgres_integration
@pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="ADVISOR_TEST_POSTGRES_URL is not configured",
)
def test_postgres_target_schema_cannot_observe_control_schema_state():
    async def scenario():
        async with isolated_postgres_schema() as (_, control_schema):
            control = create_storage(
                StorageSettings(StorageBackend.POSTGRES, Path("ignored.sqlite"), TEST_POSTGRES_URL),
                postgres_schema=control_schema,
            )
            await control.initialize()
            await ConfirmedStateService(control.snapshots).initialize(pg_snapshot())
            control_namespace = await postgres_connection_namespace(control.database)
            assert control_namespace == (control_schema, control_schema, 1)

            async with isolated_postgres_schema() as (_, target_schema):
                target = create_storage(
                    StorageSettings(
                        StorageBackend.POSTGRES,
                        Path("ignored.sqlite"),
                        TEST_POSTGRES_URL,
                    ),
                    postgres_schema=target_schema,
                )
                await target.initialize()
                assert await postgres_connection_namespace(target.database) == (
                    target_schema,
                    target_schema,
                    0,
                )
                assert await target.snapshots.get_current() is None

    asyncio.run(scenario())


@pytest.mark.postgres_integration
@pytest.mark.skipif(
    not TEST_POSTGRES_URL,
    reason="ADVISOR_TEST_POSTGRES_URL is not configured",
)
def test_postgres_missing_schema_fails_closed_without_public_fallback():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            storage = create_storage(
                StorageSettings(StorageBackend.POSTGRES, Path("ignored.sqlite"), TEST_POSTGRES_URL),
                postgres_schema=schema,
            )
            await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            with pytest.raises(
                PersistenceError,
                match=r"^PostgreSQL schema isolation unavailable\.$",
            ):
                await storage.initialize()

    asyncio.run(scenario())
