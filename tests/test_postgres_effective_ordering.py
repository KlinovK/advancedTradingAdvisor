"""D13 experiments against disposable PostgreSQL 17 schemas; no v5 migration."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
POSTGRES_URL = os.environ.get("ADVISOR_TEST_POSTGRES_URL")
POSTGRES = pytest.mark.postgres_integration


def canonical_path(value):
    """Validate the proposed representation before using Python tuple ordering."""
    if not isinstance(value, tuple) or not value:
        raise ValueError("effective path must be a nonempty tuple")
    if any(type(part) is not int or not INT64_MIN <= part <= INT64_MAX for part in value):
        raise ValueError("effective path components must be signed BIGINT integers")
    return value


def python_order(paths):
    return sorted(canonical_path(path) for path in paths)


@pytest.mark.parametrize(
    "invalid",
    [(), [], (None,), (True,), (1.0,), ("1",), (INT64_MIN - 1,), (INT64_MAX + 1,)],
)
def test_python_rejects_noncanonical_paths(invalid):
    with pytest.raises(ValueError):
        canonical_path(invalid)


def test_python_tuple_ordering_and_boundaries():
    paths = [(2, 1), (3,), (2,), (0,), (-1,), (INT64_MAX,), (INT64_MIN,), (2, 1, 0)]
    assert python_order(paths) == [
        (INT64_MIN,),
        (-1,),
        (0,),
        (2,),
        (2, 1),
        (2, 1, 0),
        (3,),
        (INT64_MAX,),
    ]
    assert canonical_path((2, 1)) == canonical_path((2, 1))


@pytest.fixture
def postgres_url(request):
    if POSTGRES_URL:
        return POSTGRES_URL
    if (
        os.environ.get("CI", "").lower() == "true"
        and "postgres_integration" in request.config.option.markexpr
    ):
        pytest.fail("CI requires ADVISOR_TEST_POSTGRES_URL for D13 PostgreSQL tests")
    pytest.skip("ADVISOR_TEST_POSTGRES_URL is not configured")


async def close_connection(connection):
    try:
        await asyncio.wait_for(connection.close(), timeout=3)
    except (TimeoutError, asyncio.CancelledError):
        connection.terminate()
        raise


@asynccontextmanager
async def isolated_schema(url):
    import asyncpg

    admin = await asyncpg.connect(dsn=url, timeout=5)
    schema = f"advisor_d13_{uuid4().hex}"
    try:
        version = int(await admin.fetchval("SHOW server_version_num"))
        assert 170000 <= version < 180000, f"D13 requires PostgreSQL 17, found {version}"
        await admin.execute(f'CREATE SCHEMA "{schema}"')
        try:
            yield schema
        finally:
            await admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    finally:
        await close_connection(admin)


@asynccontextmanager
async def scoped_connection(url, schema):
    import asyncpg

    connection = await asyncpg.connect(dsn=url, timeout=5)
    try:
        await connection.execute(f'SET search_path TO "{schema}", pg_catalog')
        assert await connection.fetchval("SELECT current_schema()") == schema
        yield connection
    finally:
        await close_connection(connection)


CREATE_SLOTS = """
    CREATE TABLE effective_slots (
        portfolio TEXT NOT NULL,
        executed_at TIMESTAMPTZ NOT NULL,
        effective_path BIGINT[] NOT NULL,
        origin TEXT NOT NULL,
        CONSTRAINT canonical_path CHECK (
            CASE WHEN cardinality(effective_path) > 0
                AND array_ndims(effective_path) = 1
                AND array_lower(effective_path, 1) = 1
            THEN array_position(effective_path, NULL) IS NULL
            ELSE FALSE END
        ),
        CONSTRAINT one_effective_slot UNIQUE (portfolio, executed_at, effective_path)
    )
