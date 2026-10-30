"""Real PostgreSQL tests for atomic financial ledger persistence."""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from test_postgres_storage import isolated_postgres_schema

from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    PayloadError,
    PersistenceError,
    StateConflictError,
)
from advanced_trading_advisor.domain.validation import DomainValidationError, ResourceNotFoundError
from advanced_trading_advisor.financial.ledger import (
    BuyFacts,
    Completeness,
    OpeningFacts,
    SellFacts,
)
from advanced_trading_advisor.financial.repositories import (
    MAX_PERSISTED_SEQUENCE,
    FinancialPortfolio,
)
from advanced_trading_advisor.financial.values import (
    AssetId,
    AssetQuantity,
    OperationId,
    USDTAmount,
    UTCTimestamp,
)
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_financial_repository import (
    PostgresFinancialLedgerRepository,
    _next_sequence,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        not os.environ.get("ADVISOR_TEST_POSTGRES_URL"),
        reason="ADVISOR_TEST_POSTGRES_URL is not configured",
    ),
]

T0 = datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=UTC)


def at(days=0, microseconds=0):
    return UTCTimestamp(T0 + timedelta(days=days, microseconds=microseconds))


def opening(asset="USDT", quantity="100", cost="100", *, incomplete="0"):
    return OpeningFacts(
        AssetId(asset),
        AssetQuantity(Decimal(quantity)),
        USDTAmount(Decimal(cost)) if cost is not None else None,
        AssetQuantity(Decimal(incomplete)),
        Completeness.UNKNOWN if cost is None else Completeness.COMPLETE,
    )


def buy(quantity="1", principal="30"):
    return BuyFacts(
        AssetId("BTC"), AssetQuantity(Decimal(quantity)), USDTAmount(Decimal(principal))
    )


def sell(quantity="1", proceeds="40"):
    return SellFacts(
        AssetId("BTC"), AssetQuantity(Decimal(quantity)), USDTAmount(Decimal(proceeds))
    )


async def counts(connection, owner):
    return (
        await connection.fetchval(
            "SELECT count(*) FROM financial_operations WHERE portfolio_id=$1", owner
        ),
        await connection.fetchval(
            "SELECT count(*) FROM financial_postings WHERE portfolio_id=$1", owner
        ),
        await connection.fetchval(
            "SELECT last_sequence FROM financial_portfolios WHERE id=$1", owner
        ),
    )


async def start_together(items, call):
    """Release only after every task has reached the pre-append barrier."""
    ready = asyncio.Event()
    release = asyncio.Event()
    arrived = 0

    async def participant(item):
        nonlocal arrived
        arrived += 1
        if arrived == len(items):
            ready.set()
        await release.wait()
        return await call(item)

    tasks = [asyncio.create_task(participant(item)) for item in items]
    try:
        await asyncio.wait_for(ready.wait(), 5)
        release.set()
        return await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), 20)
    finally:
        release.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_create_load_append_replay_and_idempotency():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            assert await repo.load_portfolio(owner) is None
            with pytest.raises(ResourceNotFoundError):
                await repo.load_operations(owner)
            created = await repo.create_portfolio(owner)
            assert created.id == owner and created.last_sequence == 0
            assert await repo.load_portfolio(owner) == created
            assert await repo.load_operations(owner) == ()
            with pytest.raises(ConflictError):
                await repo.create_portfolio(owner)
            with pytest.raises(DomainValidationError):
                await repo.create_portfolio(UUID(int=0))

            first_id, second_id, third_id = (OperationId(uuid4()) for _ in range(3))
            first = await repo.append(owner, first_id, at(), opening())
            second = await repo.append(owner, second_id, at(1), buy())
            third = await repo.append(owner, third_id, at(2), sell())
            assert [op.sequence.value for op in (first, second, third)] == [1, 2, 3]
            assert [op.id for op in await repo.load_operations(owner)] == [
                first_id,
                second_id,
                third_id,
            ]
            assert await counts(connection, owner) == (3, 5, 3)

            retry = await repo.append(owner, second_id, at(1), buy("1.0", "30.00"))
            assert retry == second
            assert retry.recorded_at == second.recorded_at
            assert await counts(connection, owner) == (3, 5, 3)
            invalid_retry = buy("1", "30")
            object.__setattr__(invalid_retry.quantity, "value", Decimal("1." + "0" * 25))
            with pytest.raises(DomainValidationError):
                await repo.append(owner, second_id, at(1), invalid_retry)
            invalid_asset = buy("1", "30")
            object.__setattr__(invalid_asset.asset, "value", "btc")
            with pytest.raises(DomainValidationError):
                await repo.append(owner, second_id, at(1), invalid_asset)
            assert await counts(connection, owner) == (3, 5, 3)
            assert (await repo.load_operations(owner))[1].recorded_at == second.recorded_at
            with pytest.raises(ConflictError):
                await repo.append(owner, second_id, at(1), buy("1", "31"))
            with pytest.raises(ConflictError):
                await repo.append(owner, second_id, at(2), buy())
            assert await counts(connection, owner) == (3, 5, 3)

    asyncio.run(scenario())


