import asyncio
import json
import sqlite3
import subprocess
import sys
from datetime import timedelta

import pytest
from test_advisor import FakeProvider, fixture
from test_portfolio_risk import NOW

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.analysis.recovery import LEASE_DURATION
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.analysis.use_case import AnalysisFailed, AnalyzeCurrentPortfolio
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    FailureCategory,
    PersistenceError,
)
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.maintenance import backup_database, inspect_database, readonly
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)


async def setup(path):
    db = Database(path)
    await db.initialize()
    snapshots, runs = SQLiteSnapshotRepository(db), SQLiteAnalysisRunRepository(db)
    saved = await ConfirmedStateService(snapshots).initialize(fixture()[0])
    return db, snapshots, runs, saved


def test_wal_foreign_keys_busy_and_interrupted_transaction(tmp_path):
    async def run():
        db, snapshots, _, saved = await setup(tmp_path / "db.sqlite")
        async with db.connection() as c:
            assert (await (await c.execute("PRAGMA journal_mode")).fetchone())[0] == "wal"
            assert (await (await c.execute("PRAGMA foreign_keys")).fetchone())[0] == 1
            assert (await (await c.execute("PRAGMA busy_timeout")).fetchone())[0] == 5000
        with pytest.raises(asyncio.CancelledError):
            async with db.transaction() as c:
                await c.execute("UPDATE current_portfolio SET revision=999")
                raise asyncio.CancelledError()
        assert await snapshots.get_current() == saved

    asyncio.run(run())


def test_concurrent_acquisition_only_one_owner(tmp_path):
    async def run():
        db, _, runs, saved = await setup(tmp_path / "db.sqlite")
        other = SQLiteAnalysisRunRepository(Database(db.path))
        attempts = [AnalysisRun.start(saved.snapshot.id, NOW) for _ in range(2)]
        results = await asyncio.gather(
            runs.acquire(attempts[0]), other.acquire(attempts[1]), return_exceptions=True
        )
        assert sum(isinstance(r, ConflictError) for r in results) == 1
        assert len(await runs.list_all_recent()) == 1
        assert not (await inspect_database(db.path, now=NOW)).failed

    asyncio.run(run())


def test_stale_recovery_fences_previous_owner_and_keeps_audit(tmp_path):
    async def run():
        db, snapshots, runs, saved = await setup(tmp_path / "db.sqlite")
        old = AnalysisRun.start(saved.snapshot.id, NOW)
        await runs.acquire(old)
        fresh = AnalysisRun.start(saved.snapshot.id, NOW + LEASE_DURATION)
        await runs.acquire(fresh)
        stored = await runs.get(old.run_id)
        assert stored.run.status is AnalysisRunStatus.FAILED
        assert stored.failure_category is FailureCategory.INTERRUPTED
        assert stored.result_json is None
        with pytest.raises(ConflictError):
            await runs.mark_completed(old.run_id, completed_at=NOW)
        assert await snapshots.get_current() == saved
        await runs.mark_failed(
            fresh.run_id,
            completed_at=fresh.started_at,
            failure_category=FailureCategory.INTERRUPTED,
        )
        assert not (await inspect_database(db.path, now=fresh.started_at)).failed

    asyncio.run(run())


def test_recent_run_not_recovered_and_expired_completion_refused(tmp_path):
    async def run():
        _, _, runs, saved = await setup(tmp_path / "db.sqlite")
        old = AnalysisRun.start(saved.snapshot.id, NOW)
        await runs.acquire(old)
        with pytest.raises(ConflictError):
            await runs.acquire(AnalysisRun.start(saved.snapshot.id, NOW + timedelta(minutes=14)))
        assert (await runs.get(old.run_id)).run.status is AnalysisRunStatus.RUNNING
        with pytest.raises(ConflictError):
            await runs.mark_completed(old.run_id, completed_at=NOW + LEASE_DURATION)

    asyncio.run(run())