"""


async def reserve(connection, portfolio, executed_at, path, origin="v5"):
    await connection.execute(
        "INSERT INTO effective_slots VALUES ($1, $2, $3::BIGINT[], $4)",
        portfolio,
        executed_at,
        list(canonical_path(path)),
        origin,
    )


@POSTGRES
def test_postgres_array_order_and_canonical_shape(postgres_url):
    import asyncpg

    async def scenario():
        async with isolated_schema(postgres_url) as schema:
            async with scoped_connection(postgres_url, schema) as connection:
                await connection.execute(CREATE_SLOTS)
                paths = [
                    (3,),
                    (2, 1),
                    (2,),
                    (2, 2),
                    (INT64_MAX,),
                    (0,),
                    (-1,),
                    (INT64_MIN,),
                    (2, 1, 0),
                ]
                timestamp = datetime(2026, 1, 1, tzinfo=UTC)
                for path in paths:
                    await reserve(connection, "p", timestamp, path)
                rows = await connection.fetch(
                    "SELECT effective_path FROM effective_slots ORDER BY effective_path"
                )
                assert [tuple(row[0]) for row in rows] == python_order(paths)
                assert await connection.fetchval(
                    "SELECT $1::BIGINT[] = $2::BIGINT[]", [2, 1], [2, 1]
                )

                invalid = [
                    "'{}'::BIGINT[]",
                    "ARRAY[1,NULL]::BIGINT[]",
                    "ARRAY[[1,2],[3,4]]::BIGINT[]",
                    "'[0:1]={2,3}'::BIGINT[]",
                ]
                for expression in invalid:
                    with pytest.raises(asyncpg.CheckViolationError):
                        async with connection.transaction():
                            await connection.execute(
                                "INSERT INTO effective_slots VALUES "
                                f"('invalid', '2026-01-01', {expression}, 'v5')"
                            )
                assert (
                    await connection.fetchval(
                        "SELECT count(*) FROM effective_slots WHERE portfolio='invalid'"
                    )
                    == 0
                )

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


@POSTGRES
def test_postgres_btree_orders_without_sort(postgres_url):
    async def scenario():
        async with isolated_schema(postgres_url) as schema:
            async with scoped_connection(postgres_url, schema) as connection:
                await connection.execute(CREATE_SLOTS)
                await connection.execute(
                    "CREATE INDEX effective_path_btree "
                    "ON effective_slots USING btree (effective_path)"
                )
                paths = [(3,), (2, 1), (2,), (-2,), (0,), (2, 0)]
                timestamp = datetime(2026, 1, 1, tzinfo=UTC)
                for path in paths:
                    await reserve(connection, "p", timestamp, path)
                query = "SELECT effective_path FROM effective_slots ORDER BY effective_path"
                assert [tuple(row[0]) for row in await connection.fetch(query)] == python_order(
                    paths
                )

                async with connection.transaction():
                    await connection.execute("SET LOCAL enable_seqscan = off")
                    await connection.execute("SET LOCAL enable_sort = off")
                    plan = json.loads(
                        await connection.fetchval(f"EXPLAIN (ANALYZE, FORMAT JSON) {query}")
                    )[0]["Plan"]
                    nodes = []

                    def visit(node):
                        nodes.append(node)
                        for child in node.get("Plans", []):
                            visit(child)

                    visit(plan)
                    assert any(
                        node["Node Type"] in {"Index Scan", "Index Only Scan"}
                        and node.get("Index Name") == "effective_path_btree"
                        for node in nodes
                    ), plan
                    assert not any(
                        node["Node Type"] in {"Sort", "Incremental Sort"} for node in nodes
                    )
                    assert [tuple(row[0]) for row in await connection.fetch(query)] == python_order(
                        paths
                    )

    asyncio.run(asyncio.wait_for(scenario(), timeout=20))


@POSTGRES
def test_postgres_slots_late_insertion_corrections_and_concurrency(postgres_url):
    import asyncpg

    async def scenario():
        async with isolated_schema(postgres_url) as schema:
            async with scoped_connection(postgres_url, schema) as first:
                await first.execute(CREATE_SLOTS)
                await first.execute(
                    "CREATE TABLE correction_versions ("
                    "portfolio TEXT NOT NULL, executed_at TIMESTAMPTZ NOT NULL, "
                    "effective_path BIGINT[] NOT NULL, version INTEGER NOT NULL, "
                    "UNIQUE (portfolio, executed_at, effective_path, version))"
                )
                timestamp = datetime(2026, 1, 1, tzinfo=UTC)
                async with first.transaction():
                    await reserve(first, "p", timestamp, (2,), "v4")
                    await reserve(first, "p", timestamp, (3,), "v5")
                await reserve(first, "p", timestamp, (2, 1))
                ordered = await first.fetch(
                    "SELECT effective_path FROM effective_slots "
                    "WHERE portfolio='p' AND executed_at=$1 ORDER BY effective_path",
                    timestamp,
                )
                assert [tuple(row[0]) for row in ordered] == python_order([(2,), (3,), (2, 1)])
                with pytest.raises(asyncpg.UniqueViolationError) as collision:
                    await reserve(first, "p", timestamp, (2,), "v5")
                assert collision.value.sqlstate == "23505"
                await reserve(first, "p", timestamp + timedelta(days=1), (2,), "v5")
                assert (
                    await first.fetchval(
                        "SELECT count(*) FROM effective_slots "
                        "WHERE effective_path=ARRAY[2]::BIGINT[]"
                    )
                    == 2
                )
                for version in (1, 2):
                    await first.execute(
                        "INSERT INTO correction_versions VALUES ('p', $1, ARRAY[2]::BIGINT[], $2)",
                        timestamp,
                        version,
                    )
                assert await first.fetchval("SELECT count(*) FROM correction_versions") == 2

                try:
                    async with first.transaction():
                        await reserve(first, "rollback", timestamp, (9,))
                        raise RuntimeError("force rollback")
                except RuntimeError:
                    pass
                assert (
                    await first.fetchval(
                        "SELECT count(*) FROM effective_slots WHERE portfolio='rollback'"
                    )
                    == 0
                )

                async with scoped_connection(postgres_url, schema) as second:
                    transaction = first.transaction()
                    await transaction.start()
                    duplicate_task = None
                    attempt_started = asyncio.Event()
                    try:
                        await reserve(first, "race", timestamp, (7,))

                        async def conflicting_insert():
                            async with second.transaction():
                                await second.execute("SET LOCAL lock_timeout = '1000ms'")
                                attempt_started.set()
                                await second.execute(
                                    "INSERT INTO effective_slots VALUES "
                                    "('race', $1, ARRAY[7]::BIGINT[], 'v5')",
                                    timestamp,
                                )

                        duplicate_task = asyncio.create_task(conflicting_insert())
                        await asyncio.wait_for(attempt_started.wait(), timeout=3)
                        with pytest.raises(asyncpg.LockNotAvailableError) as conflict:
                            await asyncio.wait_for(duplicate_task, timeout=4)
                        assert conflict.value.sqlstate == "55P03"
                        assert first.is_in_transaction()
                        await transaction.commit()
                    finally:
                        if duplicate_task is not None and not duplicate_task.done():
                            duplicate_task.cancel()
                        if duplicate_task is not None:
                            await asyncio.gather(duplicate_task, return_exceptions=True)
                        if first.is_in_transaction():
                            await transaction.rollback()
                    assert (
                        await first.fetchval(
                            "SELECT count(*) FROM effective_slots WHERE portfolio='race'"
                        )
                        == 1
                    )

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
