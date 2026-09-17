SCHEMA_VERSION = 3

STATEMENTS = (
    """CREATE TABLE schema_metadata (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        version INTEGER NOT NULL CHECK(version > 0))""",
    """CREATE TABLE portfolio_snapshots (
        snapshot_id TEXT PRIMARY KEY NOT NULL,
        confirmed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        schema_version INTEGER NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE current_portfolio (
        singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
        snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshots(snapshot_id),
        revision INTEGER NOT NULL CHECK(revision > 0),
        updated_at TEXT NOT NULL)""",
    """CREATE TABLE confirmed_updates (
        update_id TEXT PRIMARY KEY NOT NULL,
        update_type TEXT NOT NULL,
        previous_snapshot_id TEXT REFERENCES portfolio_snapshots(snapshot_id),
        new_snapshot_id TEXT NOT NULL UNIQUE REFERENCES portfolio_snapshots(snapshot_id),
        confirmed_at TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        schema_version INTEGER NOT NULL)""",
    """CREATE TABLE analysis_runs (
        run_id TEXT PRIMARY KEY NOT NULL,
        snapshot_id TEXT NOT NULL REFERENCES portfolio_snapshots(snapshot_id),
        status TEXT NOT NULL CHECK(status IN ('running','completed','failed','cancelled')),
        started_at TEXT NOT NULL,
        completed_at TEXT,
        market_cutoff TEXT,
        result_json TEXT,
        failure_category TEXT,
        schema_version INTEGER NOT NULL,
        CHECK(run_id != snapshot_id),
        CHECK((status = 'running' AND completed_at IS NULL) OR
              (status != 'running' AND completed_at IS NOT NULL)),
        CHECK(result_json IS NULL OR status = 'completed'),
        CHECK((status = 'failed' AND failure_category IS NOT NULL) OR
              (status != 'failed' AND failure_category IS NULL)))""",
    "CREATE INDEX analysis_runs_by_snapshot ON analysis_runs(snapshot_id, started_at)",
    """CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON portfolio_snapshots
       BEGIN SELECT RAISE(ABORT, 'Snapshots are immutable'); END""",
    """CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON portfolio_snapshots
       BEGIN SELECT RAISE(ABORT, 'Snapshots are immutable'); END""",
    """CREATE TRIGGER updates_no_update BEFORE UPDATE ON confirmed_updates
       BEGIN SELECT RAISE(ABORT, 'Audit records are immutable'); END""",
    """CREATE TRIGGER updates_no_delete BEFORE DELETE ON confirmed_updates
       BEGIN SELECT RAISE(ABORT, 'Audit records are immutable'); END""",
)

COLUMNS = {
    "schema_metadata": {"singleton_id", "version"},
    "portfolio_snapshots": {
        "snapshot_id",
        "confirmed_at",
        "payload_json",
        "schema_version",
        "created_at",
    },
    "current_portfolio": {"singleton_id", "snapshot_id", "revision", "updated_at"},
    "confirmed_updates": {
        "update_id",
        "update_type",
        "previous_snapshot_id",
        "new_snapshot_id",
        "confirmed_at",
        "payload_json",
        "schema_version",
    },
    "analysis_runs": {
        "run_id",
        "snapshot_id",
        "status",
        "started_at",
        "completed_at",
        "market_cutoff",
        "result_json",
        "failure_category",
        "schema_version",
    },
}

