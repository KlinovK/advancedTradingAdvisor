import asyncio
import json
import socket
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.domain.persistence import (
    ConfirmedUpdate,
    ConflictError,
    FailureCategory,
    PayloadError,
    PersistenceError,
    RevisionConflictError,
    SchemaVersionError,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import (
    DEFAULT_MANUAL_ACCOUNT_ID,
    DEFAULT_MANUAL_ACCOUNT_NAME,
    Account,
    AccountType,
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    FinancialSettings,
    LimitOrder,
    LimitOrderStatus,
    Portfolio,
    TradingSide,
)
from advanced_trading_advisor.storage.codecs import (
    AnalysisRunCodec,
    SnapshotCodec,
    UpdateCodec,
    utc_text,
    validate_result,
)
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 9, 12, 30, 0, 123456, tzinfo=UTC)


def snapshot():
    orders = tuple(
        LimitOrder(
            uuid4(),
            "BTC",
            TradingSide.BUY if i % 2 else TradingSide.SELL,
            Decimal("65432.123456789012345678901234567890"),
            Decimal("0.0100"),
            status,
            NOW - timedelta(days=2),
            NOW - timedelta(days=1),
            None if status is LimitOrderStatus.OPEN else NOW - timedelta(days=1),
            account_id=DEFAULT_MANUAL_ACCOUNT_ID,
        )
        for i, status in enumerate(LimitOrderStatus)
    )
    return ConfirmedPortfolioSnapshot(
        uuid4(),
        NOW.astimezone(timezone(timedelta(hours=3))),
        Portfolio(
            (
                Account(
                    DEFAULT_MANUAL_ACCOUNT_ID,
                    DEFAULT_MANUAL_ACCOUNT_NAME,
                    AccountType.MANUAL,
                    (
                        AssetPosition("BTC", Decimal("0.000000000000000000000000000000123456789")),
                        AssetPosition("USDC", Decimal("123456789012345678901234567890.00100")),
                    ),
                ),
            )
        ),
        FinancialSettings(Decimal("1000.0100"), Decimal("6.25")),
        orders,
    )


async def setup(path):
    db = Database(path)
    await db.initialize()
    snapshots = SQLiteSnapshotRepository(db)
    return db, snapshots, ConfirmedStateService(snapshots), SQLiteAnalysisRunRepository(db)


def test_offline_guard():
    with pytest.raises(AssertionError, match="Network access"):
        socket.create_connection(("example.com", 443))


def test_fresh_schema_and_explicit_parent(tmp_path):
    async def scenario():
        path = tmp_path / "nested" / "advisor.sqlite"
        db = Database(path)
        with pytest.raises(PersistenceError, match="parent"):
            await db.initialize()
        assert not path.parent.exists()
        with pytest.raises(PersistenceError, match="Initialize"):
            async with db.connection():
                pass
        await db.initialize(create_parent=True)
        await db.initialize()
        async with db.connection() as connection:
            assert (await (await connection.execute("PRAGMA foreign_keys")).fetchone())[0] == 1
            assert (await (await connection.execute("PRAGMA busy_timeout")).fetchone())[0] == 5000
            rows = await (await connection.execute("SELECT * FROM schema_metadata")).fetchall()
            assert [tuple(row) for row in rows] == [(1, 3)]
            types = await (
                await connection.execute(
                    "SELECT type FROM pragma_table_info(?)", ("portfolio_snapshots",)
                )
            ).fetchall()
            assert all(row[0] != "REAL" for row in types)
        assert await SQLiteSnapshotRepository(db).get_current() is None

    asyncio.run(scenario())


@pytest.mark.parametrize("version", [0, 2, 999])
def test_unsupported_schema_is_not_rewritten(tmp_path, version):
    path = tmp_path / "advisor.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_metadata(singleton_id INTEGER, version INTEGER)")
        connection.execute("INSERT INTO schema_metadata VALUES (?, ?)", (1, version))
    with pytest.raises(SchemaVersionError):
        asyncio.run(Database(path).initialize())
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version FROM schema_metadata").fetchone()[0] == version


def test_unversioned_schema_fails(tmp_path):
    path = tmp_path / "advisor.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE legacy(value TEXT)")
    with pytest.raises(SchemaVersionError, match="Unversioned"):
        asyncio.run(Database(path).initialize())


def test_incomplete_schema_fails(tmp_path):
    path = tmp_path / "advisor.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_metadata(singleton_id INTEGER, version INTEGER)")
        connection.execute("INSERT INTO schema_metadata VALUES (?, ?)", (1, 1))
    with pytest.raises(SchemaVersionError, match="tables"):
        asyncio.run(Database(path).initialize())


