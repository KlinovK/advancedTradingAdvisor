"""Real PostgreSQL reliability tests for the financial ledger append boundary."""

import asyncio
import os
from contextlib import asynccontextmanager
from decimal import Decimal
from time import perf_counter
from uuid import uuid4

import pytest
from test_postgres_financial_repository import at, buy, counts, opening, sell, start_together
from test_postgres_storage import isolated_postgres_schema

from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    PersistenceError,
    StateConflictError,
)
from advanced_trading_advisor.financial.ledger import Completeness
from advanced_trading_advisor.financial.replay import replay
from advanced_trading_advisor.financial.values import AssetId, OperationId
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase
from advanced_trading_advisor.storage.postgres_financial_repository import (
    PostgresFinancialLedgerRepository,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.skipif(
        not os.environ.get("ADVISOR_TEST_POSTGRES_URL"),
        reason="ADVISOR_TEST_POSTGRES_URL is not configured",
    ),
]


@asynccontextmanager
async def ledger_fixture():
    async with isolated_postgres_schema() as (connection, schema):
        database = PostgresDatabase(os.environ["ADVISOR_TEST_POSTGRES_URL"], schema=schema)
        await database.initialize()
        yield connection, database, PostgresFinancialLedgerRepository(database)


async def append(repo, owner, day, facts, *, operation_id=None):
    return await repo.append(owner, operation_id or OperationId(uuid4()), at(day), facts)


async def persisted_state(repo, owner):
    operations = await repo.load_operations(owner)
    state = replay(operations)
    return operations, state


def assert_asset(state, symbol, quantity, cost, incomplete="0", average=None):
    asset = state.asset(AssetId(symbol))
    assert asset is not None
    assert asset.quantity == Decimal(quantity)
    assert asset.known_cost_usdt == Decimal(cost)
    assert asset.incomplete_quantity == Decimal(incomplete)
    assert asset.average_cost_usdt == (Decimal(average) if average is not None else None)


def test_cross_portfolio_append_proceeds_while_other_portfolio_is_row_locked():
    async def scenario():
        async with ledger_fixture() as (connection, _, repo):
            first, second = uuid4(), uuid4()
            await repo.create_portfolio(first)
            await repo.create_portfolio(second)
            async with connection.transaction():
                await connection.fetchrow(
                    "SELECT id FROM financial_portfolios WHERE id=$1 FOR UPDATE", first
                )
                task = asyncio.create_task(append(repo, second, 0, opening("USDT", "17", "17")))
                try:
                    second_operation = await asyncio.wait_for(task, 5)
                    assert second_operation.sequence.value == 1
                    assert await counts(connection, second) == (1, 1, 1)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            first_operation = await append(repo, first, 0, opening("BTC", "2", "0"))
            assert first_operation.sequence.value == 1
            assert first_operation.id != second_operation.id
            assert await counts(connection, first) == (1, 1, 1)
            assert await counts(connection, second) == (1, 1, 1)
            assert (
                await connection.fetchval(
                    "SELECT count(*) FROM financial_postings WHERE portfolio_id=$1", first
                )
                == 1
            )
            for owner in (first, second):
                operations, state = await persisted_state(repo, owner)
                assert len(operations) == 1
                assert len(state.ordered_operation_ids) == 1
            assert_asset((await persisted_state(repo, first))[1], "BTC", "2", "0", average="0")
            assert_asset((await persisted_state(repo, second))[1], "USDT", "17", "17", average="1")

    asyncio.run(scenario())


class InjectedConnection:
    """Test-only failure gate around a real asyncpg connection."""

    def __init__(self, connection, statement_prefix, after_matches, reached=None, release=None):
        self.connection = connection
        self.statement_prefix = statement_prefix
        self.after_matches = after_matches
        self.matches = 0
        self.reached = reached
        self.release = release

    async def fetchrow(self, statement, *args):
        return await self.connection.fetchrow(statement, *args)

    async def fetch(self, statement, *args):
        if self.statement_prefix == "SELECT * FROM financial_operations":
            await self._maybe_fail(statement)
        return await self.connection.fetch(statement, *args)

    async def execute(self, statement, *args):
        await self._maybe_fail(statement)
        return await self.connection.execute(statement, *args)

    async def _maybe_fail(self, statement):
        if statement.lstrip().startswith(self.statement_prefix):
            self.matches += 1
            if self.matches == self.after_matches:
                if self.reached is not None:
                    self.reached.set()
                if self.release is not None:
                    await self.release.wait()
                else:
                    raise RuntimeError("Controlled test-only transaction failure")


