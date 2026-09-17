"""PostgreSQL implementations of the existing repository protocols."""

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.analysis.recovery import LEASE_DURATION, lease_expires
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.delivery.models import (
    DeliveryFailureCategory,
    DeliveryRecord,
    DeliveryStatus,
    utc,
)
from advanced_trading_advisor.domain.persistence import (
    ConfirmedUpdate,
    ConflictError,
    CurrentPortfolio,
    FailureCategory,
    PersistenceError,
    StoredAnalysisRun,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import identity, require, timestamp
from advanced_trading_advisor.portfolio.models import ConfirmedPortfolioSnapshot
from advanced_trading_advisor.scheduler.models import ScheduledExecution, ScheduledStatus

from .codecs import (
    PAYLOAD_VERSION,
    AnalysisRunCodec,
    SnapshotCodec,
    UpdateCodec,
    utc_text,
    validate_result,
)
from .delivery import SENDING_STALE_AFTER, delivery_row
from .repositories import limit_value, snapshot_row, update_row
from .scheduler import scheduled_row

PORTFOLIO_LOCK = 7_126_105_306_185_989_231
ANALYSIS_LOCK = 7_126_105_306_185_989_232
SCHEDULER_LOCK = 7_126_105_306_185_989_233
DELIVERY_LOCK = 7_126_105_306_185_989_234

UUID_COLUMNS = {
    "snapshot_id",
    "update_id",
    "previous_snapshot_id",
    "new_snapshot_id",
    "run_id",
    "owner_token",
    "analysis_run_id",
    "delivery_id",
}
TIMESTAMP_COLUMNS = {
    "confirmed_at",
    "created_at",
    "updated_at",
    "started_at",
    "completed_at",
    "market_cutoff",
    "scheduled_for",
    "expected_cutoff",
    "finished_at",
}


def normalized(row) -> dict:
    result = dict(row)
    for key in UUID_COLUMNS & result.keys():
        if result[key] is not None:
            result[key] = str(result[key])
    for key in TIMESTAMP_COLUMNS & result.keys():
        if result[key] is not None:
            result[key] = utc_text(result[key])
    return result


def affected(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[1])
    except (AttributeError, IndexError, ValueError):
        raise PersistenceError("Unexpected PostgreSQL command result.") from None


async def advisory_lock(connection, key: int) -> None:
    await connection.execute("SELECT pg_advisory_xact_lock($1)", key)


async def pg_save_snapshot(connection, snapshot: ConfirmedPortfolioSnapshot) -> None:
    payload = SnapshotCodec.encode(snapshot)
    SnapshotCodec.decode(payload)
    await connection.execute(
        """INSERT INTO portfolio_snapshots
           (snapshot_id, confirmed_at, payload_json, schema_version, created_at)
           VALUES ($1, $2, $3, $4, $5) ON CONFLICT (snapshot_id) DO NOTHING""",
        snapshot.id,
        snapshot.confirmed_at,
        payload,
        PAYLOAD_VERSION,
        datetime.now(UTC),
    )
    row = await connection.fetchrow(
        "SELECT * FROM portfolio_snapshots WHERE snapshot_id=$1", snapshot.id
    )
    existing = snapshot_row(normalized(row))
    if row["payload_json"] != payload or existing != snapshot:
        raise ConflictError("Snapshot ID already has different content.")


async def pg_current(connection, *, lock: bool = False) -> CurrentPortfolio | None:
    suffix = " FOR UPDATE OF c" if lock else ""
    row = await connection.fetchrow(
        """SELECT s.*, c.revision FROM current_portfolio c
           JOIN portfolio_snapshots s ON s.snapshot_id=c.snapshot_id
           WHERE c.singleton_id=1"""
        + suffix
    )
    return (
        CurrentPortfolio(snapshot_row(normalized(row)), row["revision"])
        if row is not None
        else None
    )


async def pg_append_update(connection, update: ConfirmedUpdate) -> None:
    payload = UpdateCodec.encode(update)
    UpdateCodec.decode(payload)
    new_row = await connection.fetchrow(
        "SELECT * FROM portfolio_snapshots WHERE snapshot_id=$1", update.new_snapshot_id
    )
    if new_row is None:
        raise PersistenceError("Update references a missing new snapshot.")
    new_snapshot = snapshot_row(normalized(new_row))
    if new_snapshot.confirmed_at != update.confirmed_at:
        raise ConflictError("Update timestamp disagrees with new snapshot.")
    if update.previous_snapshot_id is not None:
        previous = await connection.fetchrow(
            "SELECT * FROM portfolio_snapshots WHERE snapshot_id=$1",
            update.previous_snapshot_id,
        )
        if previous is None:
            raise PersistenceError("Update references a missing previous snapshot.")
        if snapshot_row(normalized(previous)).confirmed_at > update.confirmed_at:
            raise ConflictError("Confirmation time cannot move backwards.")
    status = await connection.execute(
        """INSERT INTO confirmed_updates
           (update_id, update_type, previous_snapshot_id, new_snapshot_id,
            confirmed_at, payload_json, schema_version)
           VALUES ($1,$2,$3,$4,$5,$6,$7)
           ON CONFLICT DO NOTHING""",
        update.update_id,
        update.update_type.value,
        update.previous_snapshot_id,
        update.new_snapshot_id,
        update.confirmed_at,
        payload,
        PAYLOAD_VERSION,
    )
    if affected(status) != 1:
        raise ConflictError("Update ID or confirmed snapshot has already been recorded.")


class PostgresSnapshotRepository:
    def __init__(self, database):
        self._database = database

    async def save(self, snapshot: ConfirmedPortfolioSnapshot) -> None:
        async with self._database.transaction() as connection:
            await pg_save_snapshot(connection, snapshot)

    async def get(self, snapshot_id: UUID) -> ConfirmedPortfolioSnapshot | None:
        identity(snapshot_id)
        async with self._database.connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM portfolio_snapshots WHERE snapshot_id=$1", snapshot_id
            )
            return snapshot_row(normalized(row)) if row is not None else None

    async def get_current(self) -> CurrentPortfolio | None:
        async with self._database.connection() as connection:
            return await pg_current(connection)

    async def list_recent(self, limit: int = 20) -> tuple[ConfirmedPortfolioSnapshot, ...]:
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await connection.fetch(
                """SELECT * FROM portfolio_snapshots
                   ORDER BY confirmed_at DESC, snapshot_id ASC LIMIT $1""",
                limit,
            )
            return tuple(snapshot_row(normalized(row)) for row in rows)

    async def set_current(
        self,
        snapshot: ConfirmedPortfolioSnapshot,
        *,
        expected_revision: int,
        update: ConfirmedUpdate,
    ) -> CurrentPortfolio:
        require(
            type(expected_revision) is int and expected_revision >= 0,
            "Invalid expected revision.",
        )
        snapshot = SnapshotCodec.decode(SnapshotCodec.encode(snapshot))
        update = UpdateCodec.decode(UpdateCodec.encode(update))
        async with self._database.transaction() as connection:
            await advisory_lock(connection, PORTFOLIO_LOCK)
            existing = await pg_current(connection, lock=True)
            revision = existing.revision if existing else 0
            previous_id = existing.snapshot.id if existing else None
            if expected_revision != revision or update.previous_snapshot_id != previous_id:
                raise ConflictError("Current state changed; reload before confirming.")
            if (
                update.new_snapshot_id != snapshot.id
                or update.confirmed_at != snapshot.confirmed_at
            ):
                raise ConflictError("Update must describe this exact snapshot.")
            if (existing is None) != (update.update_type is UpdateType.INITIAL_STATE):
                raise ConflictError("Initial update is allowed only for empty current state.")
            if previous_id == snapshot.id:
                raise ConflictError("Confirmation requires a new snapshot identity.")
            await pg_save_snapshot(connection, snapshot)
            await pg_append_update(connection, update)
            if existing is None:
                await connection.execute(
                    """INSERT INTO current_portfolio
                       (singleton_id, snapshot_id, revision, updated_at)
                       VALUES (1,$1,1,$2)""",
                    snapshot.id,
                    snapshot.confirmed_at,
                )
            else:
                status = await connection.execute(
                    """UPDATE current_portfolio SET snapshot_id=$1, revision=$2, updated_at=$3
                       WHERE singleton_id=1 AND revision=$4""",
                    snapshot.id,
                    revision + 1,
                    snapshot.confirmed_at,
                    revision,
                )
                if affected(status) != 1:
                    raise ConflictError("Concurrent current-state update.")
            return CurrentPortfolio(snapshot, revision + 1)


