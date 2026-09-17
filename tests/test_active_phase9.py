"""Offline Phase 9 invariants; never touches the default DB or live providers."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from test_portfolio_risk import CUTOFF, NOW, asset, market, order, state

from advanced_trading_advisor.ai.context import build_context
from advanced_trading_advisor.analysis.active import (
    ActiveManagementEngine,
    ActiveManagementPolicy,
    AssetDecisionType,
    SetupStatus,
    TelegramSeverity,
)
from advanced_trading_advisor.analysis.plan import ActivePlanValidator
from advanced_trading_advisor.analysis.policy import RecommendationPolicy
from advanced_trading_advisor.analysis.recommendations import ActionType
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.analysis.use_case import AnalyzeCurrentPortfolio
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.cli.report import render_analysis
from advanced_trading_advisor.market.models import Candle, DailyContext, DailyTrend
from advanced_trading_advisor.market.regime import MarketRegime
from advanced_trading_advisor.market.technical_features import TechnicalFeatureCalculator
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import (
    CorePosition,
    FinancialSettings,
    LimitOrderStatus,
    TradingSide,
)
from advanced_trading_advisor.risk.assessment import PortfolioRiskPolicy
from advanced_trading_advisor.risk.reserve import StableReservePolicy
from advanced_trading_advisor.storage.codecs import SnapshotCodec
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)

D = Decimal


def daily_asset(symbol="BTC", **changes):
    trigger_kind = changes.pop("with_trigger", None)
    value = asset(symbol, **changes)
    if trigger_kind is None:
        trigger_kind = (
            "sell"
            if value.technical_features.rsi_4h is not None and value.technical_features.rsi_4h >= 65
            else "buy"
        )
    atr = value.technical_features.atr_4h
    if trigger_kind == "sell":
        level = value.technical_features.resistance
        hourly = (
            Candle(
                CUTOFF - timedelta(hours=2),
                level - atr,
                level - atr,
                level - atr * D("1.5"),
                level - atr * D("1.25"),
            ),
            Candle(
                CUTOFF - timedelta(hours=1),
                level - atr * D("0.5"),
                level,
                level - atr,
                level - atr * D("0.25"),
            ),
            Candle(
                CUTOFF,
                level - atr * D("0.25"),
                level,
                level - atr,
                level - atr * D("0.6"),
            ),
        )
    else:
        level = value.technical_features.support
        hourly = (
            Candle(
                CUTOFF - timedelta(hours=2),
                level + atr * D("1.25"),
                level + atr * D("1.5"),
                level + atr,
                level + atr * D("1.25"),
            ),
            Candle(
                CUTOFF - timedelta(hours=1),
                level + atr * D("0.5"),
                level + atr,
                level,
                level + atr * D("0.25"),
            ),
            Candle(
                CUTOFF,
                level + atr * D("0.25"),
                level + atr,
                level - atr * D("0.1"),
                level + atr * D("0.6"),
            ),
        )
    return replace(
        value,
        daily_context=DailyContext(
            NOW.replace(hour=0, minute=0),
            D("100"),
            D("90"),
            None,
            D("80"),
            D("120"),
            DailyTrend.BULLISH,
        ),
        closed_1h_candles=hourly,
    )


def decision(snapshot, evidence, *, previous=None, previous_regime=None):
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    active, actions = ActiveManagementEngine().evaluate(
        snapshot, evidence, risk, previous, previous_regime
    )
    validated = ActivePlanValidator().validate_active(actions, snapshot, evidence, risk, active)
    assert not validated.rejected
    return risk, active, actions


def test_reserve_v2_hard_floor_buffer_and_unused_dry_powder():
    result = StableReservePolicy().calculate(
        total_portfolio_usd=D("10000"),
        current_stables_usd=D("2983"),
        open_buy_commitments_usd=D("0"),
        monthly_expenses_usd=D("220"),
        target_expense_runway_months=D("6"),
        market_regime=MarketRegime.NEUTRAL,
    )
    assert result.policy_version == 2
    assert result.hard_reserve_usd == D("1320")
    assert result.market_buffer_usd == D("132")
    assert result.recommended_minimum_stables_usd == D("1452")
    assert result.potential_trading_liquidity_usd == D("1531")


def test_core_persists_and_preferred_excess_is_not_forced_sell():
    snapshot = state((("ETH", "3.45"), ("BTC", "1"), ("USDC", "2983")))
    snapshot = replace(
        snapshot,
        financial_settings=FinancialSettings(D("220"), D("6")),
        core_positions=(CorePosition("ETH", D("2"), D("2.5")),),
    )
    assert SnapshotCodec.decode(SnapshotCodec.encode(snapshot)) == snapshot
    evidence = market(daily_asset("BTC"), daily_asset("ETH"), asset("USDC", "1"))
    _, active, actions = decision(snapshot, evidence)
    eth = next(a for a in active.asset_decisions if a.asset == "ETH")
    assert eth.core_floor == 2 and eth.preferred_core == D("2.5")
    assert eth.tradable_quantity == D("0.95")
    assert not any(a.asset == "ETH" and a.side is TradingSide.SELL for a in actions)


def test_ranked_buy_preserves_dry_powder_and_no_trade_is_valid():
    snapshot = state((("BTC", "1"), ("SOL", "1"), ("USDC", "2983")))
    snapshot = replace(snapshot, financial_settings=FinancialSettings(D("220"), D("6")))
    evidence = market(daily_asset("BTC"), daily_asset("SOL"), asset("USDC", "1"))
    risk, active, actions = decision(snapshot, evidence)
    assert active.ranked_setup_ids
    assert active.recommended_deployment_usd > 0
    assert active.recommended_deployment_usd < risk.reserve.potential_trading_liquidity_usd
    assert active.strategy_dry_powder_usd > 0
    assert active.severity is TelegramSeverity.ACTION
    assert all(
        a.target_price in {s.limit_price for s in active.setups}
        for a in actions
        if a.action_type is ActionType.PLACE_LIMIT_ORDER
    )
    weak = market(asset("BTC", rsi_4h=D("80")), asset("SOL", rsi_4h=D("80")), asset("USDC", "1"))
    _, quiet, quiet_actions = decision(snapshot, weak)
    assert quiet.recommended_deployment_usd == 0
    assert quiet.severity is TelegramSeverity.QUIET
    assert all(a.side is None for a in quiet_actions)


def test_setup_identity_persists_and_invalidates_without_inferred_fill():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC"), asset("USDC", "1"))
    _, first, _ = decision(snapshot, evidence)
    later = NOW + timedelta(hours=4)
    next_market = replace(
        evidence,
        as_of=later,
        assets=tuple(
            replace(a, quoted_at=later, latest_closed_candle_at=CUTOFF + timedelta(hours=4))
            if a.latest_closed_candle_at
            else replace(a, quoted_at=later)
            for a in evidence.assets
        ),
    )
    next_risk = PortfolioRiskPolicy().assess(snapshot, next_market, now=later)
    second, _ = ActiveManagementEngine().evaluate(snapshot, next_market, next_risk, first)
    assert first.ranked_setup_ids == second.ranked_setup_ids
    weak = market(asset("BTC", rsi_4h=D("80")), asset("USDC", "1"))
    _, third, _ = decision(snapshot, weak, previous=second)
    assert any(s.status is SetupStatus.INVALIDATED for s in third.setups)


def test_setup_becomes_terminal_only_after_manual_order_status_confirmation():
    for order_status, setup_status in (
        (LimitOrderStatus.FILLED, SetupStatus.FILLED_OR_CONFIRMED_MANUALLY),
        (LimitOrderStatus.CANCELLED, SetupStatus.CANCELLED),
    ):
        existing = order(symbol="BTC", quantity="1", price="82")
        snapshot = state((("BTC", "1"), ("USDC", "4000")), (existing,))
        evidence = market(daily_asset("BTC"), asset("USDC", "1"))
        _, first, _ = decision(snapshot, evidence)
        setup = next(s for s in first.setups if s.asset == "BTC")
        assert setup.status is SetupStatus.CONFIRMED
        assert setup.related_order_ids == (existing.id,)

        unchanged_risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
        unchanged, _ = ActiveManagementEngine().evaluate(snapshot, evidence, unchanged_risk, first)
        assert next(s for s in unchanged.setups if s.id == setup.id).status is SetupStatus.CONFIRMED

        resolved_order = replace(existing, status=order_status, resolved_at=NOW)
        confirmed = replace(snapshot, orders=(resolved_order,))
        confirmed_risk = PortfolioRiskPolicy().assess(confirmed, evidence, now=NOW)
        after_confirmation, _ = ActiveManagementEngine().evaluate(
            confirmed, evidence, confirmed_risk, unchanged
        )
        assert next(s for s in after_confirmation.setups if s.id == setup.id).status is setup_status


def test_material_structural_change_retires_old_setup_identity():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    first_market = market(daily_asset("BTC"), asset("USDC", "1"))
    _, first, _ = decision(snapshot, first_market)
    changed = market(daily_asset("BTC", support=D("75")), asset("USDC", "1"))
    _, second, _ = decision(snapshot, changed, previous=first)
    assert any(s.status is SetupStatus.INVALIDATED for s in second.setups)
    assert first.ranked_setup_ids[0] != second.ranked_setup_ids[0]


def test_core_floor_rejects_aggregate_sell_inventory():
    snapshot = state((("ETH", "3.45"), ("BTC", "1"), ("USDC", "4000")))
    snapshot = replace(snapshot, core_positions=(CorePosition("ETH", D("2"), D("2.5")),))
    evidence = market(daily_asset("BTC"), daily_asset("ETH"), asset("USDC", "1"))
    risk, active, _ = decision(snapshot, evidence)
    from advanced_trading_advisor.analysis.engine import advice

    sell = advice(
        snapshot.id,
        "ETH",
        ActionType.PLACE_LIMIT_ORDER,
        "unsafe",
        side=TradingSide.SELL,
        price=D("110"),
        quantity=D("1.5"),
    )
    assert ActivePlanValidator().validate_active((sell,), snapshot, evidence, risk, active).rejected


def test_small_existing_order_level_change_keeps_order():
    existing = order(symbol="BTC", quantity="1", price="82")
    snapshot = state((("BTC", "1"), ("USDC", "4000")), (existing,))
    evidence = market(daily_asset("BTC"), asset("USDC", "1"))
    _, _, actions = decision(snapshot, evidence)
    assert any(a.action_type is ActionType.KEEP_LIMIT_ORDER for a in actions)
    assert not any(a.action_type is ActionType.REPLACE_LIMIT_ORDER for a in actions)


def test_daily_context_uses_complete_closed_utc_days_only():
    end = datetime(2026, 9, 9, 12, tzinfo=UTC)
    candles = tuple(
        Candle(end - timedelta(hours=4 * (150 - index)), D("100"), D("102"), D("98"), D("100"))
        for index in range(151)
    )
    context = TechnicalFeatureCalculator().daily_context(candles)
    assert context.latest_closed_at is not None
    assert context.latest_closed_at.hour == 0
    assert context.ema20 is not None
    assert context.ema50 is None  # 30-day Demo 4H history cannot supply 50 daily closes.
    assert context.trend is not DailyTrend.INCOMPLETE


def test_high_eth_concentration_without_exit_is_informational_only():
    snapshot = state((("ETH", "3.45"), ("USDC", "100")))
    snapshot = replace(
        snapshot,
        financial_settings=FinancialSettings(D("10"), D("6")),
        core_positions=(CorePosition("ETH", D("2"), D("2.5")),),
    )
    evidence = market(daily_asset("BTC", rsi_4h=D("80")), daily_asset("ETH"), asset("USDC", "1"))
    _, active, actions = decision(snapshot, evidence)
    eth = next(a for a in active.asset_decisions if a.asset == "ETH")
    assert eth.decision is AssetDecisionType.REBALANCE_INFORMATIONAL
    assert any(a.action_type is ActionType.REBALANCE_INFORMATIONAL for a in actions)
    assert not any(a.side is TradingSide.SELL for a in actions)


def test_eth_profit_taking_uses_resistance_and_preserves_preferred_core():
    snapshot = state((("ETH", "3.45"), ("USDC", "100")))
    snapshot = replace(
        snapshot,
        financial_settings=FinancialSettings(D("10"), D("6")),
        core_positions=(CorePosition("ETH", D("2"), D("2.5")),),
    )
    evidence = market(
        daily_asset("BTC", rsi_4h=D("80")),
        daily_asset("ETH", price="109", close="109", rsi_4h=D("70")),
        asset("USDC", "1"),
    )
    _, active, actions = decision(snapshot, evidence)
    eth = next(a for a in active.asset_decisions if a.asset == "ETH")
    sells = [a for a in actions if a.asset == "ETH" and a.side is TradingSide.SELL]
    assert eth.decision is AssetDecisionType.PARTIAL_SELL
    assert len(sells) == 1 and sells[0].target_price == D("110")
    assert D("3.45") - sells[0].quantity_asset >= D("2.5")
    assert active.severity is TelegramSeverity.ACTION
    setup = next(s for s in active.setups if s.asset == "ETH" and s.direction is TradingSide.SELL)
    assert setup.price_source == "4h_resistance"
    assert setup.risk_reward > D("1.5")


def test_poor_reward_or_missing_levels_never_fabricate_a_buy():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    poor = market(daily_asset("BTC", resistance=D("85.5")), asset("USDC", "1"))
    _, active, actions = decision(snapshot, poor)
    assert not active.ranked_setup_ids
    assert not any(a.side is TradingSide.BUY for a in actions)


def test_structural_invalidation_cancels_but_small_move_keeps():
    existing = order(symbol="BTC", quantity="1", price="90")
    snapshot = state((("BTC", "1"), ("USDC", "4000")), (existing,))
    strong = market(daily_asset("BTC"), asset("USDC", "1"))
    _, active, actions = decision(snapshot, strong)
    assert any(a.action_type is ActionType.REPLACE_LIMIT_ORDER for a in actions)
    assert active.severity is TelegramSeverity.ACTION
    broken = market(daily_asset("BTC", price="80"), asset("USDC", "1"))
    _, _, cancelled = decision(snapshot, broken)
    assert any(a.action_type is ActionType.CANCEL_LIMIT_ORDER for a in cancelled)


def test_multiple_candidates_share_portfolio_budget_in_ranked_order():
    snapshot = state((("BTC", "1"), ("SOL", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC"), daily_asset("SOL"), asset("USDC", "1"))
    risk, active, actions = decision(snapshot, evidence)
    buys = [
        a
        for a in actions
        if a.action_type is ActionType.PLACE_LIMIT_ORDER and a.side is TradingSide.BUY
    ]
    assert {a.asset for a in buys} == {"BTC", "SOL"}
    assert len(active.ranked_setup_ids) >= 2
    assert sum(a.target_price * a.quantity_asset for a in buys) == active.recommended_deployment_usd
    assert active.recommended_deployment_usd <= risk.reserve.potential_trading_liquidity_usd / 4
    assert active.strategy_dry_powder_usd > active.recommended_deployment_usd


def test_meaningful_no_trade_regime_change_is_update():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(asset("BTC", rsi_4h=D("80")), asset("USDC", "1"))
    _, active, actions = decision(snapshot, evidence, previous_regime=MarketRegime.NEUTRAL)
    assert active.severity is TelegramSeverity.UPDATE
    assert not any(a.side is not None for a in actions)


def test_daily_bearish_blocks_4h_bullish_regime_upgrade():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    btc = daily_asset("BTC")
    btc = replace(btc, daily_context=replace(btc.daily_context, trend=DailyTrend.BEARISH))
    risk = PortfolioRiskPolicy().assess(snapshot, market(btc, asset("USDC", "1")), now=NOW)
    assert risk.market_regime.policy_version == 2
    assert risk.market_regime.regime is MarketRegime.NEUTRAL


def test_temp_db_active_result_history_and_confirmed_state_unchanged(tmp_path):
    snapshot = state((("BTC", "1"), ("SOL", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC"), daily_asset("SOL"), asset("USDC", "1"))

    class StaticProvider:
        async def get_market_snapshot(self, symbols):
            assert set(symbols) == {"BTC", "SOL", "USDC"}
            return evidence

    async def run():
        db = Database(tmp_path / "phase9-smoke.sqlite")
        await db.initialize()
        snapshots = SQLiteSnapshotRepository(db)
        runs = SQLiteAnalysisRunRepository(db)
        saved = await ConfirmedStateService(snapshots).initialize(snapshot)
        result = await AnalyzeCurrentPortfolio(
            snapshots,
            runs,
            StaticProvider,
            now=lambda: NOW,
        ).execute()
        assert result.active is not None
        assert result.financial.recommended_deployment_usd > 0
        assert await snapshots.get_current() == saved
        stored = await runs.latest_completed()
        recovered = AnalysisResultCodec.decode(stored.result_json)
        assert recovered == result
        report = render_analysis(recovered)
        assert "Swing setups" in report and "Strategy dry powder:" in report
        assert "Telegram severity: ACTION" in report

    asyncio.run(run())


def test_cli_core_configuration_requires_explicit_confirmation_and_is_audited(tmp_path):
    path = tmp_path / "core-config.sqlite"
    output = []
    common = dict(now=lambda: NOW, output=output.append)
    assert (
        main(
            [
                "--db",
                str(path),
                "setup",
                "--position",
                "ETH=3.45",
                "--position",
                "USDC=2983",
                "--expenses",
                "220",
                "--runway",
                "6",
            ],
            input_fn=lambda _: "yes",
            **common,
        )
        == 0
    )
    assert (
        main(
            [
                "--db",
                str(path),
                "portfolio",
                "core",
                "--asset",
                "ETH",
                "--floor",
                "2",
                "--preferred",
                "2.5",
            ],
            input_fn=lambda _: "no",
            **common,
        )
        == 0
    )

    async def read():
        db = Database(path)
        current = await SQLiteSnapshotRepository(db).get_current()
        assert current.revision == 1 and not current.snapshot.core_positions

    asyncio.run(read())
    assert (
        main(
            [
                "--db",
                str(path),
                "portfolio",
                "core",
                "--asset",
                "ETH",
                "--floor",
                "2",
                "--preferred",
                "2.5",
            ],
            input_fn=lambda _: "yes",
            **common,
        )
        == 0
    )

    async def verify():
        db = Database(path)
        current = await SQLiteSnapshotRepository(db).get_current()
        assert current.revision == 2
        assert current.snapshot.core_positions == (CorePosition("ETH", D("2"), D("2.5")),)

    asyncio.run(verify())


def test_setup_score_is_decomposed_and_policy_weights_are_configurable():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC"), asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    policy = ActiveManagementPolicy(
        score_weights=(
            ("1d_trend", 15),
            ("4h_structure", 15),
            ("level_proximity", 15),
            ("risk_reward", 20),
            ("market_regime", 10),
            ("portfolio_fit", 10),
            ("trigger_confirmation", 15),
        )
    )
    active, _ = ActiveManagementEngine(policy).evaluate(snapshot, evidence, risk)
    score = active.setups[0].score
    assert score.version == 1 and score.total == sum(p for _, p, _ in score.components)
    assert dict((name, maximum) for name, _, maximum in score.components)["1d_trend"] == 15


def test_ai_context_sees_core_and_only_validated_levels():
    snapshot = state((("BTC", "1"), ("ETH", "3.45"), ("USDC", "4000")))
    snapshot = replace(snapshot, core_positions=(CorePosition("ETH", D("2"), D("2.5")),))
    evidence = market(daily_asset("BTC"), daily_asset("ETH"), asset("USDC", "1"))
    risk, active, actions = decision(snapshot, evidence)
    import json

    context = json.loads(
        build_context(snapshot, evidence, risk, RecommendationPolicy(), active, actions)
    )
    eth = next(a for a in context["assets"] if a["symbol"] == "ETH")
    assert eth["core_hard_floor"] == "2"
    assert eth["core_preferred_quantity"] == "2.5"
    assert eth["max_candidate_sell_quantity"] == "0.95"
    assert context["deterministic_setup_levels"]
    assert all(
        a["target_price"] is None
        or a["target_price"] in {s["limit_price"] for s in context["deterministic_setup_levels"]}
        for a in context["allowed_action_terms"]
    )


def test_active_validator_rejects_arbitrary_sell_level_even_with_inventory():
    snapshot = state((("ETH", "3.45"), ("USDC", "100")))
    snapshot = replace(
        snapshot,
        financial_settings=FinancialSettings(D("10"), D("6")),
        core_positions=(CorePosition("ETH", D("2"), D("2.5")),),
    )
    evidence = market(
        daily_asset("BTC", rsi_4h=D("80")),
        daily_asset("ETH", price="109", close="109", rsi_4h=D("70")),
        asset("USDC", "1"),
    )
    risk, active, actions = decision(snapshot, evidence)
    sell = next(a for a in actions if a.side is TradingSide.SELL)
    arbitrary = replace(sell, target_price=D("111"))
    assert (
        ActivePlanValidator()
        .validate_active((arbitrary,), snapshot, evidence, risk, active)
        .rejected
    )


def test_open_sell_conflicting_with_core_is_flagged_and_cancelled():
    existing = order(side=TradingSide.SELL, symbol="ETH", quantity="2", price="110")
    snapshot = state((("ETH", "3.45"), ("USDC", "4000")), (existing,))
    snapshot = replace(snapshot, core_positions=(CorePosition("ETH", D("2"), D("2.5")),))
    evidence = market(daily_asset("BTC", rsi_4h=D("80")), daily_asset("ETH"), asset("USDC", "1"))
    risk, active, actions = decision(snapshot, evidence)
    assert any(w.value == "core_floor_conflicts_with_open_sell" for w in risk.warnings)
    assert active.severity is TelegramSeverity.ACTION
    assert any(
        a.action_type is ActionType.CANCEL_LIMIT_ORDER and a.existing_order_id == existing.id
        for a in actions
    )


def test_strong_near_support_buy_can_use_three_deterministic_levels():
    snapshot = state((("BTC", "1"), ("USDC", "4000")))
    evidence = market(daily_asset("BTC", price="90"), asset("USDC", "1"))
    risk, active, actions = decision(snapshot, evidence)
    setup = next(s for s in active.setups if s.asset == "BTC")
    buys = [a for a in actions if a.asset == "BTC" and a.side is TradingSide.BUY]
    assert len(setup.levels) == len(buys) == 3
    assert [a.target_price for a in buys] == [D("83"), D("81"), D("79")]
    assert [level.priority for level in setup.levels] == [1, 2, 3]
    assert all(level.source.startswith("4h_") for level in setup.levels)
    assert sum(a.target_price * a.quantity_asset for a in buys) <= (
        risk.reserve.potential_trading_liquidity_usd / 4
    )


def test_large_profit_taking_setup_can_use_sell_ladder_above_preferred_core():
    snapshot = state((("ETH", "20"), ("USDC", "100")))
    snapshot = replace(
        snapshot,
        financial_settings=FinancialSettings(D("10"), D("6")),
        core_positions=(CorePosition("ETH", D("2"), D("2.5")),),
    )
    evidence = market(
        daily_asset("BTC", rsi_4h=D("80")),
        daily_asset("ETH", price="109", close="109", rsi_4h=D("70")),
        asset("USDC", "1"),
    )
    _, active, actions = decision(snapshot, evidence)
    setup = next(s for s in active.setups if s.asset == "ETH" and s.direction is TradingSide.SELL)
    sells = [a for a in actions if a.asset == "ETH" and a.side is TradingSide.SELL]
    assert len(setup.levels) == len(sells) == 3
    assert [a.target_price for a in sells] == [D("110"), D("112"), D("114")]
    assert D("20") - sum(a.quantity_asset for a in sells) >= D("2.5")
