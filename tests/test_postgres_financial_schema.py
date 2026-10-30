"""Real PostgreSQL contracts for financial schema v4; never use production storage."""

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import asyncpg
import pytest
from test_financial_domain import opening as domain_opening
from test_postgres_storage import isolated_postgres_schema

from advanced_trading_advisor.domain.persistence import PersistenceError
from advanced_trading_advisor.financial.values import USDTAmount
from advanced_trading_advisor.storage.financial_mapper import operation_from_rows, operation_to_rows
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_schema import POSTGRES_MIGRATIONS

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        not os.environ.get("ADVISOR_TEST_POSTGRES_URL"),
        reason="ADVISOR_TEST_POSTGRES_URL is not configured",
    ),
]

NOW = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)


async def portfolio(connection):
    identifier = uuid4()
    await connection.execute("INSERT INTO financial_portfolios(id) VALUES ($1)", identifier)
    return identifier


async def opening(
    connection, portfolio_id, *, quantity=Decimal("1"), cost=Decimal("1"), at=NOW, sequence=1
):
    identifier = uuid4()
    await connection.execute(
        """INSERT INTO financial_operations
        (portfolio_id,operation_id,sequence,operation_type,executed_at,recorded_at,
         asset,quantity,known_cost_usdt,incomplete_quantity,completeness)
        VALUES ($1,$2,$3,'opening_balance',$4,$4,'USDT',$5,$6,0,'complete')""",
        portfolio_id,
        identifier,
        sequence,
        at,
        quantity,
        cost,
    )
    return identifier


async def posting(
    connection,
    portfolio_id,
    operation_id,
    *,
    ordinal=1,
    delta=Decimal("1"),
    asset="USDT",
    role="opening",
):
    await connection.execute(
        """INSERT INTO financial_postings
        (portfolio_id,operation_id,ordinal,asset,delta,role)
        VALUES ($1,$2,$3,$4,$5,$6)""",
        portfolio_id,
        operation_id,
        ordinal,
        asset,
        delta,
        role,
    )


async def trade(connection, portfolio_id, kind):
    identifier = uuid4()
    await connection.execute(
        """INSERT INTO financial_operations
        (portfolio_id,operation_id,sequence,operation_type,executed_at,recorded_at,
         asset,quantity,principal_usdt,proceeds_usdt)
        VALUES ($1,$2,1,$3,$4,$4,'BTC',2,$5,$6)""",
        portfolio_id,
        identifier,
        kind,
        NOW,
        Decimal("10") if kind == "buy" else None,
        Decimal("10") if kind == "sell" else None,
    )
    return identifier


def test_v4_empty_init_and_repeat_init():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await db.initialize()
            await db.initialize()
            assert await connection.fetchval("SELECT version FROM schema_metadata") == 4
            assert await connection.fetchval("SELECT count(*) FROM financial_portfolios") == 0
            for table in ("financial_portfolios", "financial_operations", "financial_postings"):
                assert await connection.fetchval("SELECT to_regclass($1)", table) is not None

    asyncio.run(scenario())