@pytest.mark.parametrize("stage", ["success", "market", "ai", "persistence", "report", "failure"])
def test_release_and_no_mutation_on_pipeline_boundaries(tmp_path, stage):
    async def run():
        db, snapshots, runs, saved = await setup(tmp_path / "db.sqlite")

        class Provider(FakeProvider):
            async def get_market_snapshot(self, symbols):
                if stage == "market":
                    raise asyncio.CancelledError()
                if stage == "failure":
                    raise RuntimeError("sensitive provider details")
                return await super().get_market_snapshot(symbols)

        class Reasoner:
            model_name = "test-model"

            async def propose(self, *args):
                raise asyncio.CancelledError()

        class CancelledRepository(SQLiteAnalysisRunRepository):
            async def mark_completed(self, run_id, **kwargs):
                async with db.transaction() as c:
                    await c.execute(
                        "UPDATE analysis_runs SET market_cutoff=? WHERE run_id=?",
                        (NOW.isoformat(), str(run_id)),
                    )
                    raise asyncio.CancelledError()

        def report(_):
            raise asyncio.CancelledError()

        use_case = AnalyzeCurrentPortfolio(
            snapshots,
            CancelledRepository(db) if stage == "persistence" else runs,
            Provider,
            now=lambda: NOW,
            reasoner=Reasoner() if stage == "ai" else None,
            prepare_report=report if stage == "report" else None,
        )
        if stage == "success":
            result = await use_case.execute()
            assert (await runs.get(result.run_id)).run.status is AnalysisRunStatus.COMPLETED
        else:
            with pytest.raises(AnalysisFailed if stage == "failure" else asyncio.CancelledError):
                await use_case.execute()
            stored = (await runs.list_all_recent())[0]
            assert stored.run.status is AnalysisRunStatus.FAILED and stored.result_json is None
            if stage != "failure":
                assert stored.failure_category is FailureCategory.INTERRUPTED
        assert await snapshots.get_current() == saved
        await runs.acquire(AnalysisRun.start(saved.snapshot.id, NOW))

    asyncio.run(run())


def test_terminal_sql_guard(tmp_path):
    async def run():
        db, snapshots, runs, _ = await setup(tmp_path / "db.sqlite")
        await AnalyzeCurrentPortfolio(snapshots, runs, FakeProvider, now=lambda: NOW).execute()
        for sql in ("DELETE FROM analysis_runs", "UPDATE analysis_runs SET status='running'"):
            with pytest.raises(sqlite3.IntegrityError):
                async with db.transaction() as c:
                    await c.execute(sql)

    asyncio.run(run())


def test_backup_consistent_under_uncommitted_writer_and_no_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-in-db")

    async def run():
        db, snapshots, runs, _ = await setup(tmp_path / "db.sqlite")
        result = await AnalyzeCurrentPortfolio(
            snapshots, runs, FakeProvider, now=lambda: NOW
        ).execute()
        async with db.connection() as c:
            await c.execute("BEGIN IMMEDIATE")
            await c.execute("UPDATE current_portfolio SET revision=999")
            path, report = await backup_database(
                db.path, tmp_path / "nested" / "copy.sqlite", now=NOW
            )
            assert report.revision == 1 and not report.failed
            await c.rollback()
        assert (
            await SQLiteAnalysisRunRepository(Database(path)).latest_completed()
        ).result_json == AnalysisResultCodec.encode(result)
        original = path.read_bytes()
        assert b"synthetic-never-in-db" not in original
        with pytest.raises(PersistenceError, match="already exists"):
            await backup_database(db.path, path)
        assert path.read_bytes() == original
        assert not (await inspect_database(path, now=NOW)).failed

    asyncio.run(run())


@pytest.mark.parametrize("damage", ["corrupt", "dangling", "revision", "missing_result", "schema"])
def test_doctor_detects_damage_and_backup_refuses_it(tmp_path, damage):
    async def run():
        db, _, runs, saved = await setup(tmp_path / "db.sqlite")
        if damage == "corrupt":
            db.path.write_bytes(b"not a sqlite database")
        elif damage == "missing_result":
            run = AnalysisRun.start(saved.snapshot.id, NOW)
            await runs.create(run)
            await runs.mark_completed(run.run_id, completed_at=NOW)
        else:
            async with db.connection() as c:
                await c.execute("PRAGMA foreign_keys=OFF")
                sql = {
                    "dangling": "UPDATE current_portfolio SET snapshot_id='missing'",
                    "revision": "UPDATE current_portfolio SET revision=999",
                    "schema": "UPDATE schema_metadata SET version=999",
                }[damage]
                await c.execute(sql)
        assert (await inspect_database(db.path, now=NOW)).failed
        with pytest.raises((PersistenceError, sqlite3.DatabaseError)):
            await backup_database(db.path, tmp_path / "bad-copy.sqlite", now=NOW)
        assert not (tmp_path / "bad-copy.sqlite").exists()

    asyncio.run(run())
    output = []
    assert (
        main(["--db", str(tmp_path / "db.sqlite"), "doctor"], output=output.append, now=lambda: NOW)
        == 1
    )
    assert "FAIL" in "\n".join(output)


