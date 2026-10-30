"""Synthetic backups only; no default database or external providers."""

import asyncio
import json
import sqlite3
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from test_active_phase9 import daily_asset
from test_portfolio_risk import CUTOFF, NOW, asset, market, state

from advanced_trading_advisor.analysis.use_case import AnalyzeCurrentPortfolio
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.storage import migration
from advanced_trading_advisor.storage.codecs import utc_text
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.maintenance import backup_database
from advanced_trading_advisor.storage.migration import (
    MigrationError,
    MigrationSource,
    build_manifest,
    digest,
    import_source,
    load_source,
    normalize,
    typed,
)
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)


async def synthetic_backup(directory):
    database = Database(directory / "synthetic-source.sqlite")
    await database.initialize()
    snapshots = SQLiteSnapshotRepository(database)
    runs = SQLiteAnalysisRunRepository(database)
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    await ConfirmedStateService(snapshots).initialize(snapshot)
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))

    class SyntheticProvider:
        async def get_market_snapshot(self, _symbols):
            return evidence

    result = await AnalyzeCurrentPortfolio(
        snapshots, runs, SyntheticProvider, now=lambda: NOW
    ).execute()
    assert result.active.setups[0].trigger.transitions
    # Historical text with different whitespace must be copied, not regenerated.
    stored = await runs.latest_completed()
    original = json.loads(stored.result_json)
    legacy = deepcopy(original)
    legacy["schema_version"] = 3
    legacy["data"].pop("active")
    for recommendation in legacy["data"]["recommendations"]:
        recommendation.pop("setup_id")
    for value in legacy["data"]["analyzed_assets"]:
        value.pop("daily_context")
        value.pop("closed_1h_candles")
    for key in ("active_policy_parameters", "reserve_policy_parameters"):
        legacy["data"]["configuration"].pop(key)
    for key in (
        "hard_expense_reserve_usd",
        "market_buffer_usd",
        "potential_trading_liquidity_usd",
        "strategy_dry_powder_usd",
        "recommended_deployment_usd",
    ):
        legacy["data"]["financial"].pop(key)
    legacy_id = uuid4()
    legacy["data"]["run_id"] = str(legacy_id)
    failed_id = uuid4()
    async with database.transaction() as c:
        await c.execute(
            "INSERT INTO analysis_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(legacy_id),
                str(snapshot.id),
                "completed",
                utc_text(NOW),
                utc_text(NOW),
                utc_text(CUTOFF),
                json.dumps(legacy, indent=2),
                None,
                1,
            ),
        )
        await c.execute(
            "INSERT INTO analysis_runs VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(failed_id),
                str(snapshot.id),
                "failed",
                utc_text(NOW),
                utc_text(NOW),
                None,
                None,
                "ai_provider",
                1,
            ),
        )
        await c.execute(
            "INSERT INTO scheduler_executions VALUES (?,?,?,?,?,?,?,?)",
            (
                utc_text(CUTOFF + timedelta(minutes=10)),
                utc_text(CUTOFF),
                str(result.run_id),
                utc_text(NOW),
                utc_text(NOW),
                "COMPLETED",
                str(result.run_id),
                None,
            ),
        )
        await c.execute(
            "INSERT INTO telegram_deliveries VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                str(result.run_id),
                "TELEGRAM",
                "a" * 64,
                utc_text(NOW),
                utc_text(NOW),
                utc_text(NOW),
                "DELIVERED",
                2,
                None,
            ),
        )
    backup, _ = await backup_database(database.path, directory / "backup.sqlite", now=NOW)
    return backup


@pytest.fixture
def backup(tmp_path):
    return asyncio.run(synthetic_backup(tmp_path))


