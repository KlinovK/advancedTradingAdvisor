"""Disposable PostgreSQL only; all writes are scoped to generated rehearsal schemas."""

import asyncio
import os
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from test_migration import backup as backup

from advanced_trading_advisor.domain.persistence import PersistenceError
from advanced_trading_advisor.storage import migration
from advanced_trading_advisor.storage.migration import (
    MigrationError,
    import_source,
    load_source,
    normalized_rows,
    read_target,
    rehearse,
    verify_source,
)
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_repositories import PORTFOLIO_LOCK
from advanced_trading_advisor.storage.postgres_schema import POSTGRES_MIGRATIONS

TEST_URL = os.environ.get("ADVISOR_TEST_POSTGRES_URL")
pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(not TEST_URL, reason="ADVISOR_TEST_POSTGRES_URL is not configured"),
]


@asynccontextmanager
async def isolated_target():
    schema = "advisor_rehearsal_" + uuid4().hex
    admin = PostgresDatabase(TEST_URL, schema="pg_catalog")
    async with admin.connection() as connection:
        await connection.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield PostgresDatabase(TEST_URL, schema=schema)
    finally:
        async with admin.connection() as connection:
            await connection.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_import_preserves_every_row_and_identical_rerun_is_noop(backup, monkeypatch):
    async def scenario():
        source = await load_source(backup)
        async with isolated_target() as target:
            assert await import_source(source, target, rehearsal=True) == "imported"
            async with target.connection() as c:
                rows = await read_target(c)
                equal = normalized_rows(rows) == normalized_rows(source.rows)
                assert equal, "Row equality failed"
            assert await verify_source(source, target) == "verified"

            async def forbidden(*_args):
                raise AssertionError("Identical target must not be written")

            monkeypatch.setattr(migration, "insert_rows", forbidden)
            assert await import_source(source, target, rehearsal=True) == "verified_noop"

    asyncio.run(scenario())


def test_existing_v3_target_remains_verifiable_and_upgrades_to_v4(backup):
    async def scenario():
        source = await load_source(backup)
        async with isolated_target() as target:
            async with target.transaction() as connection:
                for version in (1, 2, 3):
                    for statement in POSTGRES_MIGRATIONS[version]:
                        await connection.execute(statement)
                await connection.execute("INSERT INTO schema_metadata VALUES (1,3)")
                await migration.insert_rows(connection, source.rows)
            assert await verify_source(source, target) == "verified"
            assert await import_source(source, target, rehearsal=True) == "verified_noop"
            await target.initialize()
            assert await verify_source(source, target) == "verified"
            assert await import_source(source, target, rehearsal=True) == "verified_noop"

    asyncio.run(scenario())


@pytest.mark.parametrize("stage", ["insert", "validation"])
def test_import_rolls_back_all_rows(backup, monkeypatch, stage):
    async def scenario():
        source = await load_source(backup)
        original = migration.insert_rows

        async def broken(c, rows):
            if stage == "insert":
                partial = {
                    t: values if t == "portfolio_snapshots" else [] for t, values in rows.items()
                }
                await original(c, partial)
                raise RuntimeError("synthetic-secret-error")
            await original(c, rows)
            await c.execute("UPDATE current_portfolio SET revision=revision+1")

        async with isolated_target() as target:
            await target.initialize()
            monkeypatch.setattr(migration, "insert_rows", broken)
            with pytest.raises(MigrationError) as captured:
                await import_source(source, target, rehearsal=True)
            assert "synthetic-secret-error" not in str(captured.value)
            async with target.connection() as c:
                remaining = await read_target(c)
                assert all(not remaining[t] for t in migration.TABLES)

    asyncio.run(scenario())


@pytest.mark.parametrize("partial", [True, False])
def test_populated_different_target_refused_without_changes(backup, partial):
    async def scenario():
        source = await load_source(backup)
        async with isolated_target() as target:
            await target.initialize()
            async with target.transaction() as c:
                rows = {
                    t: values if not partial or t == "portfolio_snapshots" else []
                    for t, values in source.rows.items()
                }
                await migration.insert_rows(c, rows)
                if not partial:
                    await c.execute("UPDATE current_portfolio SET revision=revision+1")
            async with target.connection() as c:
                before = normalized_rows(await read_target(c))
            with pytest.raises(MigrationError):
                await import_source(source, target, rehearsal=True)
            async with target.connection() as c:
                unchanged = normalized_rows(await read_target(c)) == before
                assert unchanged, "Target changed"

    asyncio.run(scenario())