def test_snapshot_codec_exact_round_trip():
    original = snapshot()
    encoded = SnapshotCodec.encode(original)
    restored = SnapshotCodec.decode(encoded)
    assert restored == original
    assert SnapshotCodec.encode(restored) == encoded
    assert restored.confirmed_at.tzinfo is UTC
    assert (
        restored.portfolio.positions[1].amount.as_tuple()
        == original.portfolio.positions[1].amount.as_tuple()
    )
    payload = json.loads(encoded)
    assert payload["data"]["confirmed_at"] == utc_text(NOW)
    assert payload["data"]["portfolio"]["accounts"][0]["positions"][1]["amount"] == (
        "123456789012345678901234567890.00100"
    )
    assert {o.status for o in restored.orders} == set(LimitOrderStatus)
    assert {o.side for o in restored.orders} == set(TradingSide)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=5),
        lambda p: p.update(schema_version=True),
        lambda p: p.update(kind="other"),
        lambda p: p["data"].update(id="not-a-uuid"),
        lambda p: p["data"].update(confirmed_at="2026-09-09T12:30:00"),
        lambda p: p["data"].update(portfolio=None),
        lambda p: p["data"]["portfolio"]["accounts"][0]["positions"][0].update(amount=0.01),
        lambda p: p["data"]["portfolio"]["accounts"][0]["positions"][0].update(amount="NaN"),
        lambda p: p["data"]["portfolio"]["accounts"][0]["positions"][0].update(amount="-1"),
        lambda p: p["data"]["portfolio"]["accounts"][0]["positions"][0].update(amount="0"),
        lambda p: p["data"]["orders"][0].update(side="short"),
        lambda p: p["data"]["orders"][0].update(status="unknown"),
        lambda p: p["data"].update(unexpected=True),
    ],
)
def test_malformed_snapshot_rejected(mutation):
    payload = json.loads(SnapshotCodec.encode(snapshot()))
    mutation(payload)
    with pytest.raises(PayloadError):
        SnapshotCodec.decode(json.dumps(payload))


@pytest.mark.parametrize("text", ["", "null", "[]", "{}", '{"a":1,"a":2}', '{"x":NaN}'])
def test_bad_json_rejected(text):
    with pytest.raises(PayloadError):
        SnapshotCodec.decode(text)


@pytest.mark.parametrize("status", list(AnalysisRunStatus))
def test_run_codec_round_trip(status):
    run = AnalysisRun(
        uuid4(),
        uuid4(),
        NOW,
        status,
        None if status is AnalysisRunStatus.RUNNING else NOW + timedelta(seconds=1),
        NOW - timedelta(hours=4),
    )
    assert AnalysisRunCodec.decode(AnalysisRunCodec.encode(run)) == run


@pytest.mark.parametrize("update_type", list(UpdateType))
def test_update_codec_round_trip(update_type):
    update = ConfirmedUpdate(
        uuid4(),
        update_type,
        None if update_type is UpdateType.INITIAL_STATE else uuid4(),
        uuid4(),
        NOW,
    )
    assert UpdateCodec.decode(UpdateCodec.encode(update)) == update


def test_snapshot_repository_immutability(tmp_path):
    async def scenario():
        db, repo, _, _ = await setup(tmp_path / "advisor.sqlite")
        original = snapshot()
        assert await repo.get(uuid4()) is None
        await repo.save(original)
        await repo.save(original)
        assert await repo.get(original.id) == original
        assert await repo.list_recent() == (original,)
        with pytest.raises(ConflictError):
            await repo.save(
                replace(original, financial_settings=FinancialSettings(Decimal("2"), Decimal("3")))
            )
        for sql in (
            "UPDATE portfolio_snapshots SET payload_json = ? WHERE snapshot_id = ?",
            "DELETE FROM portfolio_snapshots WHERE snapshot_id = ?",
        ):
            async with db.transaction() as connection:
                with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                    await connection.execute(
                        sql,
                        ("{}", str(original.id))
                        if sql.startswith("UPDATE")
                        else (str(original.id),),
                    )
        assert await repo.get(original.id) == original
        assert await repo.get_current() is None

    asyncio.run(scenario())


