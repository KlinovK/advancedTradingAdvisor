"""Versioned PostgreSQL schema equivalent to SQLite schema v3."""

POSTGRES_SCHEMA_VERSION = 3

POSTGRES_COLUMNS = {
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
    "scheduler_executions": {
        "scheduled_for",
        "expected_cutoff",
        "owner_token",
        "started_at",
        "finished_at",
        "status",
        "analysis_run_id",
        "failure_category",
    },
    "telegram_deliveries": {
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
    },
}

POSTGRES_MIGRATIONS = {
    1: (
        """CREATE TABLE schema_metadata (
            singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
            version INTEGER NOT NULL CHECK (version > 0))""",
        """CREATE TABLE portfolio_snapshots (
            snapshot_id UUID PRIMARY KEY,
            confirmed_at TIMESTAMPTZ NOT NULL,
            payload_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL)""",
        """CREATE TABLE current_portfolio (
            singleton_id SMALLINT PRIMARY KEY CHECK (singleton_id = 1),
            snapshot_id UUID NOT NULL REFERENCES portfolio_snapshots(snapshot_id),
            revision BIGINT NOT NULL CHECK (revision > 0),
            updated_at TIMESTAMPTZ NOT NULL)""",
        """CREATE TABLE confirmed_updates (
            update_id UUID PRIMARY KEY,
            update_type TEXT NOT NULL,
            previous_snapshot_id UUID REFERENCES portfolio_snapshots(snapshot_id),
            new_snapshot_id UUID NOT NULL UNIQUE REFERENCES portfolio_snapshots(snapshot_id),
            confirmed_at TIMESTAMPTZ NOT NULL,
            payload_json TEXT NOT NULL,
            schema_version INTEGER NOT NULL)""",
        """CREATE TABLE analysis_runs (
            run_id UUID PRIMARY KEY,
            snapshot_id UUID NOT NULL REFERENCES portfolio_snapshots(snapshot_id),
            status TEXT NOT NULL CHECK (status IN ('running','completed','failed','cancelled')),
            started_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            market_cutoff TIMESTAMPTZ,
            result_json TEXT,
            failure_category TEXT,
            schema_version INTEGER NOT NULL,
            CHECK (run_id <> snapshot_id),
            CHECK ((status = 'running' AND completed_at IS NULL) OR
                   (status <> 'running' AND completed_at IS NOT NULL)),
            CHECK (completed_at IS NULL OR completed_at >= started_at),
            CHECK (result_json IS NULL OR status = 'completed'),
            CHECK ((status = 'failed' AND failure_category IS NOT NULL) OR
                   (status <> 'failed' AND failure_category IS NULL)))""",
        "CREATE INDEX analysis_runs_by_snapshot ON analysis_runs(snapshot_id, started_at)",
        """CREATE UNIQUE INDEX one_running_analysis
            ON analysis_runs ((status)) WHERE status = 'running'""",
        """CREATE FUNCTION advisor_immutable_record() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'Immutable advisor record'; END $$""",
        """CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON portfolio_snapshots
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON portfolio_snapshots
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER updates_no_update BEFORE UPDATE ON confirmed_updates
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER updates_no_delete BEFORE DELETE ON confirmed_updates
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE FUNCTION advisor_analysis_terminal_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF OLD.status <> 'running' THEN
                    RAISE EXCEPTION 'Terminal analysis runs are immutable';
                END IF;
                IF TG_OP = 'DELETE' THEN
                    RETURN OLD;
                END IF;
                RETURN NEW;
            END $$""",
        """CREATE TRIGGER runs_terminal_no_update BEFORE UPDATE ON analysis_runs
            FOR EACH ROW EXECUTE FUNCTION advisor_analysis_terminal_guard()""",
        """CREATE TRIGGER runs_terminal_no_delete BEFORE DELETE ON analysis_runs
            FOR EACH ROW EXECUTE FUNCTION advisor_analysis_terminal_guard()""",
    ),
    2: (
        """CREATE TABLE scheduler_executions (
            scheduled_for TIMESTAMPTZ PRIMARY KEY,
            expected_cutoff TIMESTAMPTZ NOT NULL,
            owner_token UUID NOT NULL UNIQUE,
            started_at TIMESTAMPTZ NOT NULL,
            finished_at TIMESTAMPTZ,
            status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETED','FAILED','SKIPPED')),
            analysis_run_id UUID REFERENCES analysis_runs(run_id),
            failure_category TEXT,
            CHECK (analysis_run_id IS NULL OR analysis_run_id = owner_token),
            CHECK ((status = 'RUNNING' AND finished_at IS NULL AND failure_category IS NULL) OR
                   (status = 'COMPLETED' AND finished_at IS NOT NULL
                    AND analysis_run_id IS NOT NULL AND failure_category IS NULL) OR
                   (status IN ('FAILED','SKIPPED') AND finished_at IS NOT NULL
                    AND failure_category IS NOT NULL)))""",
        """CREATE FUNCTION advisor_scheduler_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF TG_OP = 'DELETE' OR OLD.status <> 'RUNNING' THEN
                    RAISE EXCEPTION 'Scheduler records are immutable';
                END IF;
                RETURN NEW;
            END $$""",
        """CREATE TRIGGER scheduler_terminal_no_update BEFORE UPDATE ON scheduler_executions
            FOR EACH ROW EXECUTE FUNCTION advisor_scheduler_guard()""",
        """CREATE TRIGGER scheduler_no_delete BEFORE DELETE ON scheduler_executions
            FOR EACH ROW EXECUTE FUNCTION advisor_scheduler_guard()""",
        """CREATE FUNCTION advisor_scheduler_fence_analysis() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF EXISTS (SELECT 1 FROM scheduler_executions
                           WHERE owner_token = NEW.run_id AND status <> 'RUNNING') THEN
                    RAISE EXCEPTION 'Scheduled owner expired';
                END IF;
                RETURN NEW;
            END $$""",
        """CREATE TRIGGER scheduler_fence_analysis BEFORE INSERT ON analysis_runs
            FOR EACH ROW EXECUTE FUNCTION advisor_scheduler_fence_analysis()""",
    ),
    3: (
        """CREATE TABLE telegram_deliveries (
            delivery_id UUID PRIMARY KEY,
            analysis_run_id UUID NOT NULL REFERENCES analysis_runs(run_id),
            channel TEXT NOT NULL CHECK (channel = 'TELEGRAM'),
            destination_key TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            started_at TIMESTAMPTZ,
            finished_at TIMESTAMPTZ,
            status TEXT NOT NULL CHECK (status IN
                ('PENDING','SENDING','DELIVERED','FAILED','AMBIGUOUS')),
            attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
            failure_category TEXT,
            UNIQUE (analysis_run_id, channel, destination_key),
            CHECK ((status = 'PENDING' AND started_at IS NULL AND finished_at IS NULL
                    AND attempt_count = 0 AND failure_category IS NULL) OR
                   (status = 'SENDING' AND started_at IS NOT NULL AND finished_at IS NULL
                    AND failure_category IS NULL) OR
                   (status = 'DELIVERED' AND started_at IS NOT NULL AND finished_at IS NOT NULL
                    AND attempt_count > 0 AND failure_category IS NULL) OR
                   (status IN ('FAILED','AMBIGUOUS') AND started_at IS NOT NULL
                    AND finished_at IS NOT NULL AND attempt_count > 0
                    AND failure_category IS NOT NULL)))""",
        """CREATE FUNCTION advisor_telegram_insert_guard() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM analysis_runs WHERE run_id = NEW.analysis_run_id
                               AND status = 'completed' AND result_json IS NOT NULL) THEN
                    RAISE EXCEPTION 'Only completed analyses can be delivered';
                END IF;
                RETURN NEW;
            END $$""",
        """CREATE TRIGGER telegram_only_completed BEFORE INSERT ON telegram_deliveries
            FOR EACH ROW EXECUTE FUNCTION advisor_telegram_insert_guard()""",
        """CREATE FUNCTION advisor_telegram_transition_guard() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                IF NOT ((OLD.status = 'PENDING' AND NEW.status = 'SENDING') OR
                        (OLD.status = 'SENDING' AND NEW.status IN
                            ('PENDING','SENDING','DELIVERED','FAILED','AMBIGUOUS')) OR
                        (OLD.status = 'FAILED' AND NEW.status = 'SENDING')) THEN
                    RAISE EXCEPTION 'Invalid delivery transition';
                END IF;
                RETURN NEW;
            END $$""",
        """CREATE TRIGGER telegram_transition_guard BEFORE UPDATE ON telegram_deliveries
            FOR EACH ROW EXECUTE FUNCTION advisor_telegram_transition_guard()""",
        """CREATE TRIGGER telegram_no_delete BEFORE DELETE ON telegram_deliveries
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
    ),
}