def test_concurrent_importer_and_writer_exclusion(backup, monkeypatch):
    async def scenario():
        source = await load_source(backup)
        entered, release = asyncio.Event(), asyncio.Event()
        original = migration.insert_rows

        async def held(c, rows):
            entered.set()
            await release.wait()
            await original(c, rows)

        monkeypatch.setattr(migration, "insert_rows", held)
        async with isolated_target() as target:
            await target.initialize()
            first = asyncio.create_task(import_source(source, target, rehearsal=True))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), timeout=10)
                second = asyncio.create_task(import_source(source, target, rehearsal=True))
                async with target.transaction() as writer:
                    assert not await writer.fetchval(
                        "SELECT pg_try_advisory_xact_lock($1)", PORTFOLIO_LOCK
                    )
                with pytest.raises(PersistenceError):
                    async with target.transaction() as writer:
                        await writer.execute("SET LOCAL lock_timeout = '100ms'")
                        await writer.execute("UPDATE current_portfolio SET revision=revision")
            finally:
                release.set()
                results = await asyncio.gather(first, *([second] if second else []))
            assert sorted(results) == ["imported", "verified_noop"]

    asyncio.run(scenario())


@pytest.mark.parametrize("fail", [False, True])
def test_rehearsal_cleanup(backup, monkeypatch, fail):
    async def scenario():
        source = await load_source(backup)
        identifier = uuid4()
        monkeypatch.setattr(migration, "uuid4", lambda: identifier)
        if fail:

            async def broken(*_args, **_kwargs):
                raise RuntimeError("synthetic error must be sanitized")

            monkeypatch.setattr(migration, "import_source", broken)
            with pytest.raises(MigrationError):
                await rehearse(source, TEST_URL)
        else:
            assert await rehearse(source, TEST_URL) == "rehearsed"
        admin = PostgresDatabase(TEST_URL, schema="pg_catalog")
        async with admin.connection() as c:
            assert not await c.fetchval(
                "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=$1)",
                "advisor_rehearsal_" + identifier.hex,
            )

    asyncio.run(scenario())


def test_verify_is_readonly_and_never_initializes(backup, monkeypatch):
    async def scenario():
        source = await load_source(backup)
        async with isolated_target() as target:
            await import_source(source, target, rehearsal=True)

            async def forbidden(*_args, **_kwargs):
                raise AssertionError("Verification must never initialize")

            monkeypatch.setattr(PostgresDatabase, "initialize", forbidden)
            original = migration.read_target

            async def read(c):
                assert await c.fetchval("SHOW transaction_read_only") == "on"
                return await original(c)

            monkeypatch.setattr(migration, "read_target", read)
            assert await verify_source(source, target) == "verified"

    asyncio.run(scenario())


def test_missing_and_incompatible_target_refused(backup):
    async def scenario():
        source = await load_source(backup)
        absent = PostgresDatabase(TEST_URL, schema="advisor_rehearsal_" + uuid4().hex)
        with pytest.raises(MigrationError):
            await verify_source(source, absent)
        with pytest.raises(MigrationError):
            await import_source(source, absent, rehearsal=True)
        async with isolated_target() as target:
            async with target.connection() as c:
                await c.execute("CREATE TABLE unexpected(id integer)")
            with pytest.raises(MigrationError):
                await import_source(source, target, rehearsal=True)
            with pytest.raises(MigrationError):
                await verify_source(source, target)

    asyncio.run(scenario())


def test_empty_source_initialization_is_not_reported_as_noop():
    async def scenario():
        rows = {t: [] for t in migration.COLUMNS}
        rows["schema_metadata"] = [{"singleton_id": 1, "version": 3}]
        source = migration.MigrationSource(rows, migration.build_manifest(rows))
        async with isolated_target() as target:
            assert await import_source(source, target, rehearsal=True) == "imported"
            assert await import_source(source, target, rehearsal=True) == "verified_noop"

    asyncio.run(scenario())
