"""Explicit backup migration. No application lifecycle operations or provider calls."""

import hashlib
import json
import os
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from functools import wraps
from pathlib import Path
from uuid import UUID, uuid4

from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.domain.persistence import PersistenceError

from .codecs import AnalysisRunCodec, date, utc_text
from .delivery import delivery_row
from .maintenance import check_schema, readonly
from .postgres_database import SCHEMA_LOCK, PostgresDatabase
from .postgres_repositories import (
    ANALYSIS_LOCK,
    DELIVERY_LOCK,
    PORTFOLIO_LOCK,
    SCHEDULER_LOCK,
    TIMESTAMP_COLUMNS,
    UUID_COLUMNS,
)
from .postgres_schema import POSTGRES_COLUMNS, POSTGRES_MIGRATIONS, POSTGRES_SCHEMA_VERSION
from .repositories import many, snapshot_row, update_row
from .scheduler import scheduled_row

TABLES = (
    "portfolio_snapshots",
    "confirmed_updates",
    "analysis_runs",
    "scheduler_executions",
    "telegram_deliveries",
    "current_portfolio",
)
COLUMNS = {table: tuple(sorted(columns)) for table, columns in POSTGRES_COLUMNS.items()}
KEYS = dict(
    zip(
        TABLES,
        (
            "snapshot_id",
            "update_id",
            "run_id",
            "scheduled_for",
            "delivery_id",
            "singleton_id",
        ),
        strict=True,
    ),
    schema_metadata="singleton_id",
)
REHEARSAL_PATTERN = r"advisor_rehearsal_[0-9a-f]{32}"


class MigrationError(PersistenceError):
    """Only fixed safe messages or an internally generated cleanup schema may escape."""