class InjectedDatabase:
    def __init__(self, database, statement_prefix, after_matches=1, reached=None, release=None):
        self.database = database
        self.statement_prefix = statement_prefix
        self.after_matches = after_matches
        self.reached = reached
        self.release = release

    async def validate_financial_schema(self, connection):
        await self.database.validate_financial_schema(connection.connection)

    @asynccontextmanager
    async def transaction(self):
        async with self.database.transaction() as connection:
            yield InjectedConnection(
                connection,
                self.statement_prefix,
                self.after_matches,
                self.reached,
                self.release,
            )


@pytest.mark.parametrize(
    "statement_prefix,after_matches",
    [
        ("INSERT INTO financial_operations", 1),
        ("INSERT INTO financial_postings", 2),
        ("UPDATE financial_portfolios", 1),
    ],
)
def test_injected_write_failure_rolls_back_and_retry_has_no_gap(statement_prefix, after_matches):
    async def scenario():
        async with ledger_fixture() as (connection, database, repo):
            owner = uuid4()
            operation_id = OperationId(uuid4())
            await repo.create_portfolio(owner)
            partial_postings = statement_prefix == "INSERT INTO financial_postings"
            if partial_postings:
                await append(repo, owner, 0, opening())
            baseline = (1, 1, 1) if partial_postings else (0, 0, 0)
            injected = PostgresFinancialLedgerRepository(
                InjectedDatabase(database, statement_prefix, after_matches)
            )
            with pytest.raises(RuntimeError, match="Controlled test-only"):
                await injected.append(
                    owner, operation_id, at(1), buy("1", "2") if partial_postings else opening()
                )
            assert await counts(connection, owner) == baseline
            accepted = await repo.append(
                owner, operation_id, at(1), buy("1", "2") if partial_postings else opening()
            )
            assert accepted.sequence.value == baseline[2] + 1
            assert await counts(connection, owner) == (
                baseline[0] + 1,
                baseline[1] + len(accepted.postings),
                baseline[2] + 1,
            )
            operations, state = await persisted_state(repo, owner)
            assert operations[-1] == accepted
            assert_asset(
                state,
                "USDT",
                "98" if partial_postings else "100",
                "98" if partial_postings else "100",
                average="1",
            )

    asyncio.run(scenario())


