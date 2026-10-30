"""Readiness contracts against a disposable PostgreSQL 17 database."""

import asyncio
import importlib
import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from test_dispatch import cloud_environment
from test_http_status import status_app
from test_portfolio_risk import NOW, state
from test_postgres_financial_repository import at, opening
from test_postgres_storage import isolated_postgres_schema

from advanced_trading_advisor.cli import dispatch
from advanced_trading_advisor.cli.main import CLI
from advanced_trading_advisor.domain.persistence import SchemaVersionError
from advanced_trading_advisor.financial.values import OperationId
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.scheduler.service import Scheduler
from advanced_trading_advisor.storage.factory import create_storage
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_financial_repository import (
    PostgresFinancialLedgerRepository,
)
from advanced_trading_advisor.storage.postgres_schema import POSTGRES_MIGRATIONS
from advanced_trading_advisor.storage.settings import StorageSettings

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        not os.environ.get("ADVISOR_TEST_POSTGRES_URL"),
        reason="ADVISOR_TEST_POSTGRES_URL is not configured",
    ),
]


async def create_v3(connection):
    async with connection.transaction():
        for version in (1, 2, 3):
            for statement in POSTGRES_MIGRATIONS[version]:
                await connection.execute(statement)
        await connection.execute("INSERT INTO schema_metadata VALUES (1,3)")


def test_v3_legacy_ready_financial_rejected_and_explicit_concurrent_upgrade():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await create_v3(connection)
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            repo = PostgresFinancialLedgerRepository(db)
            await db.check_legacy_ready()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=status_app(db.check_legacy_ready)),
                base_url="http://test",
            ) as client:
                assert (await client.get("/ready")).status_code == 200
            for call in (
                repo.create_portfolio(uuid4()),
                repo.load_portfolio(uuid4()),
                repo.load_operations(uuid4()),
                repo.append(uuid4(), OperationId(uuid4()), at(), opening()),
            ):
                with pytest.raises(SchemaVersionError):
                    await call
            assert await connection.fetchval("SELECT version FROM schema_metadata") == 3
            assert await connection.fetchval("SELECT to_regclass('financial_portfolios')") is None
            await asyncio.gather(db.initialize(), db.initialize())
            assert await connection.fetchval("SELECT version FROM schema_metadata") == 4
            await db.check_legacy_ready()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=status_app(db.check_legacy_ready)),
                base_url="http://test",
            ) as client:
                assert (await client.get("/ready")).status_code == 200
            owner = uuid4()
            await repo.create_portfolio(owner)
            assert await repo.load_portfolio(owner) is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage",
    [
        "missing_table",
        "missing_column",
        "missing_trigger",
        "disabled_trigger",
        "missing_function",
        "invalid_metadata",
    ],
)
def test_incomplete_v4_rejected_before_financial_write(damage):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await db.initialize()
            if damage == "missing_table":
                await connection.execute("DROP TABLE financial_postings CASCADE")
            elif damage == "missing_column":
                await connection.execute(
                    "ALTER TABLE financial_portfolios DROP COLUMN last_sequence"
                )
            elif damage == "missing_trigger":
                await connection.execute(
                    "DROP TRIGGER financial_operations_no_update ON financial_operations"
                )
            elif damage == "disabled_trigger":
                await connection.execute(
                    "ALTER TABLE financial_postings DISABLE TRIGGER financial_postings_no_delete"
                )
            elif damage == "missing_function":
                await connection.execute("DROP FUNCTION financial_decimal_valid(numeric) CASCADE")
            else:
                await connection.execute("UPDATE schema_metadata SET version=5")
            repo = PostgresFinancialLedgerRepository(db)
            with pytest.raises(SchemaVersionError):
                await repo.create_portfolio(uuid4())
            assert await connection.fetchval("SELECT count(*) FROM financial_portfolios") == 0
            with pytest.raises(SchemaVersionError):
                await db.check_legacy_ready()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=status_app(db.check_legacy_ready)),
                base_url="http://test",
            ) as client:
                response = await client.get("/ready")
                assert response.status_code == 503
                assert response.json() == {"status": "not_ready"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage_statements",
    [
        (
            "DROP TRIGGER financial_operations_no_update ON financial_operations",
            "CREATE TRIGGER financial_operations_no_update BEFORE INSERT ON financial_operations "
            "FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()",
        ),
        (
            "DROP TRIGGER financial_operations_no_delete ON financial_operations",
            "CREATE TRIGGER financial_operations_no_delete BEFORE DELETE ON financial_operations "
            "FOR EACH ROW EXECUTE FUNCTION financial_postings_complete()",
        ),
        (
            "DROP TRIGGER financial_operation_postings_complete ON financial_operations",
            "CREATE CONSTRAINT TRIGGER financial_operation_postings_complete "
            "AFTER INSERT ON financial_operations FOR EACH ROW "
            "EXECUTE FUNCTION financial_postings_complete()",
        ),
        (
            "CREATE OR REPLACE FUNCTION financial_postings_complete() RETURNS TRIGGER "
            "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
        ),
    ],
    ids=["wrong_event", "wrong_function", "not_deferred", "changed_function_body"],
)
def test_changed_financial_guard_rejected_even_when_named_and_enabled(damage_statements):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await db.initialize()
            for statement in damage_statements:
                await connection.execute(statement)
            with pytest.raises(SchemaVersionError):
                await db.check_ready()
            with pytest.raises(SchemaVersionError):
                await PostgresFinancialLedgerRepository(db).create_portfolio(uuid4())
            assert await connection.fetchval("SELECT count(*) FROM financial_portfolios") == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "damage_sql",
    [
        "DROP TABLE telegram_deliveries",
        "ALTER TABLE portfolio_snapshots DROP COLUMN payload_json",
    ],
    ids=["missing_legacy_table", "missing_legacy_column"],
)
def test_damaged_v3_rejected_without_migration_or_unrelated_data_loss(damage_sql):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await create_v3(connection)
            sentinel = uuid4()
            await connection.execute(
                "INSERT INTO portfolio_snapshots "
                "(snapshot_id, confirmed_at, payload_json, schema_version, created_at) "
                "VALUES ($1, now(), 'sentinel', 4, now())",
                sentinel,
            )
            await connection.execute(damage_sql)
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            with pytest.raises(SchemaVersionError):
                await db.check_legacy_ready()
            assert await connection.fetchval("SELECT version FROM schema_metadata") == 3
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM portfolio_snapshots WHERE snapshot_id=$1", sentinel
                )
                == 1
            )
            assert await connection.fetchval("SELECT to_regclass('financial_portfolios')") is None

    asyncio.run(scenario())