def test_confirm_reopen_revision_and_audit(tmp_path):
    async def scenario():
        path = tmp_path / "advisor.sqlite"
        db, repo, service, _ = await setup(path)
        first = snapshot()
        initial = await service.initialize(first)
        assert initial.revision == 1
        second = replace(first, id=uuid4(), confirmed_at=NOW + timedelta(days=1))
        updated = await service.confirm(
            second,
            expected_revision=1,
            previous_snapshot_id=first.id,
            update_type=UpdateType.BALANCE_CORRECTION,
        )
        assert updated.revision == 2
        reopened = Database(path)
        await reopened.initialize()
        assert (
            await ConfirmedStateService(SQLiteSnapshotRepository(reopened)).load_current()
            == updated
        )
        audit = await SQLiteConfirmedUpdateRepository(db).list_recent()
        assert len(audit) == 2
        assert audit[0].previous_snapshot_id == first.id
        assert audit[0].new_snapshot_id == second.id
        assert audit[0].update_type is UpdateType.BALANCE_CORRECTION
        assert audit[1].update_type is UpdateType.INITIAL_STATE
        assert await repo.list_recent(1) == (second,)
        assert await repo.get(first.id) == first
        with pytest.raises(RevisionConflictError):
            await service.initialize(snapshot())
        stale = replace(second, id=uuid4())
        with pytest.raises(RevisionConflictError):
            await service.confirm(
                stale,
                expected_revision=1,
                previous_snapshot_id=first.id,
                update_type=UpdateType.DEPOSIT,
            )
        assert await repo.get(stale.id) is None
        assert await service.load_current() == updated

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["audit", "pointer"])
def test_transition_rolls_back_snapshot_audit_and_pointer(tmp_path, failure):
    async def scenario():
        db, repo, service, _ = await setup(tmp_path / "advisor.sqlite")
        first = snapshot()
        update_id = uuid4()
        initial = await service.initialize(first, update_id=update_id)
        second = replace(first, id=uuid4(), confirmed_at=NOW + timedelta(seconds=1))
        if failure == "pointer":
            async with db.transaction() as connection:
                await connection.execute("""CREATE TRIGGER fail_pointer
                    BEFORE UPDATE ON current_portfolio
                    BEGIN SELECT RAISE(ABORT, 'injected failure'); END""")
        with pytest.raises((ConflictError, sqlite3.IntegrityError)):
            await service.confirm(
                second,
                expected_revision=1,
                previous_snapshot_id=first.id,
                update_type=UpdateType.DEPOSIT,
                update_id=update_id if failure == "audit" else uuid4(),
            )
        assert await repo.get(second.id) is None
        assert await service.load_current() == initial
        assert len(await SQLiteConfirmedUpdateRepository(db).list_recent()) == 1

    asyncio.run(scenario())


def test_competing_confirmations_only_one_commits(tmp_path):
    async def scenario():
        db, repo, service, _ = await setup(tmp_path / "advisor.sqlite")
        first = snapshot()
        await service.initialize(first)
        candidates = [replace(first, id=uuid4()) for _ in range(2)]
        outcomes = await asyncio.gather(
            *(
                ConfirmedStateService(SQLiteSnapshotRepository(db)).confirm(
                    value,
                    expected_revision=1,
                    previous_snapshot_id=first.id,
                    update_type=UpdateType.BALANCE_CORRECTION,
                )
                for value in candidates
            ),
            return_exceptions=True,
        )
        assert sum(isinstance(value, RevisionConflictError) for value in outcomes) == 1
        assert (await repo.get_current()).revision == 2
        assert len(await repo.list_recent()) == 2
        assert len(await SQLiteConfirmedUpdateRepository(db).list_recent()) == 2

    asyncio.run(scenario())


def test_analysis_lifecycle_independent_of_current_state(tmp_path):
    async def scenario():
        db, repo, service, runs = await setup(tmp_path / "advisor.sqlite")
        state = await service.initialize(snapshot())
        first = AnalysisRun.start(state.snapshot.id, NOW)
        second = AnalysisRun.start(state.snapshot.id, NOW + timedelta(seconds=1))
        await runs.create(first)
        await runs.create(second)
        assert (await runs.get(first.run_id)).run == first
        result = '{"schema_version":1,"data":{"amount_usd":"1.2300","actions":[]}}'
        completed = await runs.mark_completed(
            first.run_id, completed_at=NOW + timedelta(seconds=2), result_json=result
        )
        failed = await runs.mark_failed(
            second.run_id,
            completed_at=NOW + timedelta(seconds=3),
            failure_category=FailureCategory.TIMEOUT,
        )
        assert completed.run.status is AnalysisRunStatus.COMPLETED
        assert completed.result_json == validate_result(result)
        assert failed.run.status is AnalysisRunStatus.FAILED
        assert failed.result_json is None
        assert failed.failure_category is FailureCategory.TIMEOUT
        reopened = SQLiteAnalysisRunRepository(Database(db.path))
        assert await reopened.list_recent(state.snapshot.id) == (failed, completed)
        assert await reopened.list_recent(state.snapshot.id, 1) == (failed,)
        assert await repo.get_current() == state
        assert await repo.get(state.snapshot.id) == state.snapshot
        assert len(await SQLiteConfirmedUpdateRepository(db).list_recent()) == 1
        for run in (first, second):
            with pytest.raises(ConflictError):
                await runs.mark_completed(run.run_id, completed_at=NOW + timedelta(seconds=4))
            with pytest.raises(ConflictError):
                await runs.mark_failed(
                    run.run_id,
                    completed_at=NOW + timedelta(seconds=4),
                    failure_category=FailureCategory.ANALYSIS_FAILED,
                )
        assert await runs.get(uuid4()) is None

    asyncio.run(scenario())