def test_deferred_commit_failure_rolls_back_and_retry_succeeds():
    async def scenario():
        async with ledger_fixture() as (connection, _, repo):
            owner = uuid4()
            operation_id = OperationId(uuid4())
            await repo.create_portfolio(owner)
            await connection.execute(
                """CREATE FUNCTION test_reject_financial_commit() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                    RAISE EXCEPTION 'test-only deferred commit rejection';
                END $$"""
            )
            await connection.execute(
                """CREATE CONSTRAINT TRIGGER test_reject_financial_commit
                AFTER UPDATE ON financial_portfolios DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION test_reject_financial_commit()"""
            )
            try:
                with pytest.raises(PersistenceError):
                    await repo.append(owner, operation_id, at(), opening())
                assert await counts(connection, owner) == (0, 0, 0)
            finally:
                await connection.execute(
                    "DROP TRIGGER test_reject_financial_commit ON financial_portfolios"
                )
                await connection.execute("DROP FUNCTION test_reject_financial_commit()")
            accepted = await repo.append(owner, operation_id, at(), opening())
            assert accepted.sequence.value == 1
            assert await counts(connection, owner) == (1, 1, 1)
            assert replay(await repo.load_operations(owner)).asset(AssetId("USDT")) is not None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "statement_prefix,after_matches",
    [
        ("SELECT * FROM financial_operations", 1),
        ("INSERT INTO financial_postings", 2),
    ],
)
def test_cancellation_during_history_or_posting_insert_rolls_back(statement_prefix, after_matches):
    async def scenario():
        async with ledger_fixture() as (connection, database, repo):
            owner = uuid4()
            await repo.create_portfolio(owner)
            partial_postings = statement_prefix == "INSERT INTO financial_postings"
            if partial_postings:
                await append(repo, owner, 0, opening())
            baseline = (1, 1, 1) if partial_postings else (0, 0, 0)
            reached, release = asyncio.Event(), asyncio.Event()
            operation_id = OperationId(uuid4())
            injected = PostgresFinancialLedgerRepository(
                InjectedDatabase(database, statement_prefix, after_matches, reached, release)
            )
            task = asyncio.create_task(
                injected.append(
                    owner, operation_id, at(1), buy("1", "2") if partial_postings else opening()
                )
            )
            try:
                await asyncio.wait_for(reached.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert await counts(connection, owner) == baseline
            accepted = await repo.append(
                owner, operation_id, at(1), buy("1", "2") if partial_postings else opening()
            )
            assert accepted.sequence.value == baseline[2] + 1
            assert await counts(connection, owner) == (
                baseline[0] + 1,
                baseline[1] + len(accepted.postings),
                baseline[2] + 1,
            )

    asyncio.run(scenario())


def test_cancellation_while_waiting_for_portfolio_lock_rolls_back():
    async def scenario():
        async with ledger_fixture() as (connection, database, repo):
            owner = uuid4()
            operation_id = OperationId(uuid4())
            await repo.create_portfolio(owner)
            locker_pid = connection.get_server_pid()
            task = None
            try:
                async with database.connection() as observer:
                    async with connection.transaction():
                        await connection.fetchrow(
                            "SELECT id FROM financial_portfolios WHERE id=$1 FOR UPDATE", owner
                        )
                        task = asyncio.create_task(
                            repo.append(owner, operation_id, at(), opening())
                        )
                        deadline = asyncio.get_running_loop().time() + 5
                        while True:
                            blocked = await observer.fetchval(
                                """SELECT count(*) FROM pg_stat_activity
                                WHERE datname=current_database() AND pid <> pg_backend_pid()
                                  AND query LIKE '%financial_portfolios%FOR UPDATE%'
                                  AND $1::INTEGER = ANY(pg_blocking_pids(pid))""",
                                locker_pid,
                            )
                            if blocked == 1:
                                break
                            assert asyncio.get_running_loop().time() < deadline, (
                                "Append did not wait on the intended session"
                            )
                            await asyncio.sleep(0.01)
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 5)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            assert await counts(connection, owner) == (0, 0, 0)
            accepted = await repo.append(owner, operation_id, at(), opening())
            assert accepted.sequence.value == 1
            assert await counts(connection, owner) == (1, 1, 1)

    asyncio.run(scenario())


def test_terminated_connection_before_sequence_update_rolls_back():
    class TerminatedConnection(InjectedConnection):
        def __init__(self, connection, observer):
            super().__init__(connection, "UPDATE financial_portfolios", 1)
            self.observer = observer

        async def _maybe_fail(self, statement):
            if statement.lstrip().startswith(self.statement_prefix):
                await self.observer.fetchval(
                    "SELECT pg_terminate_backend($1)", self.connection.get_server_pid()
                )

    class TerminatedDatabase:
        def __init__(self, database, observer):
            self.database = database
            self.observer = observer

        async def validate_financial_schema(self, connection):
            await self.database.validate_financial_schema(connection.connection)

        @asynccontextmanager
        async def transaction(self):
            async with self.database.transaction() as connection:
                yield TerminatedConnection(connection, self.observer)

    async def scenario():
        async with ledger_fixture() as (connection, database, repo):
            owner = uuid4()
            operation_id = OperationId(uuid4())
            await repo.create_portfolio(owner)
            with pytest.raises(PersistenceError):
                await PostgresFinancialLedgerRepository(
                    TerminatedDatabase(database, connection)
                ).append(owner, operation_id, at(), opening())
            assert await counts(connection, owner) == (0, 0, 0)
            accepted = await repo.append(owner, operation_id, at(), opening())
            assert accepted.sequence.value == 1
            assert await counts(connection, owner) == (1, 1, 1)

    asyncio.run(scenario())


