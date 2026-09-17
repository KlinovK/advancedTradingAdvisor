"""Local read-only inspection and SQLite online backup; never restore or prune."""

import os
import sqlite3
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

from advanced_trading_advisor.analysis.models import AnalysisRunStatus
from advanced_trading_advisor.analysis.recovery import lease_expires
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.domain.persistence import PersistenceError

from .codecs import AnalysisRunCodec, utc_text
from .repositories import many, snapshot_row, update_row
from .schema import COLUMNS, LEGACY_COLUMNS, SCHEDULER_COLUMNS, SCHEMA_VERSION


@asynccontextmanager
async def readonly(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise PersistenceError("Database does not exist; choose an existing database.")
    async with aiosqlite.connect(
        path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=5
    ) as c:
        c.row_factory = aiosqlite.Row
        await c.execute("PRAGMA foreign_keys=ON")
        await c.execute("PRAGMA query_only=ON")
        yield c


async def check_schema(c):
    metadata = await many(c, "SELECT singleton_id, version FROM schema_metadata")
    if len(metadata) != 1 or metadata[0][0] != 1 or metadata[0][1] not in {1, 2, SCHEMA_VERSION}:
        raise PersistenceError("Unsupported schema metadata.")
    version = metadata[0][1]
    expected_columns = (
        LEGACY_COLUMNS if version == 1 else SCHEDULER_COLUMNS if version == 2 else COLUMNS
    )
    tables = await many(
        c, "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )
    if {row[0] for row in tables} != set(expected_columns):
        raise PersistenceError("Unsupported database layout.")
    for table, expected in expected_columns.items():
        columns = await many(c, "SELECT name FROM pragma_table_info(?)", (table,))
        if {row[0] for row in columns} != expected:
            raise PersistenceError("Unsupported table layout.")
    return version


@dataclass
class Inspection:
    schema_version: int | None = None
    checks: list[str] = field(default_factory=list)
    failed: bool = False
    snapshot_id: str | None = None
    revision: int = 0
    snapshots: int = 0
    runs: int = 0
    deliveries: int = 0
    last_completed: str | None = None

    def check(self, label, condition):
        self.checks.append(("PASS " if condition else "FAIL ") + label)
        self.failed |= not condition

    def render(self):
        return "\n".join(
            self.checks
            + [
                f"Schema version: {self.schema_version or 'see checks'}",
                f"Current snapshot: {self.snapshot_id or 'none'}; revision: {self.revision}",
                f"Snapshots: {self.snapshots}; analysis runs: {self.runs}",
                f"Telegram deliveries: {self.deliveries}",
                f"Last completed analysis: {self.last_completed or 'none'}",
            ]
        )


async def inspect_database(path, *, now=None):
    now = now or datetime.now(UTC)
    report = Inspection()
    try:
        async with readonly(path) as c:
            await c.execute("BEGIN")  # Consistent read snapshot, no writer lock.
            report.schema_version = await check_schema(c)
            report.check("supported schema", True)
            report.check(
                "SQLite integrity",
                [row[0] for row in await many(c, "PRAGMA integrity_check")] == ["ok"],
            )
            report.check("foreign keys", not await many(c, "PRAGMA foreign_key_check"))
            snapshots = {
                row["snapshot_id"]: snapshot_row(row)
                for row in await many(c, "SELECT * FROM portfolio_snapshots")
            }
            updates = [update_row(row) for row in await many(c, "SELECT * FROM confirmed_updates")]
            rows = await many(c, "SELECT * FROM current_portfolio")
            report.snapshots = len(snapshots)
            report.check("current singleton", len(rows) <= 1)
            if rows:
                row = rows[0]
                report.snapshot_id, report.revision = row["snapshot_id"], row["revision"]
                current = snapshots.get(report.snapshot_id)
                report.check(
                    "current reference/timestamp",
                    current is not None
                    and row["singleton_id"] == 1
                    and row["updated_at"] == utc_text(current.confirmed_at),
                )
            by_new = {str(u.new_snapshot_id): u for u in updates}
            seen, node = set(), report.snapshot_id
            consistent = len(by_new) == len(updates)
            while node is not None:
                if node in seen or node not in by_new or node not in snapshots:
                    consistent = False
                    break
                seen.add(node)
                update = by_new[node]
                consistent &= update.confirmed_at == snapshots[node].confirmed_at
                previous = str(update.previous_snapshot_id) if update.previous_snapshot_id else None
                if previous is not None:
                    consistent &= (
                        previous in snapshots
                        and snapshots[previous].confirmed_at <= update.confirmed_at
                    )
                node = previous
            report.check(
                "immutable audit chain/revision",
                consistent and len(seen) == len(updates) and report.revision == len(seen),
            )
            report.check("snapshot/order domain codecs", True)
            rows = await many(c, "SELECT * FROM analysis_runs")
            report.runs = len(rows)
            terminal_ok = True
            stale = 0
            for row in rows:
                stored = AnalysisRunCodec.from_record(dict(row))
                run = stored.run
                snapshot = snapshots.get(str(run.snapshot_id))
                terminal_ok &= snapshot is not None and run.started_at >= snapshot.confirmed_at
                if run.status is AnalysisRunStatus.RUNNING:
                    stale += lease_expires(run.started_at) <= now
                elif run.status is AnalysisRunStatus.COMPLETED:
                    if not stored.result_json:
                        terminal_ok = False
                        continue
                    result = AnalysisResultCodec.decode(stored.result_json)
                    terminal_ok &= (
                        result.run_id == run.run_id
                        and result.snapshot_id == run.snapshot_id
                        and result.generated_at == run.completed_at
                        and result.market_cutoff == run.market_cutoff
                    )
                    at = utc_text(run.completed_at)
                    report.last_completed = max(report.last_completed or at, at)
            report.check("analysis lifecycle/result codecs", terminal_ok)
            report.check(
                "at most one unexpired analysis lease",
                sum(row["status"] == "running" for row in rows) - stale <= 1,
            )
            triggers = {
                row[0]
                for row in await many(c, "SELECT name FROM sqlite_master WHERE type='trigger'")
            }
            report.check(
                "snapshot/audit immutability guards",
                {
                    "snapshots_no_update",
                    "snapshots_no_delete",
                    "updates_no_update",
                    "updates_no_delete",
                }
                <= triggers,
            )
            if stale:
                report.checks.append(
                    f"WARN stale RUNNING analyses: {stale}; recovered on next analysis"
                )
            if len(rows) and any(row["status"] == "running" for row in rows) and not stale:
                report.checks.append("PASS active RUNNING lease preserved")
            if report.schema_version >= 2:
                from advanced_trading_advisor.scheduler.models import ScheduledStatus

                from .scheduler import scheduled_row

                scheduled = await many(c, "SELECT * FROM scheduler_executions")
                by_id = {row["run_id"]: row for row in rows}
                consistent = True
                for row in scheduled:
                    record = scheduled_row(row)
                    analysis = by_id.get(str(record.owner_token))
                    if record.status is ScheduledStatus.COMPLETED:
                        consistent &= analysis is not None and analysis["status"] == "completed"
                        if analysis and analysis["result_json"]:
                            result = AnalysisResultCodec.decode(analysis["result_json"])
                            consistent &= result.market_cutoff == record.expected_cutoff
                    elif record.status is ScheduledStatus.RUNNING:
                        if lease_expires(record.started_at) <= now:
                            report.checks.append(
                                "WARN stale scheduler slot; recovered by scheduler run/once"
                            )
                    elif analysis is not None:
                        consistent &= analysis["status"] == "failed"
                report.check("scheduler records/analysis links", consistent)
            if report.schema_version >= 3:
                from advanced_trading_advisor.delivery.models import DeliveryStatus

                from .delivery import SENDING_STALE_AFTER, delivery_row

                delivered = await many(c, "SELECT * FROM telegram_deliveries")
                report.deliveries = len(delivered)
                by_id = {row["run_id"]: row for row in rows}
                consistent = {
                    "telegram_only_completed",
                    "telegram_transition_guard",
                    "telegram_no_delete",
                } <= triggers
                for row in delivered:
                    record = delivery_row(row)
                    analysis = by_id.get(str(record.analysis_run_id))
                    consistent &= analysis is not None and analysis["status"] == "completed"
                    if analysis and analysis["result_json"]:
                        result = AnalysisResultCodec.decode(analysis["result_json"])
                        consistent &= result.run_id == record.analysis_run_id
                    if (
                        record.status is DeliveryStatus.SENDING
                        and record.started_at + SENDING_STALE_AFTER <= now
                    ):
                        report.checks.append(
                            "WARN stale SENDING Telegram delivery; outcome may be ambiguous"
                        )
                    if record.status is DeliveryStatus.AMBIGUOUS:
                        report.checks.append(
                            "WARN ambiguous Telegram delivery; no automatic resend"
                        )
                report.check("Telegram delivery records/guards", consistent)
            await c.rollback()
    except (
        PersistenceError,
        sqlite3.Error,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
    ):
        report.check(
            "database/schema/payload readable (check path and local data integrity)", False
        )
    return report


async def backup_database(source, output=None, *, now=None):
    now = now or datetime.now(UTC)
    target = (
        Path(output).expanduser().absolute()
        if output
        else Path.home()
        / ".advanced_trading_advisor"
        / "backups"
        / f"advisor-{now.astimezone(UTC):%Y%m%dT%H%M%S%fZ}.sqlite"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise PersistenceError("Backup destination already exists; choose a new path.") from None
    os.close(fd)
    try:
        deadline = time.monotonic() + 30

        def progress(status, remaining, total):
            if time.monotonic() >= deadline:
                raise PersistenceError("Backup timed out; try again when database is less busy.")

        async with readonly(source) as src:
            async with aiosqlite.connect(target) as dst:
                await src.backup(dst, pages=128, progress=progress, sleep=0.05)
        report = await inspect_database(target, now=now)
        if report.failed:
            raise PersistenceError("Backup verification failed; source database was not changed.")
        return target, report
    except BaseException:
        # Only remove this call's exclusively created artifact, never an existing file.
        target.unlink(missing_ok=True)
        raise
