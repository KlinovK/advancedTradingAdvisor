"""In-process, offline tests for private analysis history HTTP reads."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.analysis.active import (
    ActiveAnalysis,
    AssetDecision,
    AssetDecisionType,
    SetupLevel,
    SetupScore,
    SetupStatus,
    SetupType,
    TelegramSeverity,
    TradeSetup,
)
from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.analysis.policy import RecommendationPolicy
from advanced_trading_advisor.analysis.recommendations import ActionType, Recommendation
from advanced_trading_advisor.analysis.result import (
    AnalysisResult,
    AnalysisResultCodec,
    FinancialSummary,
)
from advanced_trading_advisor.analysis.traceability import DecisionConfiguration, policy_parameters
from advanced_trading_advisor.analysis.triggers import (
    TriggerContext,
    TriggerReason,
    TriggerTimeframe,
    TriggerTransition,
)
from advanced_trading_advisor.domain.persistence import FailureCategory, StoredAnalysisRun
from advanced_trading_advisor.http.app import create_app
from advanced_trading_advisor.market.models import (
    Candle,
    DailyTrend,
    DataCompleteness,
    MarketAssetData,
    MarketSnapshot,
    TechnicalFeatures,
)
from advanced_trading_advisor.market.regime import MarketRegime
from advanced_trading_advisor.portfolio.models import TradingSide

TOKEN = "synthetic-http-analysis-token"
START = datetime(2026, 9, 25, 12, tzinfo=UTC)
RUN_ID = UUID("c1111111-1111-4111-8111-111111111111")
FAILED_ID = UUID("c2222222-2222-4222-8222-222222222222")
RUNNING_ID = UUID("c3333333-3333-4333-8333-333333333333")
CANCELLED_ID = UUID("c4444444-4444-4444-8444-444444444444")
HISTORICAL_SNAPSHOT_ID = UUID("c5555555-5555-4555-8555-555555555555")
CURRENT_SNAPSHOT_ID = UUID("c6666666-6666-4666-8666-666666666666")
SETUP_ID = UUID("c7777777-7777-4777-8777-777777777777")
RECOMMENDATION_ID = UUID("c8888888-8888-4888-8888-888888888888")


def completed_result() -> AnalysisResult:
    touch_time = START - timedelta(hours=1)
    touch_candle = Candle(
        touch_time, Decimal("100.00"), Decimal("101.00"), Decimal("99.00"), Decimal("100.50")
    )
    confirmation_candle = Candle(
        START, Decimal("100.50"), Decimal("102.00"), Decimal("100.00"), Decimal("101.00")
    )
    trigger = TriggerContext(
        version=1,
        zone_low=Decimal("95.000000000000000001"),
        zone_high=Decimal("105.000000000000000001"),
        atr_at_creation=Decimal("10.000000000000000001"),
        transitions=(
            TriggerTransition(
                SetupStatus.CANDIDATE,
                SetupStatus.ZONE_TOUCHED,
                touch_time,
                touch_time,
                TriggerTimeframe.ONE_HOUR,
                TriggerReason.TOUCHED_ZONE,
                Decimal("100.000000000000000001"),
            ),
            TriggerTransition(
                SetupStatus.ZONE_TOUCHED,
                SetupStatus.CONFIRMED,
                START,
                START,
                TriggerTimeframe.ONE_HOUR,
                TriggerReason.SUPPORT_HOLD,
                Decimal("101.000000000000000001"),
            ),
        ),
        touched_at=touch_time,
        highest_observed_price=Decimal("102.00"),
        lowest_observed_price=Decimal("99.00"),
        trigger_reference_price=Decimal("101.00"),
        confirmation_at=START,
        confirmation_candle=confirmation_candle,
        expires_at=START + timedelta(hours=4),
        last_processed_1h_candle_at=START,
        execution_valid=True,
    )
    setup = TradeSetup(
        SETUP_ID,
        "BTC",
        TradingSide.BUY,
        SetupType.SUPPORT_HOLD,
        START - timedelta(hours=4),
        START - timedelta(hours=4),
        SetupStatus.CONFIRMED,
        Decimal("100.123456789012345678"),
        "support",
        Decimal("90.00"),
        "support breakdown",
        Decimal("120.00"),
        "resistance",
        Decimal("2.0"),
        Decimal("30.000000000000000001"),
        SetupScore(1, (("trend", 75, 100),)),
        levels=(SetupLevel(Decimal("100.123456789012345678"), "support", 1),),
        trigger=trigger,
    )
    active = ActiveAnalysis(
        2,
        (setup,),
        (
            AssetDecision(
                "BTC",
                AssetDecisionType.ADD,
                Decimal("5.0"),
                Decimal("500.00"),
                Decimal("50.0"),
                None,
                None,
                Decimal("5.0"),
                DailyTrend.INCOMPLETE,
                SETUP_ID,
            ),
        ),
        (SETUP_ID,),
        Decimal("30.000000000000000001"),
        Decimal("350.00"),
        TelegramSeverity.ACTION,
    )
    market = MarketSnapshot(
        START + timedelta(seconds=1),
        (
            MarketAssetData(
                "BTC",
                Decimal("100.123456789012345678"),
                START + timedelta(seconds=1),
                START,
                TechnicalFeatures(
                    change_24h_pct=Decimal("-1.234567890123456789"),
                    rsi_4h=Decimal("50.000000000000000001"),
                    support=Decimal("95.000000000000000001"),
                ),
                DataCompleteness.PARTIAL,
                Decimal("100.000000000000000001"),
                closed_1h_candles=(touch_candle, confirmation_candle),
            ),
        ),
        is_live=False,
    )
    financial = FinancialSummary(
        Decimal("1000.000000000000000001"),
        Decimal("500.00"),
        Decimal("50.0"),
        Decimal("10.00"),
        Decimal("12.0"),
        Decimal("120.00"),
        Decimal("380.00"),
        Decimal("0"),
        Decimal("50.0"),
        Decimal("0"),
        hard_expense_reserve_usd=Decimal("120.00"),
        potential_trading_liquidity_usd=Decimal("380.00"),
        strategy_dry_powder_usd=Decimal("350.00"),
        recommended_deployment_usd=Decimal("30.000000000000000001"),
    )
    recommendation = Recommendation(
        RECOMMENDATION_ID,
        "BTC",
        ActionType.PLACE_LIMIT_ORDER,
        1,
        "Confirmed support setup.",
        side=TradingSide.BUY,
        target_price=Decimal("100.123456789012345678"),
        quantity_asset=Decimal("0.123456789012345678"),
        invalidation_condition="Support breaks.",
        review_at=START + timedelta(hours=1),
        expires_at=START + timedelta(hours=4),
        setup_id=SETUP_ID,
    )
    config = DecisionConfiguration(
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
    )
    return AnalysisResult(
        RUN_ID,
        HISTORICAL_SNAPSHOT_ID,
        START + timedelta(seconds=2),
        market,
        MarketRegime.NEUTRAL,
        1,
        1,
        financial,
        ("high_asset_concentration",),
        (recommendation,),
        configuration=config,
        active=active,
    )


def records() -> tuple[StoredAnalysisRun, ...]:
    completed = StoredAnalysisRun(
        AnalysisRun(
            RUN_ID,
            HISTORICAL_SNAPSHOT_ID,
            START - timedelta(minutes=1),
            AnalysisRunStatus.COMPLETED,
            START + timedelta(seconds=3),
            START,
        ),
        AnalysisResultCodec.encode(completed_result()),
    )
    failed = StoredAnalysisRun(
        AnalysisRun(
            FAILED_ID,
            CURRENT_SNAPSHOT_ID,
            START + timedelta(minutes=1),
            AnalysisRunStatus.FAILED,
            START + timedelta(minutes=2),
        ),
        failure_category=FailureCategory.MARKET_UNAVAILABLE,
    )
    running = StoredAnalysisRun(
        AnalysisRun(RUNNING_ID, CURRENT_SNAPSHOT_ID, START + timedelta(minutes=3))
    )
    cancelled = StoredAnalysisRun(
        AnalysisRun(
            CANCELLED_ID,
            CURRENT_SNAPSHOT_ID,
            START + timedelta(minutes=4),
            AnalysisRunStatus.CANCELLED,
            START + timedelta(minutes=5),
        )
    )
    return cancelled, running, failed, completed


class FakeAnalyses:
    def __init__(self, stored: tuple[StoredAnalysisRun, ...] = ()) -> None:
        self.stored = stored
        self.calls: list[tuple] = []

    async def latest_completed(self):
        self.calls.append(("latest_completed",))
        return next(
            (
                item
                for item in self.stored
                if item.run.status is AnalysisRunStatus.COMPLETED and item.result_json is not None
            ),
            None,
        )

    async def list_all_recent(self, limit=20):
        self.calls.append(("list_all_recent", limit))
        return self.stored[:limit]

    async def get(self, run_id):
        self.calls.append(("get", run_id))
        return next((item for item in self.stored if item.run.run_id == run_id), None)

    async def acquire(self, *_args):
        raise AssertionError("Analysis HTTP must not acquire a lease.")

    async def create(self, *_args):
        raise AssertionError("Analysis HTTP must not create an analysis.")

    async def mark_completed(self, *_args, **_kwargs):
        raise AssertionError("Analysis HTTP must not write an analysis.")

    async def mark_failed(self, *_args, **_kwargs):
        raise AssertionError("Analysis HTTP must not write an analysis.")


class ForbiddenCurrentPortfolio:
    async def get_current(self):
        raise AssertionError("Analysis HTTP must not load the current portfolio.")


def app_with(repository: FakeAnalyses):
    return create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN},
        snapshots=ForbiddenCurrentPortfolio(),
        analyses=repository,
    )


def get(app, path: str, authorization: str | None = f"Bearer {TOKEN}") -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {} if authorization is None else {"Authorization": authorization}
            return await client.get(path, headers=headers)

    return asyncio.run(request())


def assert_no_float(value) -> None:
    assert not isinstance(value, float)
    if isinstance(value, dict):
        assert "result_json" not in value
        assert "schema_version" not in value
        for item in value.values():
            assert_no_float(item)
    elif isinstance(value, list):
        for item in value:
            assert_no_float(item)


@pytest.mark.parametrize(
    "path", ["/v1/analyses/latest", "/v1/analyses?limit=2", f"/v1/analyses/{RUN_ID}"]
)
@pytest.mark.parametrize("authorization", [None, "Bearer wrong"])
def test_all_analysis_routes_require_the_existing_bearer_authentication(
    path: str, authorization: str | None
) -> None:
    repository = FakeAnalyses(records())

    response = get(app_with(repository), path, authorization)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Authentication required."}
    }
    assert repository.calls == []
    assert TOKEN not in response.text


def test_latest_static_route_returns_decoded_historical_detail_and_active_setup() -> None:
    repository = FakeAnalyses(records())

    response = get(app_with(repository), "/v1/analyses/latest")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert repository.calls == [("latest_completed",)]
    body = response.json()
    assert body["run"] == {
        "run_id": str(RUN_ID),
        "snapshot_id": str(HISTORICAL_SNAPSHOT_ID),
        "started_at": "2026-09-25T11:59:00.000000Z",
        "status": "completed",
        "completed_at": "2026-09-25T12:00:03.000000Z",
        "market_cutoff": "2026-09-25T12:00:00.000000Z",
    }
    result = body["result"]
    assert result["run_id"] == str(RUN_ID)
    assert result["snapshot_id"] == str(HISTORICAL_SNAPSHOT_ID)
    assert result["snapshot_id"] != str(CURRENT_SNAPSHOT_ID)
    assert result["generated_at"] == "2026-09-25T12:00:02.000000Z"
    assert result["market_cutoff"] == "2026-09-25T12:00:00.000000Z"
    assert result["market"]["assets"][0]["price_usd"] == "100.123456789012345678"
    assert result["market"]["assets"][0]["technical_features"]["change_24h_pct"] == (
        "-1.234567890123456789"
    )
    assert result["market"]["assets"][0]["closed_1h_candles"][1]["close"] == "101.00"
    assert result["financial"]["total_portfolio_usd"] == "1000.000000000000000001"
    assert result["financial"]["recommended_deployment_usd"] == "30.000000000000000001"
    assert result["recommendations"][0]["action_type"] == "place_limit_order"
    assert result["recommendations"][0]["target_price"] == "100.123456789012345678"
    assert result["recommendations"][0]["quantity_asset"] == "0.123456789012345678"
    assert result["recommendations"][0]["setup_id"] == str(SETUP_ID)
    assert result["reasoning_mode"] == "DETERMINISTIC"
    assert result["market_regime"] == MarketRegime.NEUTRAL.value
    setup = result["active"]["setups"][0]
    assert setup["id"] == str(SETUP_ID)
    assert setup["status"] == "confirmed"
    assert setup["limit_price"] == "100.123456789012345678"
    assert setup["score"] == {"version": 1, "components": [["trend", 75, 100]]}
    assert setup["trigger"]["zone_low"] == "95.000000000000000001"
    assert setup["trigger"]["transitions"][1]["reason"] == TriggerReason.SUPPORT_HOLD.value
    assert setup["trigger"]["confirmation_candle"]["closed_at"] == ("2026-09-25T12:00:00.000000Z")
    assert result["active"]["ranked_setup_ids"] == [str(SETUP_ID)]
    assert body["failure_category"] is None
    assert all(isinstance(value, str) for value in result["financial"].values())
    assert all(
        value is None or isinstance(value, str)
        for value in result["market"]["assets"][0]["technical_features"].values()
    )
    assert all(
        isinstance(setup[field], str)
        for field in (
            "limit_price",
            "invalidation_price",
            "target_price",
            "risk_reward",
            "maximum_capital_usd",
        )
    )
    assert all(
        isinstance(setup["trigger"][field], str)
        for field in ("zone_low", "zone_high", "atr_at_creation")
    )
    assert all(
        isinstance(result["recommendations"][0][field], str)
        for field in ("target_price", "quantity_asset")
    )
    assert_no_float(body)
    assert TOKEN not in response.text


def test_latest_without_completed_analysis_returns_sanitized_404() -> None:
    repository = FakeAnalyses(records()[:3])

    response = get(app_with(repository), "/v1/analyses/latest")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "analysis_not_found", "message": "Analysis not found."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.calls == [("latest_completed",)]


def test_recent_empty_is_empty_collection_and_limit_is_passed_through() -> None:
    repository = FakeAnalyses()

    response = get(app_with(repository), "/v1/analyses?limit=3")

    assert response.status_code == 200
    assert response.json() == {"analyses": []}
    assert response.headers["cache-control"] == "no-store"
    assert repository.calls == [("list_all_recent", 3)]


def test_recent_preserves_order_limit_and_all_actual_run_statuses() -> None:
    repository = FakeAnalyses(records())
    app = app_with(repository)

    limited = get(app, "/v1/analyses?limit=2")
    complete = get(app, "/v1/analyses?limit=4")

    assert limited.status_code == complete.status_code == 200
    assert limited.headers["cache-control"] == complete.headers["cache-control"] == "no-store"
    assert repository.calls == [("list_all_recent", 2), ("list_all_recent", 4)]
    assert [item["status"] for item in complete.json()["analyses"]] == [
        "cancelled",
        "running",
        "failed",
        "completed",
    ]
    assert [item["run_id"] for item in limited.json()["analyses"]] == [
        str(CANCELLED_ID),
        str(RUNNING_ID),
    ]
    assert all("result" not in item for item in complete.json()["analyses"])
    assert all("failure_category" not in item for item in complete.json()["analyses"])
    assert all(item["snapshot_id"] != "" for item in complete.json()["analyses"])


@pytest.mark.parametrize(
    "path",
    ["/v1/analyses", "/v1/analyses?limit=0", "/v1/analyses?limit=-1", "/v1/analyses?limit=nope"],
)
def test_invalid_or_missing_limit_has_stable_sanitized_client_error(path: str) -> None:
    repository = FakeAnalyses(records())

    response = get(app_with(repository), path)

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert repository.calls == []


@pytest.mark.parametrize(
    "run_id,status,category",
    [
        (RUNNING_ID, "running", None),
        (FAILED_ID, "failed", "market_unavailable"),
        (CANCELLED_ID, "cancelled", None),
    ],
)
def test_noncompleted_detail_has_only_safe_metadata(run_id: UUID, status: str, category) -> None:
    repository = FakeAnalyses(records())

    response = get(app_with(repository), f"/v1/analyses/{run_id}")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["run"]["run_id"] == str(run_id)
    assert body["run"]["snapshot_id"] == str(CURRENT_SNAPSHOT_ID)
    assert body["run"]["status"] == status
    assert body["result"] is None
    assert body["failure_category"] == category
    assert repository.calls == [("get", run_id)]


def test_exact_completed_detail_preserves_historical_snapshot_and_decoded_result() -> None:
    repository = FakeAnalyses(records())

    response = get(app_with(repository), f"/v1/analyses/{RUN_ID}")

    assert response.status_code == 200
    body = response.json()
    assert body["run"]["snapshot_id"] == str(HISTORICAL_SNAPSHOT_ID)
    assert body["result"]["snapshot_id"] == str(HISTORICAL_SNAPSHOT_ID)
    assert body["result"]["active"]["setups"][0]["id"] == str(SETUP_ID)
    assert repository.calls == [("get", RUN_ID)]
    assert_no_float(body)


def test_completed_run_without_stored_result_has_metadata_but_no_invented_result() -> None:
    incomplete = replace(records()[-1], result_json=None)
    repository = FakeAnalyses((incomplete,))

    response = get(app_with(repository), f"/v1/analyses/{RUN_ID}")

    assert response.status_code == 200
    assert response.json()["run"]["status"] == "completed"
    assert response.json()["result"] is None
    assert repository.calls == [("get", RUN_ID)]


def test_older_decoded_result_without_active_analysis_maps_explicit_null() -> None:
    original = completed_result()
    financial = replace(
        original.financial,
        strategy_dry_powder_usd=Decimal("0"),
        recommended_deployment_usd=Decimal("0"),
    )
    legacy = replace(original, active=None, financial=financial)
    historical_payload = json.loads(AnalysisResultCodec.encode(legacy))
    # Actual v3 payloads predate the setup_id recommendation field.
    for recommendation in historical_payload["data"]["recommendations"]:
        recommendation.pop("setup_id")
    stored = replace(records()[-1], result_json=json.dumps(historical_payload))

    response = get(app_with(FakeAnalyses((stored,))), f"/v1/analyses/{RUN_ID}")

    assert response.status_code == 200
    assert response.json()["result"]["active"] is None
    assert response.json()["result"]["snapshot_id"] == str(HISTORICAL_SNAPSHOT_ID)


def test_unknown_run_id_returns_sanitized_404() -> None:
    unknown = UUID("c9999999-9999-4999-8999-999999999999")
    repository = FakeAnalyses(records())

    response = get(app_with(repository), f"/v1/analyses/{unknown}")

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "analysis_not_found", "message": "Analysis not found."}
    }
    assert repository.calls == [("get", unknown)]


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
def test_invalid_run_id_returns_sanitized_client_error_without_query(bad_id: str) -> None:
    repository = FakeAnalyses(records())

    response = get(app_with(repository), f"/v1/analyses/{bad_id}")

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert repository.calls == []


@pytest.mark.parametrize("path", ["/v1/analyses/latest", f"/v1/analyses/{RUN_ID}"])
@pytest.mark.parametrize("payload", ["private raw result_json", '{"schema_version":99,"data":{}}'])
def test_corrupted_completed_result_fails_closed_without_raw_payload(
    path: str, payload: str
) -> None:
    stored = (*records()[:-1], replace(records()[-1], result_json=payload))
    repository = FakeAnalyses(stored)

    response = get(app_with(repository), path)

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Analysis could not be loaded."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text
    assert "schema_version" not in response.text
    assert "result_json" not in response.text


def test_result_run_snapshot_identity_mismatch_fails_closed() -> None:
    wrong_result = replace(completed_result(), snapshot_id=CURRENT_SNAPSHOT_ID)
    stored = (
        *records()[:-1],
        replace(records()[-1], result_json=AnalysisResultCodec.encode(wrong_result)),
    )

    response = get(app_with(FakeAnalyses(stored)), f"/v1/analyses/{RUN_ID}")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert str(CURRENT_SNAPSHOT_ID) not in response.text
