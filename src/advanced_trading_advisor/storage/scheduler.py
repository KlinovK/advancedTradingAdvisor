from dataclasses import replace

from advanced_trading_advisor.analysis.models import AnalysisRunStatus
from advanced_trading_advisor.analysis.recovery import lease_expires
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.domain.persistence import ConflictError, FailureCategory
from advanced_trading_advisor.scheduler.models import ScheduledExecution, ScheduledStatus

from .codecs import AnalysisRunCodec, date, uid, utc_text
from .repositories import many, one


def scheduled_row(row):
    return ScheduledExecution(
        date(row["scheduled_for"]),
        date(row["expected_cutoff"]),
        uid(row["owner_token"]),
        date(row["started_at"]),
        date(row["finished_at"]) if row["finished_at"] else None,
        ScheduledStatus(row["status"]),
        uid(row["analysis_run_id"]) if row["analysis_run_id"] else None,
        FailureCategory(row["failure_category"]) if row["failure_category"] else None,
    )


async def finish_slot(c, record):
    cursor = await c.execute(
        """UPDATE scheduler_executions SET finished_at=?, status=?,
        analysis_run_id=?, failure_category=?
        WHERE scheduled_for=? AND owner_token=? AND status='RUNNING' """,
        (
            utc_text(record.finished_at),
            record.status.value,
            str(record.analysis_run_id) if record.analysis_run_id else None,
            record.failure_category.value if record.failure_category else None,
            utc_text(record.scheduled_for),
            str(record.owner_token),
        ),
    )
    if cursor.rowcount != 1:
        raise ConflictError("Scheduled slot is no longer owned by this execution.")


class SQLiteSchedulerRepository:
    def __init__(self, database):
        self.database = database

    async def _recover(self, c, now):
        rows = await many(c, "SELECT * FROM scheduler_executions WHERE status='RUNNING'")
        for row in rows:
            record = scheduled_row(row)
            if lease_expires(record.started_at) > now:
                continue
            await self._resolve(
                c, record, now, ScheduledStatus.FAILED, FailureCategory.INTERRUPTED, recovering=True
            )

    async def _resolve(self, c, record, now, status, category, *, recovering=False):
        row = await one(c, "SELECT * FROM analysis_runs WHERE run_id=?", (str(record.owner_token),))
        analysis_id = None
        if row:
            stored = AnalysisRunCodec.from_record(dict(row))
            analysis_id = stored.run.run_id
            if stored.run.status is AnalysisRunStatus.RUNNING:
                if recovering and lease_expires(stored.run.started_at) > now:
                    return  # Never fail a still-valid analysis lease.
                await c.execute(
                    """UPDATE analysis_runs SET status='failed', completed_at=?,
                    failure_category=?, result_json=NULL WHERE run_id=? AND status='running'""",
                    (utc_text(now), FailureCategory.INTERRUPTED.value, str(analysis_id)),
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
        await finish_slot(c, updated)
        return updated

    async def recover(self, now):
        async with self.database.transaction() as c:
            await self._recover(c, now)

    async def claim(self, record):
        async with self.database.transaction() as c:
            await self._recover(c, record.started_at)
            existing = await one(
                c,
                "SELECT * FROM scheduler_executions WHERE scheduled_for=?",
                (utc_text(record.scheduled_for),),
            )
            if existing:
                return False
            await c.execute(
                """INSERT INTO scheduler_executions
                (scheduled_for, expected_cutoff, owner_token, started_at, status)
                VALUES (?,?,?,?, 'RUNNING')""",
                (
                    utc_text(record.scheduled_for),
                    utc_text(record.expected_cutoff),
                    str(record.owner_token),
                    utc_text(record.started_at),
                ),
            )
            return True

    async def finish(self, record, now, status, category=None):
        async with self.database.transaction() as c:
            row = await one(
                c,
                "SELECT * FROM scheduler_executions WHERE scheduled_for=?",
                (utc_text(record.scheduled_for),),
            )
            current = scheduled_row(row)
            if (
                current.owner_token != record.owner_token
                or current.status is not ScheduledStatus.RUNNING
            ):
                raise ConflictError("Scheduled slot ownership changed.")
            return await self._resolve(c, current, now, status, category)

    async def latest(self):
        async with self.database.connection() as c:
            row = await one(
                c,
                """SELECT * FROM scheduler_executions
                ORDER BY started_at DESC, scheduled_for DESC LIMIT 1""",
            )
            return scheduled_row(row) if row else None

    async def get(self, scheduled_for):
        async with self.database.connection() as c:
            row = await one(
                c,
                "SELECT * FROM scheduler_executions WHERE scheduled_for=?",
                (utc_text(scheduled_for),),
            )
            return scheduled_row(row) if row else None