def test_backdated_replay_sequence_and_rollback():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            await repo.create_portfolio(owner)
            cash = await repo.append(owner, OperationId(uuid4()), at(1), opening())
            purchase = await repo.append(owner, OperationId(uuid4()), at(3), buy())
            backdated = await repo.append(
                owner, OperationId(uuid4()), at(2), opening("BTC", "0.5", "5")
            )
            assert [op.id for op in await repo.load_operations(owner)] == [
                cash.id,
                backdated.id,
                purchase.id,
            ]
            assert [op.sequence.value for op in await repo.load_operations(owner)] == [1, 3, 2]
            assert await counts(connection, owner) == (3, 4, 3)

            with pytest.raises(StateConflictError):
                await repo.append(owner, OperationId(uuid4()), at(), buy("1", "90"))
            with pytest.raises(StateConflictError):
                await repo.append(owner, OperationId(uuid4()), at(4), sell("2", "1"))
            assert await counts(connection, owner) == (3, 4, 3)
            next_op = await repo.append(owner, OperationId(uuid4()), at(4), buy("0.1", "1"))
            assert next_op.sequence.value == 4
            assert await counts(connection, owner) == (4, 6, 4)

    asyncio.run(scenario())


def test_posting_insert_failure_rolls_back_and_next_sequence_has_no_gap():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            await repo.create_portfolio(owner)
            await connection.execute(
                "ALTER TABLE financial_postings ADD CONSTRAINT test_reject_postings CHECK (false)"
            )
            with pytest.raises(PersistenceError):
                await repo.append(owner, OperationId(uuid4()), at(), opening())
            assert await counts(connection, owner) == (0, 0, 0)
            await connection.execute(
                "ALTER TABLE financial_postings DROP CONSTRAINT test_reject_postings"
            )
            accepted = await repo.append(owner, OperationId(uuid4()), at(), opening())
            assert accepted.sequence.value == 1
            assert await counts(connection, owner) == (1, 1, 1)

    asyncio.run(scenario())