def test_invalid_run_operations_leave_running_unchanged(tmp_path):
    async def scenario():
        _, _, service, runs = await setup(tmp_path / "advisor.sqlite")
        state = await service.initialize(snapshot())
        run = AnalysisRun.start(state.snapshot.id, NOW)
        await runs.create(run)
        with pytest.raises(ConflictError):
            await runs.create(run)
        with pytest.raises(PersistenceError):
            await runs.create(AnalysisRun.start(uuid4(), NOW))
        with pytest.raises(ConflictError):
            await runs.create(AnalysisRun.start(state.snapshot.id, NOW - timedelta(seconds=1)))
        with pytest.raises(DomainValidationError):
            await runs.create(
                replace(run, run_id=uuid4(), status=AnalysisRunStatus.COMPLETED, completed_at=NOW)
            )
        with pytest.raises(DomainValidationError):
            await runs.mark_completed(run.run_id, completed_at=NOW - timedelta(seconds=1))
        with pytest.raises(DomainValidationError):
            await runs.mark_completed(run.run_id, completed_at=NOW.replace(tzinfo=None))
        with pytest.raises(DomainValidationError):
            await runs.mark_failed(
                run.run_id, completed_at=NOW, failure_category="raw provider error"
            )
        with pytest.raises(PayloadError):
            await runs.mark_completed(
                run.run_id, completed_at=NOW, result_json='{"schema_version":2,"data":{}}'
            )
        assert (await runs.get(run.run_id)).run == run
        with pytest.raises(PersistenceError):
            await runs.mark_completed(uuid4(), completed_at=NOW)

    asyncio.run(scenario())


def test_foreign_keys_enforced(tmp_path):
    async def scenario():
        db, _, _, _ = await setup(tmp_path / "advisor.sqlite")
        async with db.transaction() as connection:
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                await connection.execute(
                    "INSERT INTO current_portfolio VALUES (?, ?, ?, ?)",
                    (1, str(uuid4()), 1, utc_text(NOW)),
                )
            with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
                await connection.execute(
                    "INSERT INTO analysis_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid4()),
                        str(uuid4()),
                        "running",
                        utc_text(NOW),
                        None,
                        None,
                        None,
                        None,
                        1,
                    ),
                )

    asyncio.run(scenario())


@pytest.mark.parametrize("version,payload", [(2, None), (1, "{}"), (1, "not json")])
def test_malformed_stored_snapshot_fails_loudly(tmp_path, version, payload):
    async def scenario():
        db, repo, _, _ = await setup(tmp_path / "advisor.sqlite")
        original = snapshot()
        async with db.transaction() as connection:
            await connection.execute(
                "INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?, ?)",
                (
                    str(original.id),
                    utc_text(NOW),
                    payload or SnapshotCodec.encode(original),
                    version,
                    utc_text(NOW),
                ),
            )
        with pytest.raises(PayloadError):
            await repo.get(original.id)

    asyncio.run(scenario())