@pytest.mark.parametrize("artifact", ["table", "function"])
def test_v3_metadata_with_partial_financial_object_is_not_legacy_ready(artifact):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await create_v3(connection)
            if artifact == "table":
                await connection.execute("CREATE TABLE financial_portfolios (id UUID PRIMARY KEY)")
            else:
                await connection.execute(
                    "CREATE FUNCTION financial_decimal_valid(value NUMERIC) RETURNS BOOLEAN "
                    "LANGUAGE SQL AS 'SELECT true'"
                )
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            with pytest.raises(SchemaVersionError):
                await db.check_legacy_ready()

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [3, 4])
def test_cloud_dispatch_accepts_legacy_schema_without_migration(monkeypatch, version):
    async def scenario():
        environment = cloud_environment() | {
            "ADVISOR_DATABASE_URL": os.environ["ADVISOR_TEST_POSTGRES_URL"]
        }
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        async with isolated_postgres_schema() as (connection, schema):
            storage = create_storage(
                StorageSettings.from_environment(environment, sqlite_path=Path("unused.sqlite")),
                postgres_schema=schema,
            )
            if version == 3:
                await create_v3(connection)
            else:
                await storage.initialize()
            await ConfirmedStateService(storage.snapshots).initialize(state())
            monkeypatch.setattr(dispatch, "create_storage", lambda _settings: storage)

            async def completed(_self):
                return "completed"

            monkeypatch.setattr(Scheduler, "dispatch", completed)
            output = []
            await dispatch.run(CLI(now=lambda: NOW, output=output.append))
            assert output[-1] == "Dispatch: completed"
            assert await connection.fetchval("SELECT version FROM schema_metadata") == version

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [3, 4])
def test_ordinary_cli_status_paths_do_not_migrate(monkeypatch, version):
    async def scenario():
        environment = {
            "ADVISOR_STORAGE_BACKEND": "postgres",
            "ADVISOR_DATABASE_URL": os.environ["ADVISOR_TEST_POSTGRES_URL"],
            "TELEGRAM_ENABLED": "false",
        }
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        async with isolated_postgres_schema() as (connection, schema):
            storage = create_storage(
                StorageSettings.from_environment(environment, sqlite_path=Path("unused.sqlite")),
                postgres_schema=schema,
            )
            if version == 3:
                await create_v3(connection)
            else:
                await storage.initialize()
            cli_module = importlib.import_module("advanced_trading_advisor.cli.main")
            monkeypatch.setattr(cli_module, "create_storage", lambda _settings: storage)
            for command in (
                ["scheduler", "status"],
                ["telegram", "status"],
                ["history", "latest"],
                ["status"],
                ["setups"],
            ):
                await CLI(now=lambda: NOW, output=lambda _line: None).run(
                    cli_module.parser().parse_args(command)
                )
                assert await connection.fetchval("SELECT version FROM schema_metadata") == version

    asyncio.run(scenario())