class PostgresConfirmedUpdateRepository:
    def __init__(self, database):
        self._database = database

    async def append(self, update: ConfirmedUpdate) -> None:
        async with self._database.transaction() as connection:
            await pg_append_update(connection, update)

    async def list_recent(self, limit: int = 20) -> tuple[ConfirmedUpdate, ...]:
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await connection.fetch(
                """SELECT * FROM confirmed_updates
                   ORDER BY confirmed_at DESC, update_id ASC LIMIT $1""",
                limit,
            )
            return tuple(update_row(normalized(row)) for row in rows)


class PostgresAnalysisRunRepository:
    def __init__(self, database):
        self._database = database

    @staticmethod
    def _stored(row) -> StoredAnalysisRun:
        return AnalysisRunCodec.from_record(normalized(row))

    async def list_all_recent(self, limit: int = 20) -> tuple[StoredAnalysisRun, ...]:
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await connection.fetch(
                "SELECT * FROM analysis_runs ORDER BY started_at DESC, run_id ASC LIMIT $1",
                limit,
            )
            return tuple(self._stored(row) for row in rows)

    async def latest_completed(self) -> StoredAnalysisRun | None:
        async with self._database.connection() as connection:
            row = await connection.fetchrow(
                """SELECT * FROM analysis_runs WHERE status=$1 AND result_json IS NOT NULL
                   ORDER BY completed_at DESC, run_id ASC LIMIT 1""",
                AnalysisRunStatus.COMPLETED.value,
            )
            return self._stored(row) if row is not None else None

    async def acquire(self, run: AnalysisRun) -> None:
        await self._create(run, exclusive=True)

    async def create(self, run: AnalysisRun) -> None:
        await self._create(run, exclusive=False)

    async def _create(self, run: AnalysisRun, *, exclusive: bool) -> None:
        run = AnalysisRunCodec.decode(AnalysisRunCodec.encode(run))
        require(run.status is AnalysisRunStatus.RUNNING, "New runs must be RUNNING.")
        async with self._database.transaction() as connection:
            await advisory_lock(connection, ANALYSIS_LOCK)
            if exclusive:
                rows = await connection.fetch(
                    "SELECT * FROM analysis_runs WHERE status='running' FOR UPDATE"
                )
                active = [self._stored(row).run for row in rows]
                if any(lease_expires(value.started_at) > run.started_at for value in active):
                    raise ConflictError("Analysis already active; wait for its lease to finish.")
                await connection.execute(
                    """UPDATE analysis_runs SET status='failed', completed_at=$1,
                       failure_category=$2, result_json=NULL
                       WHERE status='running' AND started_at<=$3""",
                    run.started_at,
                    FailureCategory.INTERRUPTED.value,
                    run.started_at - LEASE_DURATION,
                )
            if await connection.fetchval("SELECT 1 FROM analysis_runs WHERE run_id=$1", run.run_id):
                raise ConflictError("Run ID already exists.")
            snapshot = await connection.fetchrow(
                "SELECT * FROM portfolio_snapshots WHERE snapshot_id=$1", run.snapshot_id
            )
            if snapshot is None:
                raise PersistenceError("Run references a missing snapshot.")
            if run.started_at < snapshot_row(normalized(snapshot)).confirmed_at:
                raise ConflictError("Run cannot start before snapshot confirmation.")
            status = await connection.execute(
                """INSERT INTO analysis_runs
                   (run_id,snapshot_id,status,started_at,completed_at,market_cutoff,
                    result_json,failure_category,schema_version)
                   VALUES ($1,$2,$3,$4,NULL,$5,NULL,NULL,$6)
                   ON CONFLICT DO NOTHING""",
                run.run_id,
                run.snapshot_id,
                run.status.value,
                run.started_at,
                run.market_cutoff,
                PAYLOAD_VERSION,
            )
            if affected(status) != 1:
                raise ConflictError("Run ID already exists or another analysis lease is active.")

    async def get(self, run_id: UUID) -> StoredAnalysisRun | None:
        identity(run_id)
        async with self._database.connection() as connection:
            row = await connection.fetchrow("SELECT * FROM analysis_runs WHERE run_id=$1", run_id)
            return self._stored(row) if row is not None else None

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
        self,
        run_id: UUID,
        *,
        completed_at: datetime,
        failure_category: FailureCategory,
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
            row = await connection.fetchrow(
                "SELECT * FROM analysis_runs WHERE run_id=$1 FOR UPDATE", run_id
            )
            if row is None:
                raise PersistenceError("Analysis run does not exist.")
            previous = self._stored(row)
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
            command = await connection.execute(
                """UPDATE analysis_runs SET status=$1, completed_at=$2, result_json=$3,
                   failure_category=$4, market_cutoff=$5
                   WHERE run_id=$6 AND status='running'""",
                status.value,
                completed_at,
                result_json,
                failure_category.value if failure_category else None,
                updated.run.market_cutoff,
                run_id,
            )
            if affected(command) != 1:
                raise ConflictError("Analysis completion fence rejected the update.")
            return updated

    async def list_recent(
        self, snapshot_id: UUID, limit: int = 20
    ) -> tuple[StoredAnalysisRun, ...]:
        identity(snapshot_id)
        limit_value(limit)
        async with self._database.connection() as connection:
            rows = await connection.fetch(
                """SELECT * FROM analysis_runs WHERE snapshot_id=$1
                   ORDER BY started_at DESC, run_id ASC LIMIT $2""",
                snapshot_id,
                limit,
            )
            return tuple(self._stored(row) for row in rows)


class PostgresSchedulerRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _record(row) -> ScheduledExecution:
        return scheduled_row(normalized(row))

    async def _recover(self, connection, now):
        rows = await connection.fetch(
            "SELECT * FROM scheduler_executions WHERE status='RUNNING' FOR UPDATE"
        )
        for row in rows:
            record = self._record(row)
            if lease_expires(record.started_at) <= now:
                await self._resolve(
                    connection,
                    record,
                    now,
                    ScheduledStatus.FAILED,
                    FailureCategory.INTERRUPTED,
                    recovering=True,
                )

    async def _resolve(self, connection, record, now, status, category, *, recovering=False):
        row = await connection.fetchrow(
            "SELECT * FROM analysis_runs WHERE run_id=$1 FOR UPDATE", record.owner_token
        )
        analysis_id = None
        if row:
            stored = AnalysisRunCodec.from_record(normalized(row))
            analysis_id = stored.run.run_id
            if stored.run.status is AnalysisRunStatus.RUNNING:
                if recovering and lease_expires(stored.run.started_at) > now:
                    return None
                await connection.execute(
                    """UPDATE analysis_runs SET status='failed', completed_at=$1,
                       failure_category=$2, result_json=NULL
                       WHERE run_id=$3 AND status='running'""",
                    now,
                    FailureCategory.INTERRUPTED.value,
                    analysis_id,
                )
            elif stored.run.status is AnalysisRunStatus.COMPLETED:
                result = AnalysisResultCodec.decode(stored.result_json)
                if (
                    result.run_id != analysis_id
                    or result.snapshot_id != stored.run.snapshot_id
                    or result.generated_at != stored.run.completed_at
                    or result.market_cutoff != record.expected_cutoff
                    or stored.run.market_cutoff != record.expected_cutoff
                ):
                    raise ConflictError(
                        "Completed scheduled result has inconsistent identity/cutoff."
                    )
                status, category = ScheduledStatus.COMPLETED, None
            elif category is None:
                category = stored.failure_category or FailureCategory.ANALYSIS_FAILED
        if status is ScheduledStatus.COMPLETED and (row is None or row["status"] != "completed"):
            raise ConflictError("Cannot complete a scheduled slot without a completed analysis.")
        updated = replace(
            record,
            finished_at=now,
            status=status,
            analysis_run_id=analysis_id,
            failure_category=category,
        )
        command = await connection.execute(
            """UPDATE scheduler_executions SET finished_at=$1,status=$2,
               analysis_run_id=$3,failure_category=$4
               WHERE scheduled_for=$5 AND owner_token=$6 AND status='RUNNING'""",
            updated.finished_at,
            updated.status.value,
            updated.analysis_run_id,
            updated.failure_category.value if updated.failure_category else None,
            updated.scheduled_for,
            updated.owner_token,
        )
        if affected(command) != 1:
            raise ConflictError("Scheduled slot is no longer owned by this execution.")
        return updated

    async def recover(self, now):
        async with self.database.transaction() as connection:
            await advisory_lock(connection, SCHEDULER_LOCK)
            await self._recover(connection, now)

    async def claim(self, record):
        async with self.database.transaction() as connection:
            await advisory_lock(connection, SCHEDULER_LOCK)
            await self._recover(connection, record.started_at)
            status = await connection.execute(
                """INSERT INTO scheduler_executions
                   (scheduled_for,expected_cutoff,owner_token,started_at,status)
                   VALUES ($1,$2,$3,$4,'RUNNING') ON CONFLICT DO NOTHING""",
                record.scheduled_for,
                record.expected_cutoff,
                record.owner_token,
                record.started_at,
            )
            return affected(status) == 1

    async def finish(self, record, now, status, category=None):
        async with self.database.transaction() as connection:
            await advisory_lock(connection, SCHEDULER_LOCK)
            row = await connection.fetchrow(
                "SELECT * FROM scheduler_executions WHERE scheduled_for=$1 FOR UPDATE",
                record.scheduled_for,
            )
            if row is None:
                raise ConflictError("Scheduled slot does not exist.")
            current = self._record(row)
            if (
                current.owner_token != record.owner_token
                or current.status is not ScheduledStatus.RUNNING
            ):
                raise ConflictError("Scheduled slot ownership changed.")
            return await self._resolve(connection, current, now, status, category)

    async def latest(self):
        async with self.database.connection() as connection:
            row = await connection.fetchrow(
                """SELECT * FROM scheduler_executions
                   ORDER BY started_at DESC, scheduled_for DESC LIMIT 1"""
            )
            return self._record(row) if row else None

    async def get(self, scheduled_for):
        async with self.database.connection() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM scheduler_executions WHERE scheduled_for=$1", scheduled_for
            )
            return self._record(row) if row else None