def test_process_restart_reads_real_database(tmp_path):
    path = tmp_path / "advisor.sqlite"

    async def scenario():
        _, _, service, runs = await setup(path)
        state = await service.initialize(snapshot())
        await runs.create(AnalysisRun.start(state.snapshot.id, NOW))
        return state

    state = asyncio.run(scenario())
    script = """
import asyncio, sys, socket
from pathlib import Path
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteSnapshotRepository, SQLiteAnalysisRunRepository,
)
def blocked(*args, **kwargs):
    raise AssertionError('Network prohibited')
socket.socket.connect = blocked
socket.getaddrinfo = blocked
async def main():
    db = Database(Path(sys.argv[1]))
    await db.initialize()
    state = await SQLiteSnapshotRepository(db).get_current()
    assert state.revision == 1
    assert str(state.snapshot.id) == sys.argv[2]
    assert len(await SQLiteAnalysisRunRepository(db).list_recent(state.snapshot.id)) == 1
    print('restored')
asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(path), str(state.snapshot.id)],
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    assert result.stdout.strip() == "restored"


def test_malformed_schema_metadata(tmp_path):
    path = tmp_path / "advisor.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE schema_metadata(broken TEXT)")
    with pytest.raises(SchemaVersionError, match="Malformed"):
        asyncio.run(Database(path).initialize())


def test_cancelled_transaction_rolls_back(tmp_path):
    async def scenario():
        db, repo, _, _ = await setup(tmp_path / "advisor.sqlite")
        original = snapshot()
        with pytest.raises(asyncio.CancelledError):
            async with db.transaction() as connection:
                await connection.execute(
                    "INSERT INTO portfolio_snapshots VALUES (?, ?, ?, ?, ?)",
                    (
                        str(original.id),
                        utc_text(NOW),
                        SnapshotCodec.encode(original),
                        1,
                        utc_text(NOW),
                    ),
                )
                raise asyncio.CancelledError
        assert await repo.get(original.id) is None

    asyncio.run(scenario())


def test_confirmation_rejects_wrong_previous_and_historical_ids(tmp_path):
    async def scenario():
        _, repo, service, _ = await setup(tmp_path / "advisor.sqlite")
        first = snapshot()
        await service.initialize(first)
        next_snapshot = replace(first, id=uuid4())
        with pytest.raises(ConflictError):
            await service.confirm(
                next_snapshot,
                expected_revision=1,
                previous_snapshot_id=uuid4(),
                update_type=UpdateType.DEPOSIT,
            )
        assert await repo.get(next_snapshot.id) is None
        updated = await service.confirm(
            next_snapshot,
            expected_revision=1,
            previous_snapshot_id=first.id,
            update_type=UpdateType.DEPOSIT,
        )
        with pytest.raises(ConflictError):
            await service.confirm(
                first,
                expected_revision=2,
                previous_snapshot_id=next_snapshot.id,
                update_type=UpdateType.WITHDRAWAL,
            )
        assert await repo.get_current() == updated
        earlier = replace(first, id=uuid4(), confirmed_at=NOW - timedelta(hours=1))
        with pytest.raises(ConflictError):
            await service.confirm(
                earlier,
                expected_revision=2,
                previous_snapshot_id=next_snapshot.id,
                update_type=UpdateType.WITHDRAWAL,
            )
        assert await repo.get(earlier.id) is None

    asyncio.run(scenario())


def test_standalone_audit_is_append_only_and_does_not_change_state(tmp_path):
    async def scenario():
        db, repo, _, _ = await setup(tmp_path / "advisor.sqlite")
        value = snapshot()
        await repo.save(value)
        update = ConfirmedUpdate(
            uuid4(), UpdateType.INITIAL_STATE, None, value.id, value.confirmed_at
        )
        audit = SQLiteConfirmedUpdateRepository(db)
        await audit.append(update)
        assert await audit.list_recent() == (update,)
        assert await repo.get_current() is None
        with pytest.raises(ConflictError):
            await audit.append(update)
        async with db.transaction() as connection:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                await connection.execute(
                    "DELETE FROM confirmed_updates WHERE update_id = ?", (str(update.update_id),)
                )
        with pytest.raises(PersistenceError):
            await audit.append(replace(update, update_id=uuid4(), new_snapshot_id=uuid4()))

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"data":{"amount":1.5}}',
        '{"schema_version":1,"data":{"amount":NaN}}',
        '{"schema_version":1,"data":[]}',
        '{"schema_version":true,"data":{}}',
        '{"schema_version":2,"data":{}}',
    ],
)
def test_invalid_result_payload(payload):
    with pytest.raises(PayloadError):
        validate_result(payload)


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"failure_category": "raw provider exception"},
        {"completed_at": None},
        {"result_json": '{"schema_version":1,"data":{}}'},
    ],
)
def test_malformed_analysis_record(change):
    record = {
        "run_id": str(uuid4()),
        "snapshot_id": str(uuid4()),
        "status": "failed",
        "started_at": utc_text(NOW),
        "completed_at": utc_text(NOW),
        "market_cutoff": None,
        "result_json": None,
        "failure_category": "timeout",
        "schema_version": 1,
    }
    record.update(change)
    with pytest.raises(PayloadError):
        AnalysisRunCodec.from_record(record)


@pytest.mark.parametrize(
    "codec,kind", [(AnalysisRunCodec, "analysis_run"), (UpdateCodec, "confirmed_update")]
)
def test_other_codecs_reject_future_version(codec, kind):
    with pytest.raises(PayloadError):
        codec.decode(json.dumps({"schema_version": 2, "kind": kind, "data": {}}))