def test_manifest_determinism_exact_text_legacy_and_trigger_preservation(backup):
    source = asyncio.run(load_source(backup))
    assert source.manifest.schema_version == 3
    assert set(source.rows) == set(migration.COLUMNS)
    assert set(source.rows).isdisjoint(migration.FINANCIAL_TABLES)
    shuffled = {t: list(reversed(rows)) for t, rows in source.rows.items()}
    assert build_manifest(shuffled) == source.manifest
    with sqlite3.connect(f"file:{backup}?mode=ro", uri=True) as connection:
        payloads = dict(connection.execute("SELECT run_id,result_json FROM analysis_runs"))
    assert all(
        row["result_json"] == payloads[row["run_id"]] for row in source.rows["analysis_runs"]
    )
    versions = {json.loads(p)["schema_version"] for p in payloads.values() if p}
    assert versions == {3, 5}
    current = next(
        json.loads(p) for p in payloads.values() if p and json.loads(p)["schema_version"] == 5
    )
    assert current["data"]["active"]["setups"][0]["trigger"]["transitions"]
    modified = deepcopy(source.rows)
    next(r for r in modified["analysis_runs"] if r["result_json"])["result_json"] += " "
    assert build_manifest(modified) != source.manifest
    report = source.manifest.render()
    assert "a" * 64 not in report
    assert "result_json" not in report and "recommendations" not in report
    assert all(p not in report for p in payloads.values() if p)
    assert source.manifest.cutover_eligible


def test_normalization_and_typed_digests():
    value = uuid4()
    assert normalize("snapshot_id", str(value).upper()) == normalize("snapshot_id", value)
    assert normalize("started_at", "2026-09-17T15:00:00.123456+03:00") == (
        "2026-09-17T12:00:00.123456+00:00"
    )
    assert len({digest(typed(v)) for v in (None, "", "null", 0, "0")}) == 5


@pytest.mark.parametrize("damage", ["corrupt", "version", "audit"])
def test_invalid_source_rejected_without_raw_errors(backup, damage):
    if damage == "corrupt":
        backup.write_bytes(b"password-sentinel invalid SQLite")
    else:
        with sqlite3.connect(backup) as c:
            c.execute(
                "UPDATE schema_metadata SET version=99"
                if damage == "version"
                else "UPDATE current_portfolio SET revision=99"
            )
    with pytest.raises(MigrationError) as error:
        asyncio.run(load_source(backup))
    assert "password-sentinel" not in str(error.value)
    assert str(backup) not in str(error.value)


@pytest.mark.parametrize("kind", ["analysis", "scheduler", "delivery"])
def test_inflight_state_reported_but_final_import_refused(backup, kind):
    source = asyncio.run(load_source(backup))
    rows = deepcopy(source.rows)
    if kind == "analysis":
        row = deepcopy(rows["analysis_runs"][0])
        row.update(
            run_id=str(uuid4()),
            status="running",
            completed_at=None,
            result_json=None,
            failure_category=None,
            market_cutoff=None,
        )
        rows["analysis_runs"].append(row)
    elif kind == "scheduler":
        row = rows["scheduler_executions"][0]
        row.update(status="RUNNING", finished_at=None, analysis_run_id=None)
    else:
        row = rows["telegram_deliveries"][0]
        row.update(status="SENDING", finished_at=None)
    source = MigrationSource(rows, build_manifest(rows))
    assert not source.manifest.cutover_eligible

    class MustNotConnect:
        schema = "public"

        def transaction(self):
            raise AssertionError("Ineligible source must fail before connecting")

    with pytest.raises(MigrationError, match="cutover refused"):
        asyncio.run(import_source(source, MustNotConnect()))


def test_inspect_dispatch_does_not_compose_runtime_or_connect(backup, monkeypatch):
    monkeypatch.setenv("ADVISOR_STORAGE_BACKEND", "invalid-runtime-selection")
    monkeypatch.setenv("ADVISOR_DATABASE_URL", "credential-sentinel")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Runtime storage must not be composed")

    monkeypatch.setattr("advanced_trading_advisor.cli.main.create_storage", forbidden)
    output = []
    assert main(["migration", "inspect", "--source", str(backup)], output=output.append) == 0
    assert json.loads(output[0])["outcome"] == "inspected"
    assert "credential-sentinel" not in output[0]