class PostgresDeliveryRepository:
    def __init__(self, database):
        self.database = database

    @staticmethod
    def _record(row) -> DeliveryRecord:
        return delivery_row(normalized(row))

    async def _recover(self, connection, at):
        utc(at)
        rows = await connection.fetch(
            "SELECT * FROM telegram_deliveries WHERE status='SENDING' FOR UPDATE"
        )
        for raw in rows:
            record = self._record(raw)
            if record.started_at + SENDING_STALE_AFTER > at:
                continue
            if record.attempt_count == 0:
                await connection.execute(
                    """UPDATE telegram_deliveries SET status='PENDING', started_at=NULL
                       WHERE delivery_id=$1 AND status='SENDING'""",
                    record.delivery_id,
                )
            else:
                await connection.execute(
                    """UPDATE telegram_deliveries SET status='AMBIGUOUS', finished_at=$1,
                       failure_category=$2 WHERE delivery_id=$3 AND status='SENDING'""",
                    at,
                    DeliveryFailureCategory.TELEGRAM_AMBIGUOUS.value,
                    record.delivery_id,
                )

    async def recover_stale(self, at):
        async with self.database.transaction() as connection:
            await advisory_lock(connection, DELIVERY_LOCK)
            await self._recover(connection, at)

    async def claim(self, record: DeliveryRecord, *, manual_retry=False):
        require(record.status is DeliveryStatus.PENDING, "Claim requires a pending delivery.")
        require(type(manual_retry) is bool, "Invalid manual retry flag.")
        async with self.database.transaction() as connection:
            await advisory_lock(connection, DELIVERY_LOCK)
            await self._recover(connection, record.created_at)
            await connection.execute(
                """INSERT INTO telegram_deliveries
                   (delivery_id,analysis_run_id,channel,destination_key,created_at,status,attempt_count)
                   VALUES ($1,$2,'TELEGRAM',$3,$4,'PENDING',0)
                   ON CONFLICT (analysis_run_id,channel,destination_key) DO NOTHING""",
                record.delivery_id,
                record.analysis_run_id,
                record.destination_key,
                record.created_at,
            )
            row = await connection.fetchrow(
                """SELECT * FROM telegram_deliveries
                   WHERE analysis_run_id=$1 AND channel='TELEGRAM' AND destination_key=$2
                   FOR UPDATE""",
                record.analysis_run_id,
                record.destination_key,
            )
            existing = self._record(row)
            active = existing.status is DeliveryStatus.PENDING or (
                manual_retry and existing.status is DeliveryStatus.FAILED
            )
            if active:
                await connection.execute(
                    """UPDATE telegram_deliveries SET status='SENDING',started_at=$1,
                       finished_at=NULL,failure_category=NULL WHERE delivery_id=$2""",
                    record.created_at,
                    existing.delivery_id,
                )
            saved = await connection.fetchrow(
                "SELECT * FROM telegram_deliveries WHERE delivery_id=$1", existing.delivery_id
            )
            return self._record(saved), active

    async def start_attempt(self, delivery_id: UUID):
        identity(delivery_id)
        async with self.database.transaction() as connection:
            command = await connection.execute(
                """UPDATE telegram_deliveries SET attempt_count=attempt_count+1
                   WHERE delivery_id=$1 AND status='SENDING'""",
                delivery_id,
            )
            if affected(command) != 1:
                raise ConflictError("Delivery is no longer sending.")
            return self._record(
                await connection.fetchrow(
                    "SELECT * FROM telegram_deliveries WHERE delivery_id=$1", delivery_id
                )
            )

    async def finish(self, delivery_id, at, status, category=None):
        identity(delivery_id)
        utc(at)
        require(
            status in {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, DeliveryStatus.AMBIGUOUS},
            "Invalid delivery outcome.",
        )
        if status is DeliveryStatus.DELIVERED:
            require(category is None, "Delivered notification cannot have failure category.")
        else:
            require(isinstance(category, DeliveryFailureCategory), "Sanitized failure required.")
        async with self.database.transaction() as connection:
            command = await connection.execute(
                """UPDATE telegram_deliveries SET status=$1,finished_at=$2,failure_category=$3
                   WHERE delivery_id=$4 AND status='SENDING' AND attempt_count>0""",
                status.value,
                at,
                category.value if category else None,
                delivery_id,
            )
            if affected(command) != 1:
                raise ConflictError("Delivery is no longer sending.")
            return self._record(
                await connection.fetchrow(
                    "SELECT * FROM telegram_deliveries WHERE delivery_id=$1", delivery_id
                )
            )

    async def latest(self):
        async with self.database.connection() as connection:
            row = await connection.fetchrow(
                """SELECT * FROM telegram_deliveries
                   ORDER BY created_at DESC, delivery_id DESC LIMIT 1"""
            )
            return self._record(row) if row else None

    async def get(self, analysis_run_id, destination_key):
        identity(analysis_run_id)
        async with self.database.connection() as connection:
            row = await connection.fetchrow(
                """SELECT * FROM telegram_deliveries
                   WHERE analysis_run_id=$1 AND channel='TELEGRAM' AND destination_key=$2""",
                analysis_run_id,
                destination_key,
            )
            return self._record(row) if row else None