def test_reader_sees_only_committed_operation_and_postings():
    async def scenario():
        async with ledger_fixture() as (connection, database, repo):
            owner = uuid4()
            await repo.create_portfolio(owner)
            original = await append(repo, owner, 0, opening())
            reached, release = asyncio.Event(), asyncio.Event()
            injected = PostgresFinancialLedgerRepository(
                InjectedDatabase(database, "INSERT INTO financial_postings", 2, reached, release)
            )
            task = asyncio.create_task(append(injected, owner, 1, buy("1", "5")))
            try:
                await asyncio.wait_for(reached.wait(), 5)
                before, state = await asyncio.wait_for(persisted_state(repo, owner), 5)
                assert before == (original,)
                assert_asset(state, "USDT", "100", "100", average="1")
                assert await counts(connection, owner) == (1, 1, 1)
                release.set()
                accepted = await asyncio.wait_for(task, 5)
                after, state = await persisted_state(repo, owner)
                assert after == (original, accepted)
                assert await counts(connection, owner) == (2, 3, 2)
                assert_asset(state, "BTC", "1", "5", average="5")
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_cancellation_during_blocked_commit_reconciles_by_stable_uuid():
    async def scenario():
        async with ledger_fixture() as (connection, database, repo):
            owner = uuid4()
            operation_id = OperationId(uuid4())
            lock_key = uuid4().int & ((1 << 63) - 1)
            await repo.create_portfolio(owner)
            await connection.execute(
                f"""CREATE FUNCTION test_wait_at_commit() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                    PERFORM pg_advisory_xact_lock({lock_key});
                    RETURN NEW;
                END $$"""
            )
            await connection.execute(
                """CREATE CONSTRAINT TRIGGER test_wait_at_commit
                AFTER UPDATE ON financial_portfolios DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION test_wait_at_commit()"""
            )
            task = None
            try:
                async with database.connection() as observer:
                    async with connection.transaction():
                        await connection.execute("SELECT pg_advisory_xact_lock($1)", lock_key)
                        locker_pid = connection.get_server_pid()
                        task = asyncio.create_task(
                            repo.append(owner, operation_id, at(), opening())
                        )
                        deadline = asyncio.get_running_loop().time() + 5
                        while True:
                            blocked = await observer.fetchval(
                                """SELECT count(*) FROM pg_stat_activity
                                WHERE datname=current_database() AND pid <> pg_backend_pid()
                                  AND wait_event_type='Lock'
                                  AND $1::INTEGER = ANY(pg_blocking_pids(pid))""",
                                locker_pid,
                            )
                            if blocked == 1:
                                break
                            assert asyncio.get_running_loop().time() < deadline, (
                                "Append did not reach the test-only COMMIT gate"
                            )
                            await asyncio.sleep(0.01)
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 5)
            finally:
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                await connection.execute("DROP TRIGGER test_wait_at_commit ON financial_portfolios")
                await connection.execute("DROP FUNCTION test_wait_at_commit()")
            # Cancellation after COMMIT was sent is an unknown outcome until read back.
            before = await repo.load_operations(owner)
            assert len(before) in (0, 1)
            if before:
                assert before[0].id == operation_id
                assert await counts(connection, owner) == (1, 1, 1)
            else:
                assert await counts(connection, owner) == (0, 0, 0)
            retried = await repo.append(owner, operation_id, at(), opening())
            assert retried.sequence.value == 1
            assert await counts(connection, owner) == (1, 1, 1)
            assert (await repo.load_operations(owner)) == (retried,)

    asyncio.run(scenario())