def test_doctor_stale_warning_is_read_only(tmp_path):
    async def run():
        db, _, runs, saved = await setup(tmp_path / "db.sqlite")
        old = AnalysisRun.start(saved.snapshot.id, NOW)
        await runs.create(old)
        report = await inspect_database(db.path, now=NOW + LEASE_DURATION)
        assert not report.failed and any("WARN stale" in c for c in report.checks)
        assert (await runs.get(old.run_id)).run.status is AnalysisRunStatus.RUNNING

    asyncio.run(run())


def test_configuration_traceability_roundtrip_and_legacy(tmp_path):
    async def run():
        _, snapshots, runs, _ = await setup(tmp_path / "db.sqlite")
        a = await AnalyzeCurrentPortfolio(snapshots, runs, FakeProvider, now=lambda: NOW).execute()
        b = await AnalyzeCurrentPortfolio(snapshots, runs, FakeProvider, now=lambda: NOW).execute()
        assert a.configuration.serialize() == b.configuration.serialize()
        assert (
            a.configuration.stable_reserve_version == 2 and a.configuration.whole_plan_version == 3
        )
        assert a.configuration.ai_context_version is None and not a.configuration.ai_enabled
        assert "API_KEY" not in a.configuration.serialize()
        payload = json.loads(AnalysisResultCodec.encode(a))
        assert payload["schema_version"] == 5
        del payload["data"]["active"]
        for asset_data in payload["data"]["analyzed_assets"]:
            del asset_data["daily_context"]
            del asset_data["closed_1h_candles"]
        for recommendation in payload["data"]["recommendations"]:
            recommendation.pop("setup_id")
        for field in (
            "hard_expense_reserve_usd",
            "market_buffer_usd",
            "potential_trading_liquidity_usd",
            "strategy_dry_powder_usd",
            "recommended_deployment_usd",
        ):
            del payload["data"]["financial"][field]
        del payload["data"]["configuration"]
        payload["schema_version"] = 2
        old = AnalysisResultCodec.decode(json.dumps(payload))
        assert old.snapshot_id == a.snapshot_id and old.active is None
        assert old.configuration is None

    asyncio.run(run())


def test_configuration_fingerprint_excludes_runtime_failure_outcome(tmp_path):
    async def run():
        from dataclasses import replace

        _, snapshots, runs, _ = await setup(tmp_path / "db.sqlite")
        result = await AnalyzeCurrentPortfolio(
            snapshots, runs, FakeProvider, now=lambda: NOW
        ).execute()
        configuration = result.configuration
        provider = replace(configuration, ai_enabled=True, model_name="test-model")
        provider = replace(provider, ai_failure_category="ai_provider")
        transport = replace(provider, ai_failure_category="ai_transport")
        assert provider.serialize() != transport.serialize()
        assert "ai_provider" in provider.serialize()
        assert "ai_provider" not in provider.fingerprint_material()
        assert provider.fingerprint_material() == transport.fingerprint_material()

    asyncio.run(run())


def test_backup_cli_and_inspect_do_not_touch_default_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "dev.sqlite"
    output = []
    common = dict(output=output.append, input_fn=lambda _: "yes")
    assert (
        main(["--db", str(db), "setup", "--position", "USDC=3450", "--expenses", "250"], **common)
        == 0
    )
    assert main(["--db", str(db), "analyze", "--source", "static", "--no-ai"], **common) == 0
    assert main(["--db", str(db), "doctor"], **common) == 0
    target = tmp_path / "copy.sqlite"
    assert main(["--db", str(db), "backup", "--output", str(target)], **common) == 0
    nonexistent = tmp_path / "never-create.sqlite"
    assert main(["--db", str(nonexistent), "backup", "inspect", str(target)], **common) == 0
    assert not nonexistent.exists()

    async def check():
        async with readonly(target) as c:
            with pytest.raises(sqlite3.OperationalError):
                await c.execute("DELETE FROM current_portfolio")

    asyncio.run(check())


