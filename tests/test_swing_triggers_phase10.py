"""Phase 10 temporal trigger tests; synthetic closed candles and temp databases only."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from test_active_phase9 import daily_asset
from test_portfolio_risk import CUTOFF, NOW, asset, market, order, state

from advanced_trading_advisor.ai.context import build_context
from advanced_trading_advisor.analysis.active import ActiveManagementEngine, SetupStatus
from advanced_trading_advisor.analysis.plan import ActivePlanValidator
from advanced_trading_advisor.analysis.policy import RecommendationPolicy
from advanced_trading_advisor.analysis.recommendations import ActionType
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.analysis.triggers import (
    SwingTriggerEngine,
    TriggerReason,
)
from advanced_trading_advisor.analysis.use_case import AnalyzeCurrentPortfolio
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.cli.report import render_analysis
from advanced_trading_advisor.market.models import Candle
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import CorePosition, TradingSide
from advanced_trading_advisor.risk.assessment import PortfolioRiskPolicy
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)
from advanced_trading_advisor.telegram.formatter import format_analysis

D = Decimal
START = datetime(2026, 9, 9, 10, tzinfo=UTC)


def candle(hour, open_, high, low, close):
    return Candle(START + timedelta(hours=hour), *(D(str(x)) for x in (open_, high, low, close)))


def rejection_sequence():
    return (
        candle(0, 80, 82, 79, 81),
        candle(1, 88, 90, 87, 89),
        candle(2, 97, 101, 96, 99),
        candle(3, 98, 99, 93, 94),
    )


def support_sequence():
    return (
        candle(0, 120, 121, 115, 116),
        candle(1, 111, 112, 110, 111),
        candle(2, 103, 104, 99, 101),
        candle(3, 101, 106, 100, 106),
    )


def advance(engine, direction, candles, *, status=SetupStatus.CANDIDATE, context=None):
    context = context or engine.initial_context(level=D("100"), atr=D("10"))
    return engine.evaluate(
        status=status,
        context=context,
        direction=direction,
        candles=candles,
        latest_4h_close=D("100"),
        latest_4h_cutoff=START,
        invalidation_price=D("110") if direction is TradingSide.SELL else D("90"),
    )


def test_resistance_rejection_requires_touch_then_subsequent_closed_confirmation():
    engine = SwingTriggerEngine()
    status, context = advance(engine, TradingSide.SELL, rejection_sequence()[:2])
    assert status is SetupStatus.APPROACHING_ZONE
    status, context = advance(
        engine, TradingSide.SELL, rejection_sequence()[:3], status=status, context=context
    )
    assert status is SetupStatus.ZONE_TOUCHED
    assert context.touched_at == rejection_sequence()[2].closed_at
    status, context = advance(
        engine, TradingSide.SELL, rejection_sequence(), status=status, context=context
    )
    assert status is SetupStatus.CONFIRMED and context.execution_valid
    assert context.confirmation_at == rejection_sequence()[3].closed_at
    assert context.confirmation_candle == rejection_sequence()[3]
    assert context.transitions[-1].reason is TriggerReason.RESISTANCE_REJECTION


def test_support_hold_requires_touch_then_subsequent_closed_confirmation():
    engine = SwingTriggerEngine()
    status, context = advance(engine, TradingSide.BUY, support_sequence()[:2])
    assert status is SetupStatus.APPROACHING_ZONE
    status, context = advance(
        engine, TradingSide.BUY, support_sequence()[:3], status=status, context=context
    )
    assert status is SetupStatus.ZONE_TOUCHED
    status, context = advance(
        engine, TradingSide.BUY, support_sequence(), status=status, context=context
    )
    assert status is SetupStatus.CONFIRMED and context.execution_valid
    assert context.transitions[-1].reason is TriggerReason.SUPPORT_HOLD


def test_touched_or_confirmed_rejection_is_invalidated_by_closed_4h_breakout():
    engine = SwingTriggerEngine()
    touched, context = advance(engine, TradingSide.SELL, rejection_sequence()[:3])
    assert touched is SetupStatus.ZONE_TOUCHED
    status, context = engine.evaluate(
        status=touched,
        context=context,
        direction=TradingSide.SELL,
        candles=rejection_sequence()[:3],
        latest_4h_close=D("111"),
        latest_4h_cutoff=rejection_sequence()[2].closed_at,
        invalidation_price=D("110"),
    )
    assert status is SetupStatus.INVALIDATED and not context.execution_valid
    assert context.transitions[-1].reason is TriggerReason.RESISTANCE_BREAKOUT


def test_support_hold_is_invalidated_by_closed_4h_breakdown():
    engine = SwingTriggerEngine()
    touched, context = advance(engine, TradingSide.BUY, support_sequence()[:3])
    status, context = engine.evaluate(
        status=touched,
        context=context,
        direction=TradingSide.BUY,
        candles=support_sequence()[:3],
        latest_4h_close=D("89"),
        latest_4h_cutoff=support_sequence()[2].closed_at,
        invalidation_price=D("90"),
    )
    assert status is SetupStatus.INVALIDATED and not context.execution_valid
    assert context.transitions[-1].reason is TriggerReason.SUPPORT_BREAKDOWN


def test_confirmed_rejection_is_invalidated_by_later_closed_4h_breakout():
    engine = SwingTriggerEngine()
    confirmed, context = advance(engine, TradingSide.SELL, rejection_sequence())
    status, context = engine.evaluate(
        status=confirmed,
        context=context,
        direction=TradingSide.SELL,
        candles=rejection_sequence(),
        latest_4h_close=D("111"),
        latest_4h_cutoff=rejection_sequence()[-1].closed_at,
        invalidation_price=D("110"),
    )
    assert status is SetupStatus.INVALIDATED and not context.execution_valid


def test_confirmed_signal_expires_when_execution_moves_too_far_instead_of_chasing():
    engine = SwingTriggerEngine()
    confirmed, context = advance(engine, TradingSide.SELL, rejection_sequence())
    late = rejection_sequence() + (candle(4, 93, 94, 78, 80),)
    status, context = advance(engine, TradingSide.SELL, late, status=confirmed, context=context)
    assert status is SetupStatus.EXPIRED and not context.execution_valid
    assert context.transitions[-1].reason is TriggerReason.EXECUTION_MOVED_TOO_FAR


def test_same_candles_are_idempotent_and_replay_is_deterministic():
    engine = SwingTriggerEngine()
    first = advance(engine, TradingSide.SELL, rejection_sequence())
    assert (
        advance(
            engine,
            TradingSide.SELL,
            rejection_sequence(),
            status=first[0],
            context=first[1],
        )
        == first
    )
    assert advance(SwingTriggerEngine(), TradingSide.SELL, rejection_sequence()) == first
    identities = {(t.current_state, t.market_cutoff, t.reason) for t in first[1].transitions}
    assert len(identities) == len(first[1].transitions)


def test_trigger_history_rejects_a_discontinuous_transition_chain():
    _, context = advance(SwingTriggerEngine(), TradingSide.SELL, rejection_sequence())
    broken = replace(context.transitions[1], previous_state=SetupStatus.CANDIDATE)
    with pytest.raises(ValueError, match="discontinuous"):
        replace(context, transitions=(context.transitions[0], broken, *context.transitions[2:]))


def test_confirmed_trigger_enters_active_ranking_and_carries_setup_identity():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    setup = next(s for s in active.setups if s.asset == "BTC")
    assert setup.status is SetupStatus.CONFIRMED
    assert setup.id in active.ranked_setup_ids
    buys = [a for a in actions if a.action_type is ActionType.PLACE_LIMIT_ORDER]
    assert buys and all(a.setup_id == setup.id for a in buys)
    deployed = sum((a.target_price * a.quantity_asset for a in buys), D("0"))
    assert deployed == active.recommended_deployment_usd
    assert deployed < risk.reserve.potential_trading_liquidity_usd


def test_touch_without_confirmation_is_update_but_never_a_trade():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    full = daily_asset("BTC", with_trigger="buy")
    evidence = market(
        replace(full, closed_1h_candles=full.closed_1h_candles[:2]), asset("USDC", "1")
    )
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    setup = next(s for s in active.setups if s.asset == "BTC")
    assert setup.status is SetupStatus.ZONE_TOUCHED
    assert active.severity.value == "update"
    assert not any(a.side is not None for a in actions)


def test_missing_1h_history_stays_non_actionable_without_fabricated_confirmation():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    candidate = replace(daily_asset("BTC", with_trigger="buy"), closed_1h_candles=())
    evidence = market(candidate, asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    setup = next(s for s in active.setups if s.asset == "BTC")
    assert setup.status is SetupStatus.CANDIDATE
    assert setup.trigger.confirmation_at is None
    assert setup.id not in active.ranked_setup_ids
    assert active.severity.value == "quiet"
    assert not any(a.side is not None for a in actions)


def test_ai_context_contains_trigger_facts_and_validator_cannot_revive_expired_setup():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    context = json.loads(
        build_context(snapshot, evidence, risk, RecommendationPolicy(), active, actions)
    )
    trigger = context["deterministic_setup_levels"][0]
    assert trigger["trigger_state"] == "confirmed"
    assert trigger["execution_valid"] is True
    setup = active.setups[0]
    late = Candle(CUTOFF + timedelta(hours=1), D("87"), D("90"), D("87"), D("90"))
    status, expired_trigger = SwingTriggerEngine().evaluate(
        status=setup.status,
        context=setup.trigger,
        direction=setup.direction,
        candles=evidence.assets[0].closed_1h_candles + (late,),
        latest_4h_close=evidence.assets[0].latest_closed_price_usd,
        latest_4h_cutoff=evidence.assets[0].latest_closed_candle_at,
        invalidation_price=setup.invalidation_price,
    )
    expired = replace(setup, status=status, trigger=expired_trigger)
    assert expired.status is SetupStatus.EXPIRED
    expired_active = replace(active, setups=(expired,), ranked_setup_ids=())
    assert (
        ActivePlanValidator()
        .validate_active(actions, snapshot, evidence, risk, expired_active)
        .rejected
    )
    mismatched = tuple(
        replace(action, setup_id=uuid4()) if action.setup_id is not None else action
        for action in actions
    )
    assert (
        ActivePlanValidator().validate_active(mismatched, snapshot, evidence, risk, active).rejected
    )


def test_eth_core_still_bounds_confirmed_resistance_rejection():
    snapshot = state((("ETH", "3.45"), ("USDC", "100")))
    snapshot = replace(snapshot, core_positions=(CorePosition("ETH", D("2"), D("2.5")),))
    evidence = market(
        daily_asset("ETH", price="109", close="109", rsi_4h=D("70"), with_trigger="sell"),
        asset("USDC", "1"),
    )
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    validated = ActivePlanValidator().validate_active(actions, snapshot, evidence, risk, active)
    assert not validated.rejected
    sold = sum((a.quantity_asset for a in actions if a.side is TradingSide.SELL), D("0"))
    assert sold > 0 and D("3.45") - sold >= D("2.5")


def test_missed_eth_rejection_expires_and_allocator_does_not_chase_sell():
    snapshot = state((("ETH", "3.45"), ("USDC", "100")))
    snapshot = replace(snapshot, core_positions=(CorePosition("ETH", D("2"), D("2.5")),))
    eth = daily_asset("ETH", price="109", close="109", rsi_4h=D("70"), with_trigger="sell")
    evidence = market(eth, asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    confirmed, first_actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    assert any(a.side is TradingSide.SELL for a in first_actions)

    later = CUTOFF + timedelta(hours=1)
    moved = Candle(later, D("107"), D("107"), D("105.5"), D("106"))
    next_market = replace(
        evidence,
        as_of=later,
        assets=(
            replace(eth, quoted_at=later, closed_1h_candles=eth.closed_1h_candles + (moved,)),
            replace(evidence.assets[1], quoted_at=later),
        ),
    )
    next_risk = PortfolioRiskPolicy().assess(snapshot, next_market, now=later)
    expired, actions = ActiveManagementEngine().evaluate(
        snapshot, next_market, next_risk, previous=confirmed
    )
    setup = next(s for s in expired.setups if s.asset == "ETH")
    assert setup.status is SetupStatus.EXPIRED
    assert setup.id not in expired.ranked_setup_ids
    assert not any(a.side is TradingSide.SELL for a in actions)


def test_eth_4h_breakout_invalidates_rejection_and_removes_stale_sell():
    snapshot = state((("ETH", "3.45"), ("USDC", "100")))
    snapshot = replace(snapshot, core_positions=(CorePosition("ETH", D("2"), D("2.5")),))
    eth = daily_asset("ETH", price="109", close="109", rsi_4h=D("70"), with_trigger="sell")
    evidence = market(eth, asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    confirmed, _ = ActiveManagementEngine().evaluate(snapshot, evidence, risk)

    later = CUTOFF + timedelta(hours=4)
    breakout = replace(
        evidence,
        as_of=later,
        assets=(
            replace(
                eth,
                quoted_at=later,
                latest_closed_candle_at=later,
                latest_closed_price_usd=D("112"),
            ),
            replace(
                evidence.assets[1],
                quoted_at=later,
                latest_closed_candle_at=later,
            ),
        ),
    )
    breakout_risk = PortfolioRiskPolicy().assess(snapshot, breakout, now=later)
    invalidated, actions = ActiveManagementEngine().evaluate(
        snapshot, breakout, breakout_risk, previous=confirmed
    )
    setup = next(s for s in invalidated.setups if s.asset == "ETH")
    assert setup.status is SetupStatus.INVALIDATED
    assert setup.id not in invalidated.ranked_setup_ids
    assert not any(a.side is TradingSide.SELL for a in actions)


def test_sol_support_confirmation_defers_actual_capital_to_phase9_allocator():
    snapshot = state((("BTC", "0.01"), ("SOL", "3.5"), ("USDC", "4000")))
    btc = replace(daily_asset("BTC", with_trigger="buy"), closed_1h_candles=())
    evidence = market(btc, daily_asset("SOL", with_trigger="buy"), asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, actions = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    setup = next(s for s in active.setups if s.asset == "SOL")
    buys = [a for a in actions if a.asset == "SOL" and a.side is TradingSide.BUY]
    assert setup.status is SetupStatus.CONFIRMED
    assert setup.maximum_capital_usd == risk.reserve.potential_trading_liquidity_usd
    assert buys
    allocated = sum((a.target_price * a.quantity_asset for a in buys), D("0"))
    assert allocated == active.recommended_deployment_usd
    assert allocated < setup.maximum_capital_usd


def test_trigger_history_roundtrips_through_temp_db_and_result_codec(tmp_path):
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))

    class Provider:
        async def get_market_snapshot(self, symbols):
            return evidence

    async def run():
        database = Database(tmp_path / "phase10.sqlite")
        await database.initialize()
        snapshots = SQLiteSnapshotRepository(database)
        runs = SQLiteAnalysisRunRepository(database)
        await ConfirmedStateService(snapshots).initialize(snapshot)
        result = await AnalyzeCurrentPortfolio(snapshots, runs, Provider, now=lambda: NOW).execute()
        stored = await runs.latest_completed()
        decoded = AnalysisResultCodec.decode(stored.result_json)
        assert decoded == result
        assert decoded.active.setups[0].trigger.transitions
        report = render_analysis(decoded)
        message = format_analysis(decoded)
        assert "Swing setups" in report and "execution_valid=yes" in report
        assert "Setups" in message and "SUPPORT_HOLD — confirmed" in message

        malformed = json.loads(stored.result_json)
        malformed["data"]["active"]["setups"][0]["trigger"]["confirmation_candle"]["unexpected"] = (
            "rejected"
        )
        with pytest.raises(ValueError, match="Malformed or unsupported"):
            AnalysisResultCodec.decode(json.dumps(malformed))

    asyncio.run(run())


def test_cli_setups_reads_latest_persisted_state_without_market_or_ai(tmp_path):
    path = tmp_path / "phase10-cli.sqlite"
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))

    class Provider:
        async def get_market_snapshot(self, symbols):
            return evidence

    async def persist():
        database = Database(path)
        await database.initialize()
        snapshots = SQLiteSnapshotRepository(database)
        runs = SQLiteAnalysisRunRepository(database)
        await ConfirmedStateService(snapshots).initialize(snapshot)
        return await AnalyzeCurrentPortfolio(snapshots, runs, Provider, now=lambda: NOW).execute()

    result = asyncio.run(persist())
    setup_id = result.active.setups[0].id
    output = []

    def forbidden_provider():
        raise AssertionError("read-only setups command must not call market data")

    assert (
        main(
            ["--db", str(path), "setups"],
            output=output.append,
            provider_factory=forbidden_provider,
            now=lambda: NOW,
        )
        == 0
    )
    assert "CONFIRMED" in output[-1] and str(setup_id) in output[-1]
    assert (
        main(
            ["--db", str(path), "setups", str(setup_id)],
            output=output.append,
            provider_factory=forbidden_provider,
            now=lambda: NOW,
        )
        == 0
    )
    assert str(setup_id) in output[-1]


def test_touched_setup_survives_restart_then_confirms_without_duplicate_transition(tmp_path):
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    full = daily_asset("BTC", with_trigger="buy")
    touched_market = market(
        replace(full, closed_1h_candles=full.closed_1h_candles[:2]),
        asset("USDC", "1"),
    )
    confirmed_market = market(full, asset("USDC", "1"))

    class Provider:
        def __init__(self, evidence):
            self.evidence = evidence

        async def get_market_snapshot(self, symbols):
            assert set(symbols) == {"BTC", "USDC"}
            return self.evidence

    async def run():
        database = Database(tmp_path / "phase10-restart.sqlite")
        await database.initialize()
        snapshots = SQLiteSnapshotRepository(database)
        runs = SQLiteAnalysisRunRepository(database)
        await ConfirmedStateService(snapshots).initialize(snapshot)

        first = await AnalyzeCurrentPortfolio(
            snapshots,
            runs,
            lambda: Provider(touched_market),
            now=lambda: NOW,
        ).execute()
        touched = next(s for s in first.active.setups if s.asset == "BTC")
        assert touched.status is SetupStatus.ZONE_TOUCHED

        # New application/service objects model a process restart over the same SQLite file.
        second = await AnalyzeCurrentPortfolio(
            SQLiteSnapshotRepository(Database(database.path)),
            SQLiteAnalysisRunRepository(Database(database.path)),
            lambda: Provider(confirmed_market),
            now=lambda: NOW + timedelta(minutes=1),
        ).execute()
        confirmed = next(s for s in second.active.setups if s.asset == "BTC")
        assert confirmed.id == touched.id
        assert confirmed.status is SetupStatus.CONFIRMED
        assert confirmed.trigger.transitions[:-1] == touched.trigger.transitions

        third = await AnalyzeCurrentPortfolio(
            SQLiteSnapshotRepository(Database(database.path)),
            SQLiteAnalysisRunRepository(Database(database.path)),
            lambda: Provider(confirmed_market),
            now=lambda: NOW + timedelta(minutes=2),
        ).execute()
        replayed = next(s for s in third.active.setups if s.asset == "BTC")
        assert replayed.id == confirmed.id
        assert replayed.trigger.transitions == confirmed.trigger.transitions
        assert third.recommendations == second.recommendations

    asyncio.run(run())


def test_invalidated_setup_with_related_open_order_produces_bounded_cancel_action():
    existing = order(symbol="BTC", quantity="0.1", price="82")
    snapshot = state((("BTC", "1"), ("USDC", "4000")), (existing,))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, _ = ActiveManagementEngine().evaluate(snapshot, evidence, risk)
    setup = next(s for s in active.setups if s.asset == "BTC")
    assert setup.status is SetupStatus.CONFIRMED
    assert existing.id in setup.related_order_ids

    weak = market(asset("BTC", rsi_4h=D("80")), asset("USDC", "1"))
    weak_risk = PortfolioRiskPolicy().assess(snapshot, weak, now=NOW)
    invalidated, actions = ActiveManagementEngine().evaluate(
        snapshot, weak, weak_risk, previous=active
    )
    retired = next(s for s in invalidated.setups if s.id == setup.id)
    assert retired.status is SetupStatus.INVALIDATED
    cancel = next(a for a in actions if a.action_type is ActionType.CANCEL_LIMIT_ORDER)
    assert cancel.existing_order_id == existing.id
    assert cancel.setup_id == setup.id
    assert invalidated.severity.value == "action"
    assert (
        not ActivePlanValidator()
        .validate_active(actions, snapshot, weak, weak_risk, invalidated)
        .rejected
    )


def test_phase9_v4_result_payload_remains_readable(tmp_path):
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", with_trigger="buy"), asset("USDC", "1"))

    class Provider:
        async def get_market_snapshot(self, symbols):
            return evidence

    async def make_result():
        database = Database(tmp_path / "phase9-compat.sqlite")
        await database.initialize()
        snapshots = SQLiteSnapshotRepository(database)
        runs = SQLiteAnalysisRunRepository(database)
        await ConfirmedStateService(snapshots).initialize(snapshot)
        return await AnalyzeCurrentPortfolio(snapshots, runs, Provider, now=lambda: NOW).execute()

    payload = json.loads(AnalysisResultCodec.encode(asyncio.run(make_result())))
    payload["schema_version"] = 4
    for item in payload["data"]["analyzed_assets"]:
        item.pop("closed_1h_candles")
    payload["data"]["active"]["policy_version"] = 1
    for setup in payload["data"]["active"]["setups"]:
        setup.pop("trigger")
        setup["status"] = "active"
        setup["setup_type"] = "pullback_buy"
    for recommendation in payload["data"]["recommendations"]:
        recommendation.pop("setup_id")

    decoded = AnalysisResultCodec.decode(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    assert decoded.active.policy_version == 1
    assert all(s.trigger is None and s.status is SetupStatus.ACTIVE for s in decoded.active.setups)
    assert all(action.setup_id is None for action in decoded.recommendations)