def test_corrupt_history_max_sequence_and_portfolio_isolation():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner, other = uuid4(), uuid4()
            await repo.create_portfolio(owner)
            await repo.create_portfolio(other)
            original = await repo.append(owner, OperationId(uuid4()), at(), opening())
            assert await repo.load_operations(other) == ()
            with pytest.raises(ResourceNotFoundError):
                await repo.append(uuid4(), OperationId(uuid4()), at(), opening())
            await connection.execute(
                "UPDATE financial_portfolios SET last_sequence=9223372036854775807 WHERE id=$1",
                owner,
            )
            with pytest.raises(PayloadError):
                await repo.append(owner, OperationId(uuid4()), at(1), buy())
            with pytest.raises(PayloadError):
                await repo.append(owner, original.id, at(), opening())
            with pytest.raises(PayloadError):
                await repo.append(owner, original.id, at(), opening("USDT", "101", "101"))
            assert await counts(connection, owner) == (1, 1, 9223372036854775807)
            await connection.execute(
                "UPDATE financial_portfolios SET last_sequence=1 WHERE id=$1", owner
            )
            assert await repo.append(owner, original.id, at(), opening()) == original
            await connection.execute(
                "UPDATE financial_portfolios SET last_sequence=0 WHERE id=$1", owner
            )
            with pytest.raises(PayloadError):
                await repo.load_operations(owner)
            with pytest.raises(PayloadError):
                await repo.append(owner, OperationId(uuid4()), at(1), buy())
            with pytest.raises(PayloadError):
                await repo.append(owner, original.id, at(), opening())
            assert await counts(connection, owner) == (1, 1, 0)
            await connection.execute(
                "UPDATE financial_portfolios SET last_sequence=1 WHERE id=$1", other
            )
            with pytest.raises(PayloadError):
                await repo.load_operations(other)
            with pytest.raises(PayloadError):
                await repo.append(other, OperationId(uuid4()), at(), opening())
            assert await counts(connection, other) == (0, 0, 1)

    asyncio.run(scenario())


def test_bigint_exhaustion_boundary_without_impossible_database_fixture():
    portfolio = FinancialPortfolio(uuid4(), MAX_PERSISTED_SEQUENCE)
    with pytest.raises(StateConflictError, match="sequence is exhausted"):
        _next_sequence(portfolio)
    assert portfolio.last_sequence == MAX_PERSISTED_SEQUENCE


def test_lock_contention_is_visible_in_postgresql():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            await repo.create_portfolio(owner)
            task = None
            try:
                async with database.connection() as observer:
                    async with connection.transaction():
                        await connection.fetchrow(
                            "SELECT id FROM financial_portfolios WHERE id=$1 FOR UPDATE", owner
                        )
                        task = asyncio.create_task(
                            repo.append(owner, OperationId(uuid4()), at(), opening())
                        )
                        deadline = asyncio.get_running_loop().time() + 5
                        waiting = False
                        while asyncio.get_running_loop().time() < deadline:
                            waiting = await observer.fetchval(
                                """SELECT EXISTS (
                                    SELECT 1 FROM pg_stat_activity
                                    WHERE datname = current_database()
                                      AND pid <> pg_backend_pid()
                                      AND query LIKE '%financial_portfolios%FOR UPDATE%'
                                      AND wait_event_type = 'Lock')"""
                            )
                            if waiting:
                                break
                            await asyncio.sleep(0.01)
                        assert waiting, "Competing append never waited on a PostgreSQL lock"
                        assert not task.done()
                accepted = await asyncio.wait_for(task, 5)
                assert accepted.sequence.value == 1
                assert await counts(connection, owner) == (1, 1, 1)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_persisted_financially_invalid_history_is_rejected():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            operation_id = uuid4()
            await repo.create_portfolio(owner)
            async with connection.transaction():
                await connection.execute(
                    """INSERT INTO financial_operations
                    (portfolio_id,operation_id,sequence,operation_type,executed_at,recorded_at,
                     asset,quantity,principal_usdt)
                    VALUES ($1,$2,1,'buy',$3,$3,'BTC',1,10)""",
                    owner,
                    operation_id,
                    T0,
                )
                await connection.execute(
                    """INSERT INTO financial_postings
                    (portfolio_id,operation_id,ordinal,asset,delta,role)
                    VALUES ($1,$2,1,'BTC',1,'traded_asset'),
                           ($1,$2,2,'USDT',-10,'settlement')""",
                    owner,
                    operation_id,
                )
                await connection.execute(
                    "UPDATE financial_portfolios SET last_sequence=1 WHERE id=$1", owner
                )
            with pytest.raises(PayloadError, match="Persisted financial history is invalid"):
                await repo.load_operations(owner)
            with pytest.raises(PayloadError, match="Persisted financial history is invalid"):
                await repo.append(owner, OperationId(uuid4()), at(1), opening())
            with pytest.raises(PayloadError, match="Persisted financial history is invalid"):
                await repo.append(owner, OperationId(operation_id), at(), buy("1", "10"))
            assert await counts(connection, owner) == (1, 2, 1)

    asyncio.run(scenario())


