import asyncio
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.analysis.active import ActiveAnalysis, TelegramSeverity
from advanced_trading_advisor.analysis.history_query import AnalysisHistoryQuery, AnalysisRunDetail
from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.analysis.policy import RecommendationPolicy
from advanced_trading_advisor.analysis.recommendations import ActionType, Recommendation
from advanced_trading_advisor.analysis.result import (
    AnalysisResult,
    AnalysisResultCodec,
    FinancialSummary,
)
from advanced_trading_advisor.analysis.traceability import DecisionConfiguration, policy_parameters
from advanced_trading_advisor.domain.persistence import FailureCategory, StoredAnalysisRun
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.market.models import MarketSnapshot
from advanced_trading_advisor.market.regime import MarketRegime

START = datetime(2026, 9, 25, 12, tzinfo=UTC)
RUN_ID = UUID("e114877b-3954-486d-8849-e10a8f439b8e")
FAILED_ID = UUID("e514574b-c398-4ef4-a0a2-7ca785780080")
RUNNING_ID = UUID("165f2f56-a477-4fd7-a123-cc542eb039ee")
CANCELLED_ID = UUID("c4d76466-90c4-425b-94dc-d35b35a4d5e8")
HISTORICAL_SNAPSHOT_ID = UUID("f07034b1-12f1-424c-8f40-f366956e2439")
OTHER_SNAPSHOT_ID = UUID("7666432b-688b-4e7c-982c-a079878ed920")
RECOMMENDATION_ID = UUID("b80f588d-5bf4-411a-92df-012d772d4b9d")


def completed_result() -> AnalysisResult:
    return AnalysisResult(
        RUN_ID,
        HISTORICAL_SNAPSHOT_ID,
        START + timedelta(seconds=2),
        MarketSnapshot(START + timedelta(seconds=1), (), is_live=False),
        MarketRegime.NEUTRAL,
        1,
        1,
        FinancialSummary(
            Decimal("100"),
            Decimal("50"),
            Decimal("50"),
            Decimal("10"),
            Decimal("12"),
            Decimal("120"),
            Decimal("0"),
            Decimal("70"),
            Decimal("5"),
            Decimal("0"),
        ),
        (),
        (Recommendation(RECOMMENDATION_ID, "BTC", ActionType.HOLD, 3, "Wait."),),
        configuration=DecisionConfiguration(
            1,
            1,
            1,
            "12",
            "static",
            False,
            None,
            None,
            None,
            None,
            None,
            None,
            policy_parameters(RecommendationPolicy()),
        ),
        active=ActiveAnalysis(2, (), (), (), Decimal("0"), Decimal("0"), TelegramSeverity.QUIET),
    )


def stored_runs() -> tuple[StoredAnalysisRun, ...]:
    completed = StoredAnalysisRun(
        AnalysisRun(
            RUN_ID,
            HISTORICAL_SNAPSHOT_ID,
            START,
            AnalysisRunStatus.COMPLETED,
            START + timedelta(seconds=3),
        ),
        AnalysisResultCodec.encode(completed_result()),
    )
    failed = StoredAnalysisRun(
        AnalysisRun(
            FAILED_ID,
            OTHER_SNAPSHOT_ID,
            START + timedelta(seconds=10),
            AnalysisRunStatus.FAILED,
            START + timedelta(seconds=12),
        ),
        failure_category=FailureCategory.MARKET_UNAVAILABLE,
    )
    running = StoredAnalysisRun(
        AnalysisRun(RUNNING_ID, OTHER_SNAPSHOT_ID, START + timedelta(seconds=20))
    )
    cancelled = StoredAnalysisRun(
        AnalysisRun(
            CANCELLED_ID,
            OTHER_SNAPSHOT_ID,
            START + timedelta(seconds=30),
            AnalysisRunStatus.CANCELLED,
            START + timedelta(seconds=31),
        )
    )
    return cancelled, running, failed, completed


class ReadOnlyRepository:
    def __init__(self, records: tuple[StoredAnalysisRun, ...] = ()) -> None:
        self.records = records
        self.calls = []

    async def latest_completed(self):
        self.calls.append(("latest_completed",))
        return next(
            (
                stored
                for stored in self.records
                if stored.run.status is AnalysisRunStatus.COMPLETED
                and stored.result_json is not None
            ),
            None,
        )

    async def list_all_recent(self, limit=20):
        self.calls.append(("list_all_recent", limit))
        return self.records[:limit]

    async def get(self, run_id):
        self.calls.append(("get", run_id))
        return next((stored for stored in self.records if stored.run.run_id == run_id), None)

    async def acquire(self, *_args):
        raise AssertionError("History query must not acquire an analysis lease")

    async def create(self, *_args):
        raise AssertionError("History query must not create an analysis run")

    async def mark_completed(self, *_args, **_kwargs):
        raise AssertionError("History query must not complete an analysis run")

    async def mark_failed(self, *_args, **_kwargs):
        raise AssertionError("History query must not fail an analysis run")


def test_latest_completed_returns_none_when_absent() -> None:
    repository = ReadOnlyRepository(stored_runs()[:3])
    assert asyncio.run(AnalysisHistoryQuery(repository).latest_completed()) is None
    assert repository.calls == [("latest_completed",)]


