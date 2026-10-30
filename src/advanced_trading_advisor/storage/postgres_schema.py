"""Versioned PostgreSQL schema; v4 adds the independent financial ledger."""

POSTGRES_SCHEMA_VERSION = 4

POSTGRES_COLUMNS = {
    "schema_metadata": {"singleton_id", "version"},
    "financial_portfolios": {"id", "last_sequence"},
    "financial_operations": {
        "portfolio_id",
        "operation_id",
        "sequence",
        "operation_type",
        "executed_at",
        "recorded_at",
        "asset",
        "quantity",
        "known_cost_usdt",
        "incomplete_quantity",
        "completeness",
        "principal_usdt",
        "proceeds_usdt",
    },
    "financial_postings": {"portfolio_id", "operation_id", "ordinal", "asset", "delta", "role"},
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

FINANCIAL_TABLES = frozenset({"financial_portfolios", "financial_operations", "financial_postings"})
POSTGRES_LEGACY_COLUMNS = {
    table: columns for table, columns in POSTGRES_COLUMNS.items() if table not in FINANCIAL_TABLES
}
FINANCIAL_REQUIRED_TRIGGERS = {
    "financial_operations": frozenset(
        {
            "financial_operation_postings_complete",
            "financial_operations_no_update",
            "financial_operations_no_delete",
            "financial_operations_no_truncate",
        }
    ),
    "financial_postings": frozenset(
        {
            "financial_posting_complete",
            "financial_postings_no_update",
            "financial_postings_no_delete",
            "financial_postings_no_truncate",
        }
    ),
}

# tgtype bits: ROW=1, BEFORE=2, INSERT=4, DELETE=8, UPDATE=16, TRUNCATE=32.
# Each tuple is (event/timing/granularity, function, constraint, deferrable, initially deferred).
FINANCIAL_TRIGGER_CONTRACT = {
    ("financial_operations", "financial_operation_postings_complete"): (
        5,
        "financial_postings_complete",
        True,
        True,
        True,
    ),
    ("financial_postings", "financial_posting_complete"): (
        5,
        "financial_postings_complete",
        True,
        True,
        True,
    ),
    **{
        (table, f"{table}_no_{action}"): (
            trigger_type,
            "advisor_immutable_record",
            False,
            False,
            False,
        )
        for table in ("financial_operations", "financial_postings")
        for action, trigger_type in (("update", 19), ("delete", 11), ("truncate", 34))
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
    4: (
        """CREATE FUNCTION financial_decimal_valid(value NUMERIC) RETURNS BOOLEAN
            LANGUAGE SQL IMMUTABLE STRICT AS $$
            SELECT value <> 'NaN'::NUMERIC
               AND value <> 'Infinity'::NUMERIC
               AND value <> '-Infinity'::NUMERIC
               AND abs(value) < 1e60::NUMERIC
               AND scale(value) <= 24
               AND length(regexp_replace(trim_scale(abs(value))::TEXT, '[^0-9]', '', 'g')) <= 60
            $$""",
        """CREATE TABLE financial_portfolios (
            id UUID PRIMARY KEY CHECK (id <> '00000000-0000-0000-0000-000000000000'::UUID),
            last_sequence BIGINT NOT NULL DEFAULT 0 CHECK (last_sequence >= 0))""",
        """CREATE TABLE financial_operations (
            portfolio_id UUID NOT NULL REFERENCES financial_portfolios(id),
            operation_id UUID NOT NULL
                CHECK (operation_id <> '00000000-0000-0000-0000-000000000000'::UUID),
            sequence BIGINT NOT NULL CHECK (sequence > 0),
            operation_type TEXT NOT NULL
                CHECK (operation_type IN ('opening_balance', 'buy', 'sell')),
            executed_at TIMESTAMPTZ NOT NULL,
            recorded_at TIMESTAMPTZ NOT NULL
                CHECK (isfinite(executed_at) AND isfinite(recorded_at)
                    AND recorded_at >= executed_at),
            asset TEXT NOT NULL CHECK (asset COLLATE "C" ~ '^[A-Z0-9][A-Z0-9._-]{0,19}$'),
            quantity NUMERIC NOT NULL
                CHECK (financial_decimal_valid(quantity) AND quantity > 0),
            known_cost_usdt NUMERIC
                CHECK (known_cost_usdt IS NULL OR
                    (financial_decimal_valid(known_cost_usdt) AND known_cost_usdt >= 0)),
            incomplete_quantity NUMERIC
                CHECK (incomplete_quantity IS NULL OR
                    (financial_decimal_valid(incomplete_quantity) AND incomplete_quantity >= 0)),
            completeness TEXT CHECK (completeness IN ('complete', 'partial', 'unknown')),
            principal_usdt NUMERIC
                CHECK (principal_usdt IS NULL OR
                    (financial_decimal_valid(principal_usdt) AND principal_usdt > 0)),
            proceeds_usdt NUMERIC
                CHECK (proceeds_usdt IS NULL OR
                    (financial_decimal_valid(proceeds_usdt) AND proceeds_usdt > 0)),
            PRIMARY KEY (portfolio_id, operation_id),
            UNIQUE (portfolio_id, sequence),
            CHECK (
                (operation_type = 'opening_balance'
                    AND principal_usdt IS NULL AND proceeds_usdt IS NULL
                    AND incomplete_quantity IS NOT NULL AND completeness IS NOT NULL
                    AND incomplete_quantity <= quantity
                    AND (
                        (completeness = 'complete' AND known_cost_usdt IS NOT NULL
                            AND incomplete_quantity = 0)
                        OR (completeness = 'unknown' AND known_cost_usdt IS NULL
                            AND incomplete_quantity = quantity)
                        OR (completeness = 'partial' AND known_cost_usdt IS NOT NULL
                            AND incomplete_quantity > 0 AND incomplete_quantity < quantity)
                    )
                    AND (asset <> 'USDT' OR
                        (completeness = 'complete' AND known_cost_usdt = quantity)))
                OR (operation_type = 'buy' AND asset <> 'USDT'
                    AND principal_usdt IS NOT NULL AND proceeds_usdt IS NULL
                    AND known_cost_usdt IS NULL AND incomplete_quantity IS NULL
                    AND completeness IS NULL)
                OR (operation_type = 'sell' AND asset <> 'USDT'
                    AND proceeds_usdt IS NOT NULL AND principal_usdt IS NULL
                    AND known_cost_usdt IS NULL AND incomplete_quantity IS NULL
                    AND completeness IS NULL)
            ))""",
        """CREATE INDEX financial_operations_replay_order
            ON financial_operations(portfolio_id, executed_at, sequence)""",
        """CREATE TABLE financial_postings (
            portfolio_id UUID NOT NULL,
            operation_id UUID NOT NULL,
            ordinal INTEGER NOT NULL CHECK (ordinal > 0),
            asset TEXT NOT NULL CHECK (asset COLLATE "C" ~ '^[A-Z0-9][A-Z0-9._-]{0,19}$'),
            delta NUMERIC NOT NULL
                CHECK (financial_decimal_valid(delta) AND delta <> 0),
            role TEXT NOT NULL CHECK (role IN ('opening', 'traded_asset', 'settlement')),
            PRIMARY KEY (portfolio_id, operation_id, ordinal),
            FOREIGN KEY (portfolio_id, operation_id)
                REFERENCES financial_operations(portfolio_id, operation_id))""",
        """CREATE FUNCTION financial_postings_complete() RETURNS TRIGGER
            LANGUAGE plpgsql AS $$
            DECLARE operation_row financial_operations%ROWTYPE;
                    actual_count INTEGER;
            BEGIN
                SELECT * INTO operation_row FROM financial_operations
                WHERE portfolio_id = NEW.portfolio_id AND operation_id = NEW.operation_id;
                SELECT count(*) INTO actual_count FROM financial_postings
                WHERE portfolio_id = NEW.portfolio_id AND operation_id = NEW.operation_id;
                IF operation_row.operation_type = 'opening_balance' THEN
                    IF actual_count <> 1 OR NOT EXISTS (
                        SELECT 1 FROM financial_postings WHERE portfolio_id = NEW.portfolio_id
                        AND operation_id = NEW.operation_id AND ordinal = 1
                        AND asset = operation_row.asset AND delta = operation_row.quantity
                        AND role = 'opening') THEN
                        RAISE EXCEPTION 'Invalid financial postings';
                    END IF;
                ELSIF actual_count <> 2 OR NOT EXISTS (
                    SELECT 1 FROM financial_postings WHERE portfolio_id = NEW.portfolio_id
                    AND operation_id = NEW.operation_id AND ordinal = 1
                    AND asset = operation_row.asset AND role = 'traded_asset'
                    AND delta = CASE WHEN operation_row.operation_type = 'buy'
                        THEN operation_row.quantity ELSE -operation_row.quantity END
                ) OR NOT EXISTS (
                    SELECT 1 FROM financial_postings WHERE portfolio_id = NEW.portfolio_id
                    AND operation_id = NEW.operation_id AND ordinal = 2
                    AND asset = 'USDT' AND role = 'settlement'
                    AND delta = CASE WHEN operation_row.operation_type = 'buy'
                        THEN -operation_row.principal_usdt ELSE operation_row.proceeds_usdt END
                ) THEN
                    RAISE EXCEPTION 'Invalid financial postings';
                END IF;
                RETURN NEW;
            END $$""",
        """CREATE CONSTRAINT TRIGGER financial_operation_postings_complete
            AFTER INSERT ON financial_operations DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION financial_postings_complete()""",
        """CREATE CONSTRAINT TRIGGER financial_posting_complete
            AFTER INSERT ON financial_postings DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION financial_postings_complete()""",
        """CREATE TRIGGER financial_operations_no_update BEFORE UPDATE ON financial_operations
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER financial_operations_no_delete BEFORE DELETE ON financial_operations
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER financial_operations_no_truncate BEFORE TRUNCATE ON financial_operations
            FOR EACH STATEMENT EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER financial_postings_no_update BEFORE UPDATE ON financial_postings
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER financial_postings_no_delete BEFORE DELETE ON financial_postings
            FOR EACH ROW EXECUTE FUNCTION advisor_immutable_record()""",
        """CREATE TRIGGER financial_postings_no_truncate BEFORE TRUNCATE ON financial_postings
            FOR EACH STATEMENT EXECUTE FUNCTION advisor_immutable_record()""",
    ),
}


def _function_source(version: int, signature: str) -> str:
    statement = next(
        sql
        for sql in POSTGRES_MIGRATIONS[version]
        if sql.startswith(f"CREATE FUNCTION {signature} RETURNS ")
    )
    return " ".join(statement.split("AS $$", 1)[1].rsplit("$$", 1)[0].split())


# Compare catalog source to the applied migration, ignoring irrelevant whitespace.
FINANCIAL_FUNCTION_CONTRACT = {
    "advisor_immutable_record": (
        "plpgsql",
        "trigger",
        "v",
        False,
        _function_source(1, "advisor_immutable_record()"),
    ),
    "financial_decimal_valid": (
        "sql",
        "boolean",
        "i",
        True,
        _function_source(4, "financial_decimal_valid(value NUMERIC)"),
    ),
    "financial_postings_complete": (
        "plpgsql",
        "trigger",
        "v",
        False,
        _function_source(4, "financial_postings_complete()"),
    ),
}