def test_concurrent_distinct_identical_and_conflicting_requests():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            await repo.create_portfolio(owner)
            await repo.append(owner, OperationId(uuid4()), at(), opening("USDT", "1000", "1000"))

            ids = [OperationId(uuid4()) for _ in range(8)]

            async def distinct(identifier):
                return await repo.append(owner, identifier, at(1), buy("1", "10"))

            results = await start_together(ids, distinct)
            assert {op.sequence.value for op in results} == set(range(2, 10))
            assert await counts(connection, owner) == (9, 17, 9)

            shared = OperationId(uuid4())

            async def same(principal):
                return await repo.append(owner, shared, at(2), buy("1", principal))

            identical = await start_together(["10"] * 6, same)
            assert all(op == identical[0] for op in identical)
            assert all(op.recorded_at == identical[0].recorded_at for op in identical)
            assert await counts(connection, owner) == (10, 19, 10)

            conflicting = await start_together(["10", "11"], same)
            assert sum(isinstance(item, ConflictError) for item in conflicting) == 1
            assert (
                sum(item == identical[0] for item in conflicting if not isinstance(item, Exception))
                == 1
            )
            assert await counts(connection, owner) == (10, 19, 10)

            fresh_id = OperationId(uuid4())

            async def competing(principal):
                return await repo.append(owner, fresh_id, at(3), buy("1", principal))

            fresh_results = await start_together(["10", "11"], competing)
            assert sum(isinstance(item, ConflictError) for item in fresh_results) == 1
            accepted = [item for item in fresh_results if not isinstance(item, Exception)]
            assert len(accepted) == 1 and accepted[0].sequence.value == 11
            assert await counts(connection, owner) == (11, 21, 11)

    asyncio.run(scenario())


def test_concurrent_spending_cannot_overdraw_portfolio():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            await repo.create_portfolio(owner)
            await repo.append(owner, OperationId(uuid4()), at(), opening("USDT", "15", "15"))

            async def spend(_):
                return await repo.append(owner, OperationId(uuid4()), at(1), buy("1", "10"))

            results = await start_together([None, None], spend)
            assert sum(isinstance(item, StateConflictError) for item in results) == 1
            assert sum(not isinstance(item, Exception) for item in results) == 1
            assert await counts(connection, owner) == (2, 3, 2)

    asyncio.run(scenario())


def test_decimal_timestamp_and_known_zero_round_trip():
    async def scenario():
        async with isolated_postgres_schema() as (connection, schema):
            database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
            await database.initialize()
            repo = PostgresFinancialLedgerRepository(database)
            owner = uuid4()
            await repo.create_portfolio(owner)
            amount = "9" * 36 + "." + "9" * 24
            first = await repo.append(
                owner, OperationId(uuid4()), at(microseconds=1), opening("BTC", amount, "0")
            )
            stored = (await repo.load_operations(owner))[0]
            assert stored == first
            assert stored.executed_at.value.microsecond == 123457
            assert stored.facts.quantity.value.as_tuple() == Decimal(amount).as_tuple()
            assert stored.facts.known_cost_usdt.value == Decimal("0")
            assert await counts(connection, owner) == (1, 1, 1)
            with pytest.raises(DomainValidationError):
                await repo.append(owner, OperationId(uuid4()), at(1), opening("BTC", "1e-25", "0"))

    asyncio.run(scenario())