def test_latest_completed_decodes_full_result_and_keeps_historical_identity() -> None:
    repository = ReadOnlyRepository(stored_runs())
    detail = asyncio.run(AnalysisHistoryQuery(repository).latest_completed())
    assert isinstance(detail, AnalysisRunDetail)
    assert detail.run.run_id == RUN_ID
    assert detail.run.snapshot_id == HISTORICAL_SNAPSHOT_ID
    assert detail.run.status is AnalysisRunStatus.COMPLETED
    assert detail.run.started_at == START
    assert detail.run.completed_at == START + timedelta(seconds=3)
    assert detail.run.market_cutoff is None
    assert detail.result == completed_result()
    assert detail.result.active is not None
    assert detail.result.active.setups == ()
    assert detail.failure_category is None
    assert repository.calls == [("latest_completed",)]


def test_recent_is_bounded_ordered_metadata_without_raw_results() -> None:
    repository = ReadOnlyRepository(stored_runs())
    query = AnalysisHistoryQuery(repository)
    assert asyncio.run(query.recent()) == tuple(stored.run for stored in repository.records)
    summaries = asyncio.run(query.recent(3))
    assert tuple(run.run_id for run in summaries) == (CANCELLED_ID, RUNNING_ID, FAILED_ID)
    assert tuple(run.status for run in summaries) == (
        AnalysisRunStatus.CANCELLED,
        AnalysisRunStatus.RUNNING,
        AnalysisRunStatus.FAILED,
    )
    assert asyncio.run(query.recent(4))[-1].status is AnalysisRunStatus.COMPLETED
    assert all(isinstance(item, AnalysisRun) for item in summaries)
    assert all(not hasattr(item, "result_json") for item in summaries)
    assert all(not hasattr(item, "result") for item in summaries)
    assert repository.calls == [
        ("list_all_recent", 20),
        ("list_all_recent", 3),
        ("list_all_recent", 4),
    ]


def test_recent_empty_and_limit_validation_precedes_repository_call() -> None:
    repository = ReadOnlyRepository()
    query = AnalysisHistoryQuery(repository)
    assert asyncio.run(query.recent(1)) == ()
    assert repository.calls == [("list_all_recent", 1)]
    for invalid in (0, -1, True, "1"):
        with pytest.raises(DomainValidationError, match="Limit must be positive"):
            asyncio.run(query.recent(invalid))
    assert repository.calls == [("list_all_recent", 1)]


def test_detail_uses_exact_id_and_decodes_only_completed_result() -> None:
    repository = ReadOnlyRepository(stored_runs())
    query = AnalysisHistoryQuery(repository)
    completed = asyncio.run(query.detail(RUN_ID))
    assert completed.run.run_id == completed.result.run_id == RUN_ID
    assert completed.run.snapshot_id == completed.result.snapshot_id == HISTORICAL_SNAPSHOT_ID
    assert completed.result == completed_result()
    assert completed.result.active is not None

    failed = asyncio.run(query.detail(FAILED_ID))
    assert failed.run.status is AnalysisRunStatus.FAILED
    assert failed.result is None
    assert failed.failure_category is FailureCategory.MARKET_UNAVAILABLE
    running = asyncio.run(query.detail(RUNNING_ID))
    assert running.run.status is AnalysisRunStatus.RUNNING
    assert running.result is None
    cancelled = asyncio.run(query.detail(CANCELLED_ID))
    assert cancelled.run.status is AnalysisRunStatus.CANCELLED
    assert cancelled.result is None
    assert asyncio.run(query.detail(OTHER_SNAPSHOT_ID)) is None
    assert repository.calls == [
        ("get", RUN_ID),
        ("get", FAILED_ID),
        ("get", RUNNING_ID),
        ("get", CANCELLED_ID),
        ("get", OTHER_SNAPSHOT_ID),
    ]


def test_completed_run_without_result_returns_metadata_without_fake_result() -> None:
    completed = replace(stored_runs()[-1], result_json=None)
    detail = asyncio.run(AnalysisHistoryQuery(ReadOnlyRepository((completed,))).detail(RUN_ID))
    assert detail.run.status is AnalysisRunStatus.COMPLETED
    assert detail.result is None


@pytest.mark.parametrize(
    "payload",
    [
        "not JSON",
        '{"schema_version":99,"data":{}}',
    ],
)
def test_malformed_or_unsupported_completed_result_is_not_discarded(payload) -> None:
    corrupt = replace(stored_runs()[-1], result_json=payload)
    query = AnalysisHistoryQuery(ReadOnlyRepository((corrupt,)))
    with pytest.raises(ValueError, match="Malformed or unsupported analysis result"):
        asyncio.run(query.detail(RUN_ID))


def test_result_identity_mismatch_is_rejected_instead_of_substituting_a_snapshot() -> None:
    wrong = replace(completed_result(), snapshot_id=OTHER_SNAPSHOT_ID)
    stored = replace(stored_runs()[-1], result_json=AnalysisResultCodec.encode(wrong))
    with pytest.raises(DomainValidationError, match="identity disagrees"):
        asyncio.run(AnalysisHistoryQuery(ReadOnlyRepository((stored,))).detail(RUN_ID))


def test_public_query_result_types_do_not_expose_raw_payloads_or_repository_objects() -> None:
    repository = ReadOnlyRepository(stored_runs())
    query = AnalysisHistoryQuery(repository)

    async def scenario():
        await query.latest_completed()
        await query.recent(2)
        return await query.detail(RUN_ID)

    detail = asyncio.run(scenario())
    assert {field.name for field in fields(AnalysisRunDetail)} == {
        "run",
        "result",
        "failure_category",
    }
    assert not hasattr(detail, "result_json")
    assert not hasattr(detail.run, "result_json")
    assert repository.calls == [
        ("latest_completed",),
        ("list_all_recent", 2),
        ("get", RUN_ID),
    ]