def test_bounded_mixed_contention_preserves_each_portfolio_history():
    async def scenario():
        async with ledger_fixture() as (connection, _, repo):
            owners = [uuid4() for _ in range(3)]
            shared = {}
            for owner in owners:
                await repo.create_portfolio(owner)
                await append(repo, owner, 0, opening())
                shared[owner] = OperationId(uuid4())
                await append(repo, owner, 1, buy("1", "10"), operation_id=shared[owner])

            requests = []
            for owner in owners:
                requests.extend((owner, OperationId(uuid4()), "10", "valid") for _ in range(4))
                requests.extend((owner, OperationId(uuid4()), "200", "invalid") for _ in range(2))
                requests.extend((owner, shared[owner], "10", "retry") for _ in range(3))
                requests.append((owner, shared[owner], "11", "conflict"))

            async def submit(item):
                owner, operation_id, principal, _ = item
                return await repo.append(owner, operation_id, at(1), buy("1", principal))

            results = await start_together(requests, submit)
            assert len(results) == 30
            for request, result in zip(requests, results, strict=True):
                expected = request[3]
                if expected == "invalid":
                    assert isinstance(result, StateConflictError)
                elif expected == "conflict":
                    assert isinstance(result, ConflictError)
                else:
                    assert not isinstance(result, BaseException)
            for owner in owners:
                assert await counts(connection, owner) == (6, 11, 6)
                operations, state = await persisted_state(repo, owner)
                assert sorted(op.sequence.value for op in operations) == list(range(1, 7))
                assert len({op.id for op in operations}) == 6
                assert len(state.ordered_operation_ids) == 6
                assert_asset(state, "USDT", "50", "50", average="1")
                assert_asset(state, "BTC", "5", "50", average="10")
                assert (
                    await connection.fetchval(
                        """SELECT count(*) FROM financial_postings posting
                    LEFT JOIN financial_operations operation
                    ON (posting.portfolio_id, posting.operation_id) =
                       (operation.portfolio_id, operation.operation_id)
                    WHERE posting.portfolio_id=$1 AND operation.operation_id IS NULL""",
                        owner,
                    )
                    == 0
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("existing_buys", [0, 10, 30])
def test_append_performance_characterization(existing_buys):
    class TimedConnection:
        def __init__(self, connection, timer):
            self.connection = connection
            self.timer = timer

        async def fetchrow(self, statement, *args):
            result = await self.connection.fetchrow(statement, *args)
            if "FROM financial_portfolios" in statement and "FOR UPDATE" in statement:
                self.timer.lock_acquired = perf_counter()
            return result

        async def fetch(self, statement, *args):
            return await self.connection.fetch(statement, *args)

        async def execute(self, statement, *args):
            return await self.connection.execute(statement, *args)

    class TimedDatabase:
        def __init__(self, database):
            self.database = database
            self.lock_acquired = None
            self.lock_released = None

        async def validate_financial_schema(self, connection):
            await self.database.validate_financial_schema(connection.connection)

        @asynccontextmanager
        async def transaction(self):
            async with self.database.connection() as connection:
                async with connection.transaction():
                    yield TimedConnection(connection, self)
                self.lock_released = perf_counter()

    async def scenario():
        async with ledger_fixture() as (_, database, repo):
            owner = uuid4()
            await repo.create_portfolio(owner)
            await append(repo, owner, 0, opening("USDT", "1000", "1000"))
            for index in range(existing_buys):
                await append(repo, owner, index + 1, buy("1", "1"))
            operation_id = OperationId(uuid4())
            timed = TimedDatabase(database)
            started = perf_counter()
            created = await PostgresFinancialLedgerRepository(timed).append(
                owner, operation_id, at(existing_buys + 1), buy("1", "1")
            )
            append_ms = (perf_counter() - started) * 1000
            assert timed.lock_acquired is not None and timed.lock_released is not None
            lock_ms = (timed.lock_released - timed.lock_acquired) * 1000
            started = perf_counter()
            retried = await repo.append(owner, operation_id, at(existing_buys + 1), buy("1", "1"))
            retry_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            history = await repo.load_operations(owner)
            load_ms = (perf_counter() - started) * 1000
            started = perf_counter()
            replay(history)
            replay_ms = (perf_counter() - started) * 1000
            assert created == retried
            assert len(history) == existing_buys + 2
            print(
                f"PERF existing_buys={existing_buys} append_ms={append_ms:.3f} "
                f"retry_ms={retry_ms:.3f} load_ms={load_ms:.3f} "
                f"replay_ms={replay_ms:.3f} lock_ms={lock_ms:.3f}"
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,steps,expected_assets,expected_pnl",
    [
        ("opening", [(0, opening("USDT", "100", "100"))], [("USDT", "100", "100", "0", "1")], "0"),
        (
            "buy",
            [(0, opening()), (1, buy("2", "30"))],
            [("BTC", "2", "30", "0", "15"), ("USDT", "70", "70", "0", "1")],
            "0",
        ),
        (
            "sell",
            [(0, opening()), (1, buy("2", "30")), (2, sell("2", "40"))],
            [("BTC", "0", "0", "0", None), ("USDT", "110", "110", "0", "1")],
            "10",
        ),
        (
            "weighted_average",
            [(0, opening()), (1, buy("2", "20")), (2, buy("2", "40"))],
            [("BTC", "4", "60", "0", "15"), ("USDT", "40", "40", "0", "1")],
            "0",
        ),
        (
            "partial_sale",
            [(0, opening()), (1, buy("2", "20")), (2, buy("2", "40")), (3, sell("1", "25"))],
            [("BTC", "3", "45", "0", "15"), ("USDT", "65", "65", "0", "1")],
            "10",
        ),
        (
            "unknown_cost",
            [(0, opening("BTC", "2", None, incomplete="2")), (1, sell("1", "8"))],
            [("BTC", "1", "0", "1", None), ("USDT", "8", "8", "0", "1")],
            None,
        ),
        (
            "known_zero",
            [(0, opening("BTC", "2", "0")), (1, sell("1", "8"))],
            [("BTC", "1", "0", "0", "0"), ("USDT", "8", "8", "0", "1")],
            "8",
        ),
        (
            "mixed_cost",
            [
                (0, opening("BTC", "2", None, incomplete="2")),
                (1, opening("BTC", "2", "20")),
                (2, sell("2", "30")),
            ],
            [("BTC", "2", "10", "1", None), ("USDT", "30", "30", "0", "1")],
            None,
        ),
        (
            "backdated_buy",
            [(0, opening()), (2, buy("1", "40")), (1, buy("1", "20"))],
            [("BTC", "2", "60", "0", "30"), ("USDT", "40", "40", "0", "1")],
            "0",
        ),
        (
            "backdated_sell",
            [(0, opening("BTC", "2", "20")), (2, sell("1", "15")), (1, opening("BTC", "2", "40"))],
            [("BTC", "3", "45", "0", "15"), ("USDT", "15", "15", "0", "1")],
            "0",
        ),
    ],
)
def test_persisted_accounting_replays_exactly(name, steps, expected_assets, expected_pnl):
    async def scenario():
        async with ledger_fixture() as (connection, _, repo):
            owner = uuid4()
            await repo.create_portfolio(owner)
            for day, facts in steps:
                await append(repo, owner, day, facts)
            operations, state = await persisted_state(repo, owner)
            assert len(operations) == len(steps), name
            assert sorted(op.sequence.value for op in operations) == list(range(1, len(steps) + 1))
            assert await counts(connection, owner) == (
                len(steps),
                sum(len(op.postings) for op in operations),
                len(steps),
            )
            for asset in expected_assets:
                assert_asset(state, *asset)
            assert state.total_realized_pnl_usdt == (
                Decimal(expected_pnl) if expected_pnl is not None else None
            )
            if name == "mixed_cost":
                assert state.sales[0].completeness is Completeness.PARTIAL
                assert state.sales[0].known_cost_removed_usdt == Decimal("10")
                assert state.sales[0].incomplete_quantity_removed == Decimal("1")
            if name == "unknown_cost":
                assert state.sales[0].completeness is Completeness.UNKNOWN
            if name == "backdated_sell":
                assert [op.sequence.value for op in operations] == [1, 3, 2]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "steps,rejected_day,rejected_facts",
    [
        ([(1, opening())], 0, buy("1", "20")),
        ([(1, opening("BTC", "1", "10")), (3, sell("1", "20"))], 2, sell("1", "20")),
    ],
)
def test_backdated_negative_historical_balance_cannot_persist(steps, rejected_day, rejected_facts):
    async def scenario():
        async with ledger_fixture() as (connection, _, repo):
            owner = uuid4()
            await repo.create_portfolio(owner)
            for day, facts in steps:
                await append(repo, owner, day, facts)
            before = await counts(connection, owner)
            with pytest.raises(StateConflictError):
                await append(repo, owner, rejected_day, rejected_facts)
            assert await counts(connection, owner) == before
            operations, state = await persisted_state(repo, owner)
            assert len(operations) == before[0]
            assert all(position.quantity >= 0 for position in state.assets)

    asyncio.run(scenario())