def test_cli_sanitizes_unexpected_errors_and_requires_process_url(backup, monkeypatch):
    monkeypatch.delenv("ADVISOR_DATABASE_URL", raising=False)
    output = []
    assert (
        main(
            ["migration", "verify", "--source", str(backup), "--target-schema", "public"],
            output=output.append,
        )
        == 1
    )
    assert "process environment" in output[-1]
    monkeypatch.setenv("ADVISOR_DATABASE_URL", "not-printed-secret")

    async def broken(*_args):
        raise RuntimeError("not-printed-secret raw provider payload")

    monkeypatch.setattr("advanced_trading_advisor.cli.migration.verify_source", broken)
    output.clear()
    assert (
        main(
            ["migration", "verify", "--source", str(backup), "--target-schema", "public"],
            output=output.append,
        )
        == 1
    )
    assert output == ["Error: Migration configuration or validation failed."]


def test_live_source_path_refused(monkeypatch, backup):
    monkeypatch.setenv("ADVISOR_DB_PATH", str(backup))
    with pytest.raises(MigrationError, match="backup, not"):
        asyncio.run(load_source(backup))


@pytest.mark.parametrize("schema", ["public", "advisor_test_other", "advisor_rehearsal_bad"])
def test_rehearsal_import_refuses_non_generated_target(backup, schema):
    source = asyncio.run(load_source(backup))

    class MustNotConnect:
        def transaction(self):
            raise AssertionError("Unsafe schema must not connect")

    target = MustNotConnect()
    target.schema = schema
    with pytest.raises(MigrationError, match="not allowed"):
        asyncio.run(import_source(source, target, rehearsal=True))


def test_rehearsal_cleanup_error_reports_only_generated_name(backup, monkeypatch):
    from contextlib import asynccontextmanager

    source = asyncio.run(load_source(backup))
    identifier = uuid4()
    monkeypatch.setattr(migration, "uuid4", lambda: identifier)
    statements = []

    class FakeDatabase:
        def __init__(self, *_args, **_kwargs):
            pass

        @asynccontextmanager
        async def connection(self):
            yield self

        async def execute(self, sql):
            statements.append(sql)
            if sql.startswith("DROP"):
                raise RuntimeError("secret-url-and-payload")

    async def failed_import(*_args, **_kwargs):
        raise RuntimeError("secret-url-and-payload")

    monkeypatch.setattr(migration, "PostgresDatabase", FakeDatabase)
    monkeypatch.setattr(migration, "import_source", failed_import)
    with pytest.raises(MigrationError) as error:
        asyncio.run(migration.rehearse(source, "secret-url-and-payload"))
    assert str(error.value) == (
        "Rehearsal cleanup failed; schema: advisor_rehearsal_" + identifier.hex
    )
    assert len(statements) == 2
    assert statements[1] == f'DROP SCHEMA "advisor_rehearsal_{identifier.hex}" CASCADE'


def test_verification_uses_readonly_transaction_without_initialization(backup):
    source = asyncio.run(load_source(backup))
    events = []
    from contextlib import asynccontextmanager

    class Connection:
        @asynccontextmanager
        async def transaction(self, **options):
            events.append(options)
            yield

        async def fetch(self, query):
            if "information_schema.tables" in query:
                return [{"table_name": t} for t in migration.POSTGRES_COLUMNS]
            if "information_schema.columns" in query:
                return [
                    {"table_name": t, "column_name": c}
                    for t, columns in migration.POSTGRES_COLUMNS.items()
                    for c in columns
                ]
            table = query.split('"')[1]
            return source.rows[table]

        async def fetchval(self, query):
            if "schema_metadata" in query:
                return migration.POSTGRES_SCHEMA_VERSION
            if "count(*)" in query:
                return 0
            raise AssertionError("Unexpected target query")

    class Database:
        @asynccontextmanager
        async def connection(self):
            yield Connection()

        async def initialize(self):
            raise AssertionError("verify must not initialize")

    assert asyncio.run(migration.verify_source(source, Database())) == "verified"
    assert events == [{"isolation": "repeatable_read", "readonly": True}]