def test_mapper_round_trip_with_asyncpg_records():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            operation = domain_opening(1, "USDT", "1e-24", "1e-24")
            row, postings = operation_to_rows(operation, owner)
            async with connection.transaction():
                await connection.execute(
                    """INSERT INTO financial_operations
                    (portfolio_id,operation_id,sequence,operation_type,executed_at,recorded_at,
                     asset,quantity,known_cost_usdt,incomplete_quantity,completeness,
                     principal_usdt,proceeds_usdt)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                    *(
                        row[key]
                        for key in (
                            "portfolio_id",
                            "operation_id",
                            "sequence",
                            "operation_type",
                            "executed_at",
                            "recorded_at",
                            "asset",
                            "quantity",
                            "known_cost_usdt",
                            "incomplete_quantity",
                            "completeness",
                            "principal_usdt",
                            "proceeds_usdt",
                        )
                    ),
                )
                for item in postings:
                    await connection.execute(
                        """INSERT INTO financial_postings
                        (portfolio_id,operation_id,ordinal,asset,delta,role)
                        VALUES ($1,$2,$3,$4,$5,$6)""",
                        *(
                            item[key]
                            for key in (
                                "portfolio_id",
                                "operation_id",
                                "ordinal",
                                "asset",
                                "delta",
                                "role",
                            )
                        ),
                    )
            stored = await connection.fetchrow(
                "SELECT * FROM financial_operations WHERE portfolio_id=$1", owner
            )
            stored_postings = await connection.fetch(
                "SELECT * FROM financial_postings WHERE portfolio_id=$1 ORDER BY ordinal", owner
            )
            assert operation_from_rows(stored, stored_postings, owner) == operation

    asyncio.run(scenario())


def test_v3_upgrade_preserves_legacy_rows_and_repeat_init():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            async with connection.transaction():
                for version in (1, 2, 3):
                    for statement in POSTGRES_MIGRATIONS[version]:
                        await connection.execute(statement)
                await connection.execute("INSERT INTO schema_metadata VALUES (1,3)")
                legacy_id = uuid4()
                await connection.execute(
                    """INSERT INTO portfolio_snapshots
                    (snapshot_id, confirmed_at, payload_json, schema_version, created_at)
                    VALUES ($1,$2,$3,3,$2)""",
                    legacy_id,
                    NOW,
                    '{"legacy":"unchanged"}',
                )
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await db.initialize()
            assert await connection.fetchval("SELECT version FROM schema_metadata") == 4
            assert (
                await connection.fetchval(
                    "SELECT payload_json FROM portfolio_snapshots WHERE snapshot_id=$1",
                    legacy_id,
                )
                == '{"legacy":"unchanged"}'
            )
            await db.initialize()
            assert await connection.fetchval("SELECT version FROM schema_metadata") == 4

    asyncio.run(scenario())


def test_v4_upgrade_failure_rolls_back(monkeypatch):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            async with connection.transaction():
                for version in (1, 2, 3):
                    for statement in POSTGRES_MIGRATIONS[version]:
                        await connection.execute(statement)
                await connection.execute("INSERT INTO schema_metadata VALUES (1,3)")
            monkeypatch.setitem(
                POSTGRES_MIGRATIONS, 4, (*POSTGRES_MIGRATIONS[4][:2], "INVALID MIGRATION SQL")
            )
            db = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            with pytest.raises(PersistenceError):
                await db.initialize()
            assert await connection.fetchval("SELECT version FROM schema_metadata") == 3
            assert await connection.fetchval("SELECT to_regclass('financial_portfolios')") is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value,valid",
    [
        ("0", True),
        ("-0", True),
        ("1e59", True),
        ("9" * 60, True),
        ("1e60", False),
        ("9" * 61, False),
        ("1e-24", True),
        ("1e-25", False),
        ("9" * 36 + "." + "9" * 24, True),
        ("9" * 37 + "." + "9" * 24, False),
        ("1.000000000000000000000000", True),
        ("1.0000000000000000000000000", False),
        ("NaN", False),
        ("Infinity", False),
        ("-Infinity", False),
    ],
)
def test_decimal_predicate_on_real_postgres(value, valid):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            assert (
                await connection.fetchval(
                    "SELECT financial_decimal_valid($1::NUMERIC)", Decimal(value)
                )
                is valid
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "text",
    [
        "1e59",
        "9" * 60,
        "1e-24",
        "9" * 36 + "." + "9" * 24,
        "1.000000000000000000000000",
    ],
)
def test_financial_numeric_columns_round_trip_without_value_loss(text):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            amount = Decimal(text)
            async with connection.transaction():
                op = await opening(connection, owner, quantity=amount, cost=amount)
                await posting(connection, owner, op, delta=amount)
            stored = await connection.fetchrow(
                """SELECT o.quantity, o.known_cost_usdt, p.delta
                FROM financial_operations o JOIN financial_postings p
                USING (portfolio_id, operation_id)
                WHERE o.portfolio_id=$1 AND o.operation_id=$2""",
                owner,
                op,
            )
            assert tuple(stored) == (amount, amount, amount)

    asyncio.run(scenario())


@pytest.mark.parametrize("text", ["1e60", "9" * 61, "1e-25"])
def test_financial_numeric_columns_reject_out_of_policy_values(text):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            amount = Decimal(text)
            with pytest.raises(asyncpg.PostgresError):
                async with connection.transaction():
                    op = await opening(connection, owner, quantity=amount, cost=amount)
                    await posting(connection, owner, op, delta=amount)
            assert await connection.fetchval("SELECT count(*) FROM financial_operations") == 0

    asyncio.run(scenario())


def test_posting_completeness_immutability_and_round_trip():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            first = await portfolio(connection)
            second = await portfolio(connection)
            assert (
                await connection.fetchval(
                    "SELECT last_sequence FROM financial_portfolios WHERE id=$1", first
                )
                == 0
            )
            with pytest.raises(asyncpg.PostgresError):
                async with connection.transaction():
                    await opening(connection, first)
            assert await connection.fetchval("SELECT count(*) FROM financial_operations") == 0
            async with connection.transaction():
                op = await opening(connection, first)
                await posting(connection, first, op)
            row = await connection.fetchrow(
                "SELECT quantity,executed_at,recorded_at "
                "FROM financial_operations WHERE operation_id=$1",
                op,
            )
            assert row["quantity"] == Decimal("1")
            assert row["executed_at"] == row["recorded_at"] == NOW
            assert await connection.fetchval("SELECT count(*) FROM financial_operations") == 1
            with pytest.raises(asyncpg.PostgresError):
                async with connection.transaction():
                    await posting(connection, first, op, ordinal=2)
            assert await connection.fetchval("SELECT count(*) FROM financial_postings") == 1
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute(
                    "UPDATE financial_operations SET quantity=2 WHERE operation_id=$1", op
                )
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute(
                    "DELETE FROM financial_operations WHERE operation_id=$1", op
                )
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute(
                    "UPDATE financial_postings SET delta=2 WHERE operation_id=$1", op
                )
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute("DELETE FROM financial_postings WHERE operation_id=$1", op)
            with pytest.raises(asyncpg.PostgresError):
                async with connection.transaction():
                    other = await opening(connection, second)
                    await posting(connection, first, other)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM financial_operations WHERE portfolio_id=$1", second
                )
                == 0
            )

    asyncio.run(scenario())


def test_truncate_cannot_remove_immutable_financial_facts():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            async with connection.transaction():
                op = await opening(connection, owner)
                await posting(connection, owner, op)
            for statement in (
                "TRUNCATE financial_postings",
                "TRUNCATE financial_operations",
                "TRUNCATE financial_postings CASCADE",
                "TRUNCATE financial_operations CASCADE",
            ):
                with pytest.raises(asyncpg.PostgresError) as rejected:
                    await connection.execute(statement)
                if statement != "TRUNCATE financial_operations":
                    assert "Immutable advisor record" in str(rejected.value)
                assert await connection.fetchval("SELECT count(*) FROM financial_operations") == 1
                assert await connection.fetchval("SELECT count(*) FROM financial_postings") == 1

    asyncio.run(scenario())


def test_multiple_operations_commit_or_roll_back_together():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            async with connection.transaction():
                first = await opening(connection, owner, sequence=1)
                await posting(connection, owner, first)
                second = await opening(connection, owner, sequence=2)
                await posting(connection, owner, second)
            rows = await connection.fetch(
                "SELECT operation_id,sequence FROM financial_operations "
                "WHERE portfolio_id=$1 ORDER BY sequence",
                owner,
            )
            assert [(row["operation_id"], row["sequence"]) for row in rows] == [
                (first, 1),
                (second, 2),
            ]
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM financial_postings WHERE portfolio_id=$1", owner
                )
                == 2
            )
            with pytest.raises(asyncpg.PostgresError):
                async with connection.transaction():
                    third = await opening(connection, owner, sequence=3)
                    await posting(connection, owner, third)
                    fourth = await opening(connection, owner, sequence=4)
                    await posting(connection, owner, fourth, delta=Decimal("2"))
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM financial_operations WHERE portfolio_id=$1", owner
                )
                == 2
            )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM financial_postings WHERE portfolio_id=$1", owner
                )
                == 2
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("zero", [Decimal("-0"), Decimal("0")])
def test_signed_zero_round_trip_and_sign_constraints(zero):
    assert USDTAmount(zero).value == Decimal("0")

    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            op = uuid4()
            async with connection.transaction():
                await connection.execute(
                    """INSERT INTO financial_operations
                    (portfolio_id,operation_id,sequence,operation_type,executed_at,recorded_at,
                     asset,quantity,known_cost_usdt,incomplete_quantity,completeness)
                    VALUES ($1,$2,1,'opening_balance',$3,$3,'BTC',1,$4,0,'complete')""",
                    owner,
                    op,
                    NOW,
                    zero,
                )
                await posting(connection, owner, op, asset="BTC")
            stored = await connection.fetchval(
                "SELECT known_cost_usdt FROM financial_operations WHERE operation_id=$1", op
            )
            assert stored == zero == Decimal("0")
            assert stored.as_tuple().sign == 0
            assert await connection.fetchval("SELECT financial_decimal_valid($1::NUMERIC)", zero)
            with pytest.raises(asyncpg.CheckViolationError):
                await posting(connection, owner, op, ordinal=2, delta=zero)

    asyncio.run(scenario())


def test_sequence_boundaries_and_portfolio_isolation():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            other = await portfolio(connection)
            for invalid in (0, -1):
                with pytest.raises(asyncpg.PostgresError):
                    async with connection.transaction():
                        op = await opening(connection, owner, sequence=invalid)
                        await posting(connection, owner, op)
            maximum = 9223372036854775807
            async with connection.transaction():
                op = await opening(connection, owner, sequence=maximum)
                await posting(connection, owner, op)
                await connection.execute(
                    "UPDATE financial_portfolios SET last_sequence=$2 WHERE id=$1",
                    owner,
                    maximum,
                )
            assert (
                await connection.fetchval(
                    "SELECT sequence FROM financial_operations WHERE operation_id=$1", op
                )
                == maximum
            )
            with pytest.raises(asyncpg.PostgresError):
                await connection.execute(
                    "UPDATE financial_portfolios SET last_sequence=last_sequence+1 WHERE id=$1",
                    owner,
                )
            assert (
                await connection.fetchval(
                    "SELECT last_sequence FROM financial_portfolios WHERE id=$1", other
                )
                == 0
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["buy", "sell"])
def test_trade_postings_must_match_facts(kind):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            asset_delta = Decimal("2") if kind == "buy" else Decimal("-2")
            settlement_delta = Decimal("-10") if kind == "buy" else Decimal("10")
            with pytest.raises(asyncpg.PostgresError):
                async with connection.transaction():
                    op = await trade(connection, owner, kind)
                    await posting(
                        connection, owner, op, delta=asset_delta, asset="BTC", role="traded_asset"
                    )
                    await posting(
                        connection,
                        owner,
                        op,
                        ordinal=2,
                        delta=-settlement_delta,
                        role="settlement",
                    )
            async with connection.transaction():
                op = await trade(connection, owner, kind)
                await posting(
                    connection, owner, op, delta=asset_delta, asset="BTC", role="traded_asset"
                )
                await posting(
                    connection,
                    owner,
                    op,
                    ordinal=2,
                    delta=settlement_delta,
                    role="settlement",
                )
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM financial_postings WHERE portfolio_id=$1", owner
                )
                == 2
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("asset,delta", [("btc", "1"), ("USDT", "0")])
def test_invalid_posting_row_rejected_at_insert(asset, delta):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            with pytest.raises(asyncpg.CheckViolationError):
                async with connection.transaction():
                    op = await opening(connection, owner)
                    await posting(connection, owner, op, asset=asset, delta=Decimal(delta))
            assert await connection.fetchval("SELECT count(*) FROM financial_operations") == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "ordinal,asset,delta,role",
    [
        (2, "USDT", "1", "opening"),
        (1, "BTC", "1", "opening"),
        (1, "USDT", "2", "opening"),
        (1, "USDT", "1", "settlement"),
    ],
)
def test_contradictory_posting_rejected_at_commit(ordinal, asset, delta, role):
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            await PostgresDatabase(
                os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema
            ).initialize()
            owner = await portfolio(connection)
            inserted = False
            with pytest.raises(asyncpg.RaiseError, match="Invalid financial postings"):
                async with connection.transaction():
                    op = await opening(connection, owner)
                    await posting(
                        connection,
                        owner,
                        op,
                        ordinal=ordinal,
                        asset=asset,
                        delta=Decimal(delta),
                        role=role,
                    )
                    inserted = True
            assert inserted
            assert await connection.fetchval("SELECT count(*) FROM financial_operations") == 0

    asyncio.run(scenario())