def test_lease_survives_repository_process_boundary(tmp_path):
    async def acquire():
        _, _, runs, saved = await setup(tmp_path / "db.sqlite")
        await runs.acquire(AnalysisRun.start(saved.snapshot.id, NOW))
        return saved.snapshot.id

    snapshot_id = asyncio.run(acquire())
    script = """
import asyncio, sys
from pathlib import Path
from uuid import UUID
from datetime import datetime
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import SQLiteAnalysisRunRepository
from advanced_trading_advisor.analysis.models import AnalysisRun
from advanced_trading_advisor.domain.persistence import ConflictError
async def run():
    repo = SQLiteAnalysisRunRepository(Database(Path(sys.argv[1])))
    try:
        run = AnalysisRun.start(UUID(sys.argv[2]), datetime.fromisoformat(sys.argv[3]))
        await repo.acquire(run)
    except ConflictError:
        return 0
    return 3
sys.exit(asyncio.run(run()))
"""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path / "db.sqlite"),
            str(snapshot_id),
            NOW.isoformat(),
        ],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0


def test_cancellation_after_acquisition_commit_releases_own_run(tmp_path):
    async def run():
        db, snapshots, _, saved = await setup(tmp_path / "db.sqlite")

        class InterruptedAcquire(SQLiteAnalysisRunRepository):
            async def acquire(self, run):
                await super().acquire(run)
                raise asyncio.CancelledError()

        runs = InterruptedAcquire(db)
        with pytest.raises(asyncio.CancelledError):
            await AnalyzeCurrentPortfolio(snapshots, runs, FakeProvider, now=lambda: NOW).execute()
        stored = (await runs.list_all_recent())[0]
        assert stored.failure_category is FailureCategory.INTERRUPTED
        assert await snapshots.get_current() == saved
        await SQLiteAnalysisRunRepository(db).acquire(AnalysisRun.start(saved.snapshot.id, NOW))

    asyncio.run(run())


def test_persistence_failure_sanitized_and_recoverable(tmp_path):
    async def run():
        db, snapshots, _, saved = await setup(tmp_path / "db.sqlite")

        class FailedSave(SQLiteAnalysisRunRepository):
            async def mark_completed(self, *args, **kwargs):
                raise sqlite3.OperationalError("private details")

        runs = FailedSave(db)
        with pytest.raises(AnalysisFailed, match="database") as exc:
            await AnalyzeCurrentPortfolio(snapshots, runs, FakeProvider, now=lambda: NOW).execute()
        assert "private details" not in str(exc.value)
        stored = (await runs.list_all_recent())[0]
        assert stored.failure_category is FailureCategory.DATABASE and stored.result_json is None
        assert await snapshots.get_current() == saved
        await runs.acquire(AnalysisRun.start(saved.snapshot.id, NOW))

    asyncio.run(run())


def test_ai_contract_provenance_and_fallback_category(tmp_path):
    import httpx
    from test_ai import adapter

    async def run():
        _, snapshots, runs, _ = await setup(tmp_path / "db.sqlite")
        ai, _ = adapter(error=httpx.ReadTimeout("private provider error"))
        result = await AnalyzeCurrentPortfolio(
            snapshots, runs, FakeProvider, now=lambda: NOW, reasoner=ai
        ).execute()
        config = result.configuration
        assert (config.ai_context_version, config.ai_schema_version, config.ai_prompt_version) == (
            4,
            3,
            4,
        )
        assert config.ai_failure_category == "ai_timeout"
        assert config.reasoning_effort == "low" and config.model_name == ai.model_name
        assert "private provider error" not in AnalysisResultCodec.encode(result)

    asyncio.run(run())


def test_default_backup_path_is_timestamped_and_private(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    async def run():
        db, _, _, _ = await setup(tmp_path / "db.sqlite")
        path, report = await backup_database(db.path, now=NOW)
        assert path.parent == tmp_path / ".advanced_trading_advisor" / "backups"
        assert path.name.endswith("Z.sqlite") and "20260909T123000" in path.name
        assert path.stat().st_mode & 0o777 == 0o600 and not report.failed

    asyncio.run(run())
