from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import aiosqlite

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.analysis.recovery import LEASE_DURATION, lease_expires
from advanced_trading_advisor.domain.persistence import (
    ConfirmedUpdate,
    ConflictError,
    CurrentPortfolio,
    FailureCategory,
    PayloadError,
    PersistenceError,
    RevisionConflictError,
    StoredAnalysisRun,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import identity, require, timestamp
from advanced_trading_advisor.portfolio.models import ConfirmedPortfolioSnapshot

from .codecs import (
    PAYLOAD_VERSION,
    AnalysisRunCodec,
    SnapshotCodec,
    UpdateCodec,
    utc_text,
    validate_result,
)
from .database import Database


def limit_value(limit: int) -> None:
    require(type(limit) is int and limit > 0, "Limit must be positive.")


async def one(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchone()


async def many(connection, sql, parameters=()):
    async with connection.execute(sql, parameters) as cursor:
        return await cursor.fetchall()


def snapshot_row(row) -> ConfirmedPortfolioSnapshot:
    if row["schema_version"] != PAYLOAD_VERSION:
        raise PayloadError("Unsupported snapshot row version.")
    result = SnapshotCodec.decode(row["payload_json"])
    if str(result.id) != row["snapshot_id"] or utc_text(result.confirmed_at) != row["confirmed_at"]:
        raise PayloadError("Snapshot columns disagree with payload.")
    return result


def update_row(row) -> ConfirmedUpdate:
    if row["schema_version"] != PAYLOAD_VERSION:
        raise PayloadError("Unsupported update row version.")
    result = UpdateCodec.decode(row["payload_json"])
    expected = (
        str(result.update_id),
        result.update_type.value,
        str(result.previous_snapshot_id) if result.previous_snapshot_id else None,
        str(result.new_snapshot_id),
        utc_text(result.confirmed_at),
    )
    actual = tuple(
        row[k]
        for k in (
            "update_id",
            "update_type",
            "previous_snapshot_id",
            "new_snapshot_id",
            "confirmed_at",
        )
    )
    if actual != expected:
        raise PayloadError("Update columns disagree with payload.")
    return result


async def save_snapshot(connection, snapshot: ConfirmedPortfolioSnapshot) -> None:
    payload = SnapshotCodec.encode(snapshot)
    SnapshotCodec.decode(payload)
    existing = await one(
        connection, "SELECT * FROM portfolio_snapshots WHERE snapshot_id = ?", (str(snapshot.id),)
    )
    if existing is not None:
        snapshot_row(existing)
        if existing["payload_json"] != payload:
            raise ConflictError("Snapshot ID already has different content.")
        return
    await connection.execute(
        """INSERT INTO portfolio_snapshots
           (snapshot_id, confirmed_at, payload_json, schema_version, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (
            str(snapshot.id),
            utc_text(snapshot.confirmed_at),
            payload,
            PAYLOAD_VERSION,
            utc_text(datetime.now(UTC)),
        ),
    )


async def current(connection) -> CurrentPortfolio | None:
    row = await one(
        connection,
        """SELECT s.*, c.revision FROM current_portfolio c
        JOIN portfolio_snapshots s ON s.snapshot_id = c.snapshot_id WHERE c.singleton_id = ?""",
        (1,),
    )
    return CurrentPortfolio(snapshot_row(row), row["revision"]) if row is not None else None


async def append_update(connection, update: ConfirmedUpdate) -> None:
    payload = UpdateCodec.encode(update)
    UpdateCodec.decode(payload)
    new_row = await one(
        connection,
        "SELECT * FROM portfolio_snapshots WHERE snapshot_id = ?",
        (str(update.new_snapshot_id),),
    )
    if new_row is None:
        raise PersistenceError("Update references a missing new snapshot.")
    new_snapshot = snapshot_row(new_row)
    if new_snapshot.confirmed_at != update.confirmed_at:
        raise ConflictError("Update timestamp disagrees with new snapshot.")
    if update.previous_snapshot_id is not None:
        previous = await one(
            connection,
            "SELECT * FROM portfolio_snapshots WHERE snapshot_id = ?",
            (str(update.previous_snapshot_id),),
        )
        if previous is None:
            raise PersistenceError("Update references a missing previous snapshot.")
        if snapshot_row(previous).confirmed_at > update.confirmed_at:
            raise ConflictError("Confirmation time cannot move backwards.")
    try:
        await connection.execute(
            """INSERT INTO confirmed_updates
            (update_id, update_type, previous_snapshot_id, new_snapshot_id,
             confirmed_at, payload_json, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                str(update.update_id),
                update.update_type.value,
                str(update.previous_snapshot_id) if update.previous_snapshot_id else None,
                str(update.new_snapshot_id),
                utc_text(update.confirmed_at),
                payload,
                PAYLOAD_VERSION,
            ),
        )
    except aiosqlite.IntegrityError as error:
        raise ConflictError("Update ID or confirmed snapshot has already been recorded.") from error


class SQLiteSnapshotRepository:
    def __init__(self, database: Database):
        self._database = database

    async def save(self, snapshot: ConfirmedPortfolioSnapshot) -> None:
        async with self._database.transaction() as connection:
            await save_snapshot(connection, snapshot)

    async def get(self, snapshot_id: UUID) -> ConfirmedPortfolioSnapshot | None:
        identity(snapshot_id)
        async with self._database.connection() as connection:
            row = await one(
                connection,
                "SELECT * FROM portfolio_snapshots WHERE snapshot_id = ?",
                (str(snapshot_id),),
            )
            return snapshot_row(row) if row is not None else None

    async def get_current(self) -> CurrentPortfolio | None:
        async with self._database.connection() as connection:
            return await current(connection)

    async def list_recent(self, limit: int = 20) -> tuple[ConfirmedPortfolioSnapshot, ...]:
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await many(
                connection,
                """SELECT * FROM portfolio_snapshots
                ORDER BY confirmed_at DESC, snapshot_id ASC LIMIT ?""",
                (limit,),
            )
            return tuple(snapshot_row(row) for row in rows)

    async def set_current(
        self,
        snapshot: ConfirmedPortfolioSnapshot,
        *,
        expected_revision: int,
        update: ConfirmedUpdate,
    ) -> CurrentPortfolio:
        require(
            type(expected_revision) is int and expected_revision >= 0, "Invalid expected revision."
        )
        # Round-trip validates the complete nested value before starting a write transaction.
        snapshot = SnapshotCodec.decode(SnapshotCodec.encode(snapshot))
        update = UpdateCodec.decode(UpdateCodec.encode(update))
        async with self._database.transaction() as connection:
            existing = await current(connection)
            revision = existing.revision if existing else 0
            previous_id = existing.snapshot.id if existing else None
            if expected_revision != revision or update.previous_snapshot_id != previous_id:
                raise RevisionConflictError("Current state changed; reload before confirming.")
            if (
                update.new_snapshot_id != snapshot.id
                or update.confirmed_at != snapshot.confirmed_at
            ):
                raise ConflictError("Update must describe this exact snapshot.")
            if (existing is None) != (update.update_type is UpdateType.INITIAL_STATE):
                raise ConflictError("Initial update is allowed only for empty current state.")
            if previous_id == snapshot.id:
                raise ConflictError("Confirmation requires a new snapshot identity.")
            await save_snapshot(connection, snapshot)
            await append_update(connection, update)
            if existing is None:
                await connection.execute(
                    """INSERT INTO current_portfolio
                    (singleton_id, snapshot_id, revision, updated_at) VALUES (?, ?, ?, ?)""",
                    (1, str(snapshot.id), 1, utc_text(snapshot.confirmed_at)),
                )
            else:
                cursor = await connection.execute(
                    """UPDATE current_portfolio
                    SET snapshot_id = ?, revision = ?, updated_at = ?
                    WHERE singleton_id = ? AND revision = ?""",
                    (str(snapshot.id), revision + 1, utc_text(snapshot.confirmed_at), 1, revision),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflictError("Concurrent current-state update.")
            return CurrentPortfolio(snapshot, revision + 1)


class SQLiteConfirmedUpdateRepository:
    def __init__(self, database: Database):
        self._database = database

    async def append(self, update: ConfirmedUpdate) -> None:
        # Append-only audit capability; never changes current state or balances.
        async with self._database.transaction() as connection:
            await append_update(connection, update)

    async def list_recent(self, limit: int = 20) -> tuple[ConfirmedUpdate, ...]:
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await many(
                connection,
                """SELECT * FROM confirmed_updates
                ORDER BY confirmed_at DESC, update_id ASC LIMIT ?""",
                (limit,),
            )
            return tuple(update_row(row) for row in rows)


class SQLiteAnalysisRunRepository:
    def __init__(self, database: Database):
        self._database = database

    async def list_all_recent(self, limit: int = 20) -> tuple[StoredAnalysisRun, ...]:
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await many(
                connection,
                """SELECT * FROM analysis_runs
                ORDER BY started_at DESC, run_id ASC LIMIT ?""",
                (limit,),
            )
            return tuple(AnalysisRunCodec.from_record(dict(row)) for row in rows)

    async def latest_completed(self) -> StoredAnalysisRun | None:
        async with self._database.connection() as connection:
            row = await one(
                connection,
                """SELECT * FROM analysis_runs
                WHERE status = ? AND result_json IS NOT NULL
                ORDER BY completed_at DESC, run_id ASC LIMIT 1""",
                (AnalysisRunStatus.COMPLETED.value,),
            )
            return AnalysisRunCodec.from_record(dict(row)) if row is not None else None

    async def acquire(self, run: AnalysisRun) -> None:
        """Run UUID is the fencing token; recovery and acquisition share one write transaction."""
        await self._create(run, exclusive=True)

    async def create(self, run: AnalysisRun) -> None:
        # Low-level import/fixture API retained for old callers. Executions MUST use acquire.
        await self._create(run, exclusive=False)

    async def _create(self, run: AnalysisRun, *, exclusive: bool) -> None:
        run = AnalysisRunCodec.decode(AnalysisRunCodec.encode(run))
        require(run.status is AnalysisRunStatus.RUNNING, "New runs must be RUNNING.")
        async with self._database.transaction() as connection:
            if exclusive:
                rows = await many(connection, "SELECT * FROM analysis_runs WHERE status='running'")
                active = [AnalysisRunCodec.from_record(dict(row)).run for row in rows]
                if any(lease_expires(r.started_at) > run.started_at for r in active):
                    raise ConflictError("Analysis already active; wait for its lease to finish.")
                await connection.execute(
                    """UPDATE analysis_runs SET status='failed', completed_at=?,
                       failure_category=?, result_json=NULL
                       WHERE status='running' AND started_at<=?""",
                    (
                        utc_text(run.started_at),
                        FailureCategory.INTERRUPTED.value,
                        utc_text(run.started_at - LEASE_DURATION),
                    ),
                )
            if (
                await one(
                    connection,
                    "SELECT run_id FROM analysis_runs WHERE run_id = ?",
                    (str(run.run_id),),
                )
                is not None
            ):
                raise ConflictError("Run ID already exists.")
            snapshot = await one(
                connection,
                "SELECT * FROM portfolio_snapshots WHERE snapshot_id = ?",
                (str(run.snapshot_id),),
            )
            if snapshot is None:
                raise PersistenceError("Run references a missing snapshot.")
            if run.started_at < snapshot_row(snapshot).confirmed_at:
                raise ConflictError("Run cannot start before snapshot confirmation.")
            await connection.execute(
                """INSERT INTO analysis_runs
                (run_id, snapshot_id, status, started_at, completed_at, market_cutoff,
                 result_json, failure_category, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(run.run_id),
                    str(run.snapshot_id),
                    run.status.value,
                    utc_text(run.started_at),
                    None,
                    utc_text(run.market_cutoff) if run.market_cutoff is not None else None,
                    None,
                    None,
                    PAYLOAD_VERSION,
                ),
            )

    async def get(self, run_id: UUID) -> StoredAnalysisRun | None:
        identity(run_id)
        async with self._database.connection() as connection:
            row = await one(
                connection, "SELECT * FROM analysis_runs WHERE run_id = ?", (str(run_id),)
            )
            return AnalysisRunCodec.from_record(dict(row)) if row is not None else None

    async def mark_completed(
        self,
        run_id: UUID,
        *,
        completed_at: datetime,
        result_json: str | None = None,
        market_cutoff: datetime | None = None,
    ) -> StoredAnalysisRun:
        return await self._finish(
            run_id,
            AnalysisRunStatus.COMPLETED,
            completed_at,
            validate_result(result_json),
            None,
            market_cutoff,
        )

    async def mark_failed(
        self, run_id: UUID, *, completed_at: datetime, failure_category: FailureCategory
    ) -> StoredAnalysisRun:
        require(isinstance(failure_category, FailureCategory), "Use a sanitized failure category.")
        return await self._finish(
            run_id, AnalysisRunStatus.FAILED, completed_at, None, failure_category
        )

    async def _finish(
        self, run_id, status, completed_at, result_json, failure_category, market_cutoff=None
    ):
        identity(run_id)
        timestamp(completed_at)
        async with self._database.transaction() as connection:
            row = await one(
                connection, "SELECT * FROM analysis_runs WHERE run_id = ?", (str(run_id),)
            )
            if row is None:
                raise PersistenceError("Analysis run does not exist.")
            previous = AnalysisRunCodec.from_record(dict(row))
            if previous.run.status is not AnalysisRunStatus.RUNNING:
                raise ConflictError("Terminal analysis runs cannot be changed.")
            if status is AnalysisRunStatus.COMPLETED and completed_at >= lease_expires(
                previous.run.started_at
            ):
                raise ConflictError("Analysis lease expired; result cannot be published.")
            updated = StoredAnalysisRun(
                replace(
                    previous.run,
                    status=status,
                    completed_at=completed_at,
                    market_cutoff=market_cutoff
                    if market_cutoff is not None
                    else previous.run.market_cutoff,
                ),
                result_json,
                failure_category,
            )
            await connection.execute(
                """UPDATE analysis_runs
                SET status = ?, completed_at = ?, result_json = ?, failure_category = ?,
                    market_cutoff = ?
                WHERE run_id = ? AND status = ?""",
                (
                    status.value,
                    utc_text(completed_at),
                    result_json,
                    failure_category.value if failure_category else None,
                    utc_text(updated.run.market_cutoff)
                    if updated.run.market_cutoff is not None
                    else None,
                    str(run_id),
                    AnalysisRunStatus.RUNNING.value,
                ),
            )
            return updated

    async def list_recent(
        self, snapshot_id: UUID, limit: int = 20
    ) -> tuple[StoredAnalysisRun, ...]:
        identity(snapshot_id)
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await many(
                connection,
                """SELECT * FROM analysis_runs WHERE snapshot_id = ?
                ORDER BY started_at DESC, run_id ASC LIMIT ?""",
                (str(snapshot_id), limit),
            )
            return tuple(AnalysisRunCodec.from_record(dict(row)) for row in rows)