def safe_operation(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        try:
            return await function(*args, **kwargs)
        except MigrationError:
            raise
        except Exception:
            raise MigrationError("Migration storage or validation failed.") from None

    return wrapped


def demand(condition, message="Migration state validation failed."):
    if not condition:
        raise MigrationError(message)


def normalize(column, value):
    if value is None:
        return None
    if column in UUID_COLUMNS:
        return str(UUID(str(value)))
    if column in TIMESTAMP_COLUMNS:
        return utc_text(date(value) if isinstance(value, str) else value)
    demand(type(value) in {str, int})
    return value


def normalized_rows(rows):
    demand(set(rows) == set(COLUMNS))
    result = {}
    for table, columns in COLUMNS.items():
        converted = []
        for row in rows[table]:
            demand(set(row) == set(columns))
            converted.append({c: normalize(c, row[c]) for c in columns})
        result[table] = sorted(converted, key=lambda r: r[KEYS[table]])
    return result


def digest(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def typed(value):
    return ["null" if value is None else "int" if type(value) is int else "text", value]


@dataclass(frozen=True)
class MigrationManifest:
    manifest_version: int
    tool_version: int
    schema_version: int
    tables: dict
    current: dict | None
    statuses: dict
    latest_completed: dict | None
    latest_scheduler: dict | None
    running_analyses: int
    running_scheduler: int
    sending_deliveries: int
    relationships_valid: bool = True

    @property
    def cutover_eligible(self):
        return not (self.running_analyses or self.running_scheduler or self.sending_deliveries)

    def render(self, *, outcome="inspected", equal=None):
        return json.dumps(
            {
                **asdict(self),
                "cutover_eligible": self.cutover_eligible,
                "outcome": outcome,
                "equal": equal,
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class MigrationSource:
    rows: dict = field(repr=False)
    manifest: MigrationManifest


def validate_rows(rows):
    demand(rows["schema_metadata"] == [{"singleton_id": 1, "version": 3}])
    for table, values in rows.items():
        demand(len({r[KEYS[table]] for r in values}) == len(values))
    snapshots = {r["snapshot_id"]: snapshot_row(r) for r in rows["portfolio_snapshots"]}
    updates = [update_row(r) for r in rows["confirmed_updates"]]
    by_new = {str(u.new_snapshot_id): u for u in updates}
    demand(len(by_new) == len(updates))
    for update in updates:
        new = snapshots.get(str(update.new_snapshot_id))
        demand(new is not None and new.confirmed_at == update.confirmed_at)
        if update.previous_snapshot_id is not None:
            previous = snapshots.get(str(update.previous_snapshot_id))
            demand(previous is not None and previous.confirmed_at <= update.confirmed_at)
    current = rows["current_portfolio"]
    demand(len(current) <= 1)
    node, revision = None, 0
    if current:
        row = current[0]
        node, revision = row["snapshot_id"], row["revision"]
        demand(row["singleton_id"] == 1 and type(revision) is int and revision > 0)
        demand(node in snapshots and row["updated_at"] == utc_text(snapshots[node].confirmed_at))
    seen = set()
    while node is not None:
        demand(node not in seen and node in by_new)
        seen.add(node)
        previous = by_new[node].previous_snapshot_id
        node = str(previous) if previous else None
    demand(len(seen) == len(updates) == revision)
    runs, results = {}, {}
    for row in rows["analysis_runs"]:
        run = AnalysisRunCodec.from_record(row).run
        demand(row["snapshot_id"] in snapshots)
        demand(run.started_at >= snapshots[row["snapshot_id"]].confirmed_at)
        runs[row["run_id"]] = row
        if row["status"] == "completed":
            demand(row["result_json"] is not None)
            result = AnalysisResultCodec.decode(row["result_json"])
            demand(result.run_id == run.run_id and result.snapshot_id == run.snapshot_id)
            demand(
                result.generated_at == run.completed_at
                and result.market_cutoff == run.market_cutoff
            )
            results[row["run_id"]] = result
    owners = set()
    for row in rows["scheduler_executions"]:
        slot = scheduled_row(row)
        demand(row["owner_token"] not in owners)
        owners.add(row["owner_token"])
        linked = runs.get(row["owner_token"])
        if row["analysis_run_id"] is not None:
            demand(row["analysis_run_id"] in runs)
        if row["status"] == "COMPLETED":
            demand(linked is not None and linked["status"] == "completed")
            demand(results[row["owner_token"]].market_cutoff == slot.expected_cutoff)
        elif row["status"] != "RUNNING" and linked is not None:
            demand(linked["status"] == "failed")
    deliveries = set()
    for row in rows["telegram_deliveries"]:
        delivery_row(row)
        key = (row["analysis_run_id"], row["channel"], row["destination_key"])
        demand(key not in deliveries and row["analysis_run_id"] in results)
        deliveries.add(key)


def build_manifest(raw_rows):
    rows = normalized_rows(raw_rows)
    validate_rows(rows)
    tables = {}
    for table, values in rows.items():
        payloads = []
        for row in values:
            for column in ("payload_json", "result_json"):
                if column in row:
                    value = row[column]
                    payloads.append(
                        [
                            typed(row[KEYS[table]]),
                            column,
                            hashlib.sha256(value.encode("utf-8")).hexdigest()
                            if value is not None
                            else None,
                        ]
                    )
        tables[table] = {
            "count": len(values),
            "primary_keys_sha256": digest([typed(r[KEYS[table]]) for r in values]),
            "rows_sha256": digest([[[c, typed(r[c])] for c in COLUMNS[table]] for r in values]),
            "payloads_sha256": digest(payloads),
        }
    statuses = {
        t: dict(sorted(Counter(r["status"] for r in rows[t]).items()))
        for t in ("analysis_runs", "scheduler_executions", "telegram_deliveries")
    }
    completed = sorted(
        (r for r in rows["analysis_runs"] if r["status"] == "completed"),
        key=lambda r: r["run_id"],
    )
    # Stable tie-breaking without float timestamps losing microsecond precision.
    completed.sort(key=lambda r: date(r["completed_at"]), reverse=True)
    slots = rows["scheduler_executions"]
    current = rows["current_portfolio"]
    return MigrationManifest(
        1,
        1,
        POSTGRES_SCHEMA_VERSION,
        tables,
        {k: current[0][k] for k in ("snapshot_id", "revision")} if current else None,
        statuses,
        {k: completed[0][k] for k in ("run_id", "completed_at")} if completed else None,
        {k: slots[-1][k] for k in ("scheduled_for", "status")} if slots else None,
        statuses["analysis_runs"].get("running", 0),
        statuses["scheduler_executions"].get("RUNNING", 0),
        statuses["telegram_deliveries"].get("SENDING", 0),
    )


@safe_operation
async def load_source(path):
    path = Path(path).expanduser().resolve()
    live_paths = {Path.home() / ".advanced_trading_advisor/advisor.sqlite"}
    if os.environ.get("ADVISOR_DB_PATH"):
        live_paths.add(Path(os.environ["ADVISOR_DB_PATH"]).expanduser())
    demand(
        all(path != p.resolve() and (not p.exists() or not path.samefile(p)) for p in live_paths),
        "Migration requires a backup, not the configured live database.",
    )
    async with readonly(path) as connection:
        await connection.execute("BEGIN")
        demand(await check_schema(connection) == 3, "Migration requires SQLite schema v3.")
        demand([r[0] for r in await many(connection, "PRAGMA integrity_check")] == ["ok"])
        demand(not await many(connection, "PRAGMA foreign_key_check"))
        rows = {
            t: [dict(r) for r in await many(connection, f'SELECT * FROM "{t}"')] for t in COLUMNS
        }
        manifest = build_manifest(rows)
        await connection.rollback()
    return MigrationSource(rows, manifest)


async def target_tables(connection):
    return {
        r["table_name"]
        for r in await connection.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema=current_schema() AND table_type='BASE TABLE'"
        )
    }


async def read_target(connection):
    demand(await target_tables(connection) == set(COLUMNS), "Target schema is incompatible.")
    columns = await connection.fetch(
        "SELECT table_name,column_name FROM information_schema.columns "
        "WHERE table_schema=current_schema()"
    )
    actual = {}
    for row in columns:
        actual.setdefault(row["table_name"], set()).add(row["column_name"])
    demand(actual == POSTGRES_COLUMNS, "Target schema is incompatible.")
    return {t: [dict(r) for r in await connection.fetch(f'SELECT * FROM "{t}"')] for t in COLUMNS}


async def insert_rows(connection, rows):
    for table in TABLES:
        columns = COLUMNS[table]
        sql = (
            f'INSERT INTO "{table}" ('
            + ",".join(f'"{c}"' for c in columns)
            + ") VALUES ("
            + ",".join(f"${i}" for i in range(1, len(columns) + 1))
            + ")"
        )
        for row in rows[table]:
            values = []
            for column in columns:
                value = row[column]
                if value is not None:
                    if column in UUID_COLUMNS:
                        value = UUID(str(value))
                    elif column in TIMESTAMP_COLUMNS:
                        value = date(value) if isinstance(value, str) else value
                values.append(value)
            await connection.execute(sql, *values)


@safe_operation
async def import_source(source, database, *, rehearsal=False):
    demand(build_manifest(source.rows) == source.manifest)
    demand(
        re.fullmatch(REHEARSAL_PATTERN, database.schema) is not None
        if rehearsal
        else database.schema == "public",
        "Import target schema is not allowed.",
    )
    if not rehearsal:
        demand(
            source.manifest.cutover_eligible, "Source has RUNNING or SENDING work; cutover refused."
        )
    async with database.transaction() as connection:
        # Same order for all importers; operational writers take at most one of these locks.
        for lock in (SCHEMA_LOCK, PORTFOLIO_LOCK, ANALYSIS_LOCK, SCHEDULER_LOCK, DELIVERY_LOCK):
            await connection.execute("SELECT pg_advisory_xact_lock($1)", lock)
        tables = await target_tables(connection)
        initialized = not tables
        if initialized:
            for migration in POSTGRES_MIGRATIONS.values():
                for statement in migration:
                    await connection.execute(statement)
            await connection.execute("INSERT INTO schema_metadata VALUES (1,3)")
        demand(await target_tables(connection) == set(COLUMNS), "Target schema is incompatible.")
        await connection.execute(
            "LOCK TABLE " + ",".join(f'"{t}"' for t in COLUMNS) + " IN ACCESS EXCLUSIVE MODE"
        )
        target = await read_target(connection)
        existing = build_manifest(target)
        if existing == source.manifest and normalized_rows(target) == normalized_rows(source.rows):
            return "imported" if initialized else "verified_noop"
        demand(
            all(not target[t] for t in TABLES), "Target contains different state; import refused."
        )
        await insert_rows(connection, source.rows)
        imported = await read_target(connection)
        demand(
            build_manifest(imported) == source.manifest
            and normalized_rows(imported) == normalized_rows(source.rows),
            "Imported state differs; transaction rolled back.",
        )
    return "imported"


@safe_operation
async def verify_source(source, database):
    demand(build_manifest(source.rows) == source.manifest)
    async with database.connection() as connection:
        async with connection.transaction(isolation="repeatable_read", readonly=True):
            rows = await read_target(connection)
            demand(
                build_manifest(rows) == source.manifest
                and normalized_rows(rows) == normalized_rows(source.rows),
                "Source and target differ.",
            )
    return "verified"


@safe_operation
async def rehearse(source, database_url):
    schema = "advisor_rehearsal_" + uuid4().hex
    demand(re.fullmatch(REHEARSAL_PATTERN, schema) is not None)
    # Bootstrap namespace is pg_catalog: the admin never reads or initializes public.
    admin = PostgresDatabase(database_url, schema="pg_catalog")
    created = False
    try:
        try:
            async with admin.connection() as connection:
                await connection.execute(f'CREATE SCHEMA "{schema}"')
                created = True
        except Exception:
            # A lost CREATE response may have left a schema. Never assume ownership and drop
            # after an unacknowledged creation; report only the generated name for inspection.
            raise MigrationError(
                f"Rehearsal creation unconfirmed; inspect schema: {schema}"
            ) from None
        target = PostgresDatabase(database_url, schema=schema)
        await import_source(source, target, rehearsal=True)
        await verify_source(source, target)
    finally:
        if created:
            try:
                async with admin.connection() as connection:
                    await connection.execute(f'DROP SCHEMA "{schema}" CASCADE')
            except Exception:
                raise MigrationError(f"Rehearsal cleanup failed; schema: {schema}") from None
    return "rehearsed"