LEGACY_COLUMNS = dict(COLUMNS)
SCHEDULER_STATEMENTS = (
    """CREATE TABLE scheduler_executions (
        scheduled_for TEXT PRIMARY KEY NOT NULL,
        expected_cutoff TEXT NOT NULL,
        owner_token TEXT NOT NULL UNIQUE,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED','SKIPPED')),
        analysis_run_id TEXT REFERENCES analysis_runs(run_id),
        failure_category TEXT,
        CHECK(analysis_run_id IS NULL OR analysis_run_id=owner_token),
        CHECK((status='RUNNING' AND finished_at IS NULL AND failure_category IS NULL) OR
              (status='COMPLETED' AND finished_at IS NOT NULL AND analysis_run_id IS NOT NULL
               AND failure_category IS NULL) OR
              (status IN ('FAILED','SKIPPED') AND finished_at IS NOT NULL
               AND failure_category IS NOT NULL)))""",
    """CREATE TRIGGER scheduler_terminal_no_update BEFORE UPDATE ON scheduler_executions
       WHEN OLD.status != 'RUNNING'
       BEGIN SELECT RAISE(ABORT, 'Terminal scheduler records are immutable'); END""",
    """CREATE TRIGGER scheduler_no_delete BEFORE DELETE ON scheduler_executions
       BEGIN SELECT RAISE(ABORT, 'Scheduler records are auditable'); END""",
    """CREATE TRIGGER scheduler_fence_analysis BEFORE INSERT ON analysis_runs
       WHEN EXISTS(SELECT 1 FROM scheduler_executions
                   WHERE owner_token=NEW.run_id AND status!='RUNNING')
       BEGIN SELECT RAISE(ABORT, 'Scheduled owner expired'); END""",
)
STATEMENTS += SCHEDULER_STATEMENTS
COLUMNS["scheduler_executions"] = {
    "scheduled_for",
    "expected_cutoff",
    "owner_token",
    "started_at",
    "finished_at",
    "status",
    "analysis_run_id",
    "failure_category",
}

SCHEDULER_COLUMNS = dict(COLUMNS)
DELIVERY_STATEMENTS = (
    """CREATE TABLE telegram_deliveries (
        delivery_id TEXT PRIMARY KEY NOT NULL,
        analysis_run_id TEXT NOT NULL REFERENCES analysis_runs(run_id),
        channel TEXT NOT NULL CHECK(channel='TELEGRAM'),
        destination_key TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        finished_at TEXT,
        status TEXT NOT NULL CHECK(status IN
            ('PENDING','SENDING','DELIVERED','FAILED','AMBIGUOUS')),
        attempt_count INTEGER NOT NULL CHECK(attempt_count >= 0),
        failure_category TEXT,
        UNIQUE(analysis_run_id, channel, destination_key),
        CHECK((status='PENDING' AND started_at IS NULL AND finished_at IS NULL
               AND attempt_count=0 AND failure_category IS NULL) OR
              (status='SENDING' AND started_at IS NOT NULL AND finished_at IS NULL
               AND failure_category IS NULL) OR
              (status='DELIVERED' AND started_at IS NOT NULL AND finished_at IS NOT NULL
               AND attempt_count>0 AND failure_category IS NULL) OR
              (status IN ('FAILED','AMBIGUOUS') AND started_at IS NOT NULL
               AND finished_at IS NOT NULL AND attempt_count>0
               AND failure_category IS NOT NULL)))""",
    """CREATE TRIGGER telegram_only_completed BEFORE INSERT ON telegram_deliveries
       WHEN NOT EXISTS(SELECT 1 FROM analysis_runs
                       WHERE run_id=NEW.analysis_run_id AND status='completed'
                         AND result_json IS NOT NULL)
       BEGIN SELECT RAISE(ABORT, 'Only completed analyses can be delivered'); END""",
    """CREATE TRIGGER telegram_transition_guard BEFORE UPDATE ON telegram_deliveries
       WHEN NOT ((OLD.status='PENDING' AND NEW.status='SENDING') OR
                 (OLD.status='SENDING' AND NEW.status IN
                     ('PENDING','SENDING','DELIVERED','FAILED','AMBIGUOUS')) OR
                 (OLD.status='FAILED' AND NEW.status='SENDING'))
       BEGIN SELECT RAISE(ABORT, 'Invalid delivery transition'); END""",
    """CREATE TRIGGER telegram_no_delete BEFORE DELETE ON telegram_deliveries
       BEGIN SELECT RAISE(ABORT, 'Delivery records are auditable'); END""",
)
STATEMENTS += DELIVERY_STATEMENTS
COLUMNS["telegram_deliveries"] = {
    "delivery_id",
    "analysis_run_id",
    "channel",
    "destination_key",
    "created_at",
    "started_at",
    "finished_at",
    "status",
    "attempt_count",
    "failure_category",
}
