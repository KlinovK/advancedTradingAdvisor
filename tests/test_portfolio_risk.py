import asyncio
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from uuid import UUID, uuid4

import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.market.errors import MarketDataError, StaleMarketDataError
from advanced_trading_advisor.market.models import (
    DataCompleteness,
    MarketAssetData,
    MarketSnapshot,
    TechnicalFeatures,
)
from advanced_trading_advisor.market.regime import (
    MarketRegime,
    MarketRegimeConfig,
    MarketRegimePolicy,
)
from advanced_trading_advisor.market.service import MarketDataService
from advanced_trading_advisor.portfolio.calculator import PortfolioCalculator, market_symbols
from advanced_trading_advisor.portfolio.models import (
    DEFAULT_MANUAL_ACCOUNT_ID,
    DEFAULT_MANUAL_ACCOUNT_NAME,
    Account,
    AccountType,
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    FinancialSettings,
    LimitOrder,
    LimitOrderStatus,
    Portfolio,
    TradingSide,
)
from advanced_trading_advisor.risk.assessment import PortfolioRiskPolicy, RiskCondition
from advanced_trading_advisor.risk.reserve import (
    ReserveComponent,
    StableReserveConfig,
    StableReservePolicy,
)

D = Decimal
NOW = datetime(2026, 9, 9, 12, 30, tzinfo=UTC)
CUTOFF = NOW.replace(minute=0)


def asset(symbol="BTC", price="100", close="100", **feature_changes):
    features = TechnicalFeatures(
        D("1"), D("3"), D("55"), D("95"), D("90"), D("2"), D("85"), D("110")
    )
    return MarketAssetData(
        symbol,
        D(price),
        NOW,
        CUTOFF,
        replace(features, **feature_changes),
        DataCompleteness.COMPLETE,
        D(close),
    )


def state(positions=(("BTC", "100"), ("USDC", "4000")), orders=()):
    return ConfirmedPortfolioSnapshot(
        uuid4(),
        NOW,
        Portfolio(
            (
                Account(
                    DEFAULT_MANUAL_ACCOUNT_ID,
                    DEFAULT_MANUAL_ACCOUNT_NAME,
                    AccountType.MANUAL,
                    tuple(AssetPosition(s, D(q)) for s, q in positions),
                ),
            )
        ),
        FinancialSettings(D("250")),
        orders,
    )


def test_valuation_uses_derived_balances_from_multiple_accounts() -> None:
    original = state(())
    manual = Account(
        DEFAULT_MANUAL_ACCOUNT_ID,
        DEFAULT_MANUAL_ACCOUNT_NAME,
        AccountType.MANUAL,
        (AssetPosition("BTC", D("0.8")), AssetPosition("USDC", D("100"))),
    )
    wallet = Account(
        UUID("22222222-2222-4222-8222-222222222222"),
        "Wallet",
        AccountType.EXTERNAL_WALLET,
        (AssetPosition("BTC", D("2.7")), AssetPosition("USDC", D("50"))),
    )
    snapshot = replace(original, portfolio=Portfolio((manual, wallet)))

    valuation = PortfolioCalculator().calculate(
        snapshot, market(asset("BTC", price="100"), asset("USDC", price="1")), now=NOW
    )

    assert valuation.assets[0].symbol == "BTC"
    assert valuation.assets[0].quantity == D("3.5")
    assert valuation.total_portfolio_usd == D("500")
    assert valuation.current_stables_usd == D("150")
    assert snapshot.portfolio.accounts == (manual, wallet)


def order(
    side=TradingSide.BUY, symbol="SOL", quantity="2", price="200", status=LimitOrderStatus.OPEN
):
    return LimitOrder(
        uuid4(),
        symbol,
        side,
        D(price),
        D(quantity),
        status,
        NOW,
        NOW,
        None if status is LimitOrderStatus.OPEN else NOW,
        account_id=DEFAULT_MANUAL_ACCOUNT_ID,
    )


def market(*assets):
    return MarketSnapshot(NOW, tuple(assets), is_live=True)


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, MarketRegime.RISK_ON),
        ({"rsi_4h": D("45")}, MarketRegime.CONSTRUCTIVE),
        ({"ema20_4h": D("100")}, MarketRegime.NEUTRAL),
        ({"rsi_4h": D("75")}, MarketRegime.ELEVATED_RISK),
        ({"atr_4h": D("5")}, MarketRegime.ELEVATED_RISK),
        ({"ema20_4h": D("105"), "ema50_4h": D("110"), "rsi_4h": D("30")}, MarketRegime.RISK_OFF),
        (
            {"ema20_4h": D("105"), "ema50_4h": D("110"), "change_7d_pct": D("-10")},
            MarketRegime.RISK_OFF,
        ),
        ({"ema20_4h": D("105"), "ema50_4h": D("110")}, MarketRegime.ELEVATED_RISK),
    ],
)
def test_regime_explicit_rules(changes, expected):
    result = MarketRegimePolicy().assess(market(asset(**changes)), now=NOW)
    assert result.regime is expected
    assert result.policy_version == 1
    assert result.reason


def test_regime_breadth_uses_only_non_stables_and_closed_prices():
    policy = MarketRegimePolicy()
    btc = asset(price="1")  # Live quote disagreement must not change the technical regime.
    bearish = asset("ETH", close="80")
    assert policy.assess(market(btc, bearish), now=NOW).regime is MarketRegime.CONSTRUCTIVE
    assert (
        policy.assess(market(btc, asset("USDC", close="1")), now=NOW).regime is MarketRegime.RISK_ON
    )


@pytest.mark.parametrize("kind", ["missing_btc", "price_only", "missing_close", "empty"])
def test_missing_evidence_is_conservative(kind):
    value = {
        "missing_btc": market(asset("ETH")),
        "price_only": market(MarketAssetData("BTC", D("100"), NOW)),
        "missing_close": market(replace(asset(), latest_closed_price_usd=None)),
        "empty": market(),
    }[kind]
    result = MarketRegimePolicy().assess(value, now=NOW)
    assert result.regime is MarketRegime.ELEVATED_RISK
    assert result.data_completeness is not DataCompleteness.COMPLETE


def test_stale_and_incompatible_cutoffs_fail_conservatively():
    policy = MarketRegimePolicy()
    assert (
        policy.assess(market(asset()), now=NOW + timedelta(hours=1)).regime is MarketRegime.RISK_OFF
    )
    value = market(
        asset(), replace(asset("ETH"), latest_closed_candle_at=CUTOFF - timedelta(hours=4))
    )
    assert value.effective_market_cutoff is None
    assert value.completeness is DataCompleteness.PARTIAL
    assert policy.assess(value, now=NOW).regime is MarketRegime.RISK_OFF


def test_regime_configuration_changes_thresholds():
    policy = MarketRegimePolicy(MarketRegimeConfig(risk_on_min_rsi=D("60")))
    assert policy.assess(market(asset()), now=NOW).regime is MarketRegime.CONSTRUCTIVE
    with pytest.raises(DomainValidationError):
        MarketRegimeConfig(risk_on_breadth=D("2"))


def test_valuation_union_depegged_stables_and_orders():
    orders = (
        order(),
        order(TradingSide.SELL, "BTC", "1", "120"),
        order(TradingSide.SELL, "BTC", "2", "110"),
        order(symbol="IGNORED", status=LimitOrderStatus.CANCELLED),
    )
    snapshot = state((("BTC", "10"), ("USDC", "1000"), ("USDT", "1000")), orders)
    assert market_symbols(snapshot) == ("BTC", "SOL", "USDC", "USDT")
    value = PortfolioCalculator().calculate(
        snapshot,
        market(asset(), asset("USDC", "0.98"), asset("USDT", "1.02"), asset("SOL", "150")),
        now=NOW,
    )
    assert value.total_portfolio_usd == D("3000")
    assert value.current_stables_usd == D("2000")
    assert value.non_stable_crypto_usd == D("1000")
    assert abs(value.stable_allocation_pct - D("66.666666666666666666666666666666666667")) < D(
        "1e-33"
    )
    assert sum(a.market_value_usd for a in value.assets) == value.total_portfolio_usd
    assert value.open_buy_commitments_usd == D("400")  # Limit 200, not live 150.
    sell = value.open_sell_reservations[0]
    assert sell.reserved_quantity == D("3")
    assert sell.market_notional_usd == D("300")
    assert sell.limit_notional_usd == D("340")
    assert not sell.exceeds_holdings
    assert snapshot.portfolio.positions[0].amount == D("10")  # Pure calculation.


def test_missing_open_order_quote_and_stale_quote_fail():
    snapshot = state(orders=(order(),))
    with pytest.raises(MarketDataError, match="missing"):
        PortfolioCalculator().calculate(snapshot, market(asset(), asset("USDC", "1")), now=NOW)
    with pytest.raises(StaleMarketDataError):
        PortfolioCalculator().calculate(
            state(), market(asset(), asset("USDC", "1")), now=NOW + timedelta(minutes=11)
        )


def test_empty_portfolio_and_sell_without_holdings():
    empty = PortfolioCalculator().calculate(state(()), market(), now=NOW)
    assert empty.total_portfolio_usd == empty.stable_allocation_pct == 0
    snapshot = state((), (order(TradingSide.SELL),))
    result = PortfolioCalculator().calculate(snapshot, market(asset("SOL")), now=NOW)
    assert result.open_sell_reservations[0].exceeds_holdings
    assert result.open_sell_reservations[0].held_quantity == 0


def reserve(**changes):
    arguments = dict(
        total_portfolio_usd=D("14000"),
        current_stables_usd=D("4000"),
        open_buy_commitments_usd=D("0"),
        monthly_expenses_usd=D("250"),
        market_regime=MarketRegime.NEUTRAL,
    )
    # Historical V1 behavior remains callable for stored-run compatibility.
    return StableReservePolicy(StableReserveConfig(version=1)).calculate(**(arguments | changes))


@pytest.mark.parametrize(
    "commitment,available,deployable,runway",
    [
        ("0", "4000", "1000", "16"),
        ("400", "3600", "600", "14.4"),
        ("1000", "3000", "0", "12"),
        ("5000", "0", "0", "0"),
    ],
)
def test_requested_expense_floor_examples(commitment, available, deployable, runway):
    result = reserve(open_buy_commitments_usd=D(commitment))
    assert result.expense_reserve_floor_usd == D("3000")
    assert result.portfolio_liquidity_floor_usd == D("2800")
    assert result.recommended_minimum_stables_usd == D("3000")
    assert result.available_uncommitted_stables_usd == D(available)
    assert result.deployable_stables_usd == D(deployable)
    assert result.actual_expense_runway_months == D(runway)
    assert result.dominant_reserve_reason is ReserveComponent.EXPENSE
    assert result.market_risk_reserve_usd == 0


@pytest.mark.parametrize("regime,fraction", list(StableReserveConfig().liquidity_fractions))
def test_every_regime_liquidity_floor(regime, fraction):
    result = reserve(total_portfolio_usd=D("100000"), market_regime=regime)
    assert result.portfolio_liquidity_floor_usd == D("100000") * fraction
    assert result.recommended_minimum_stables_usd == result.portfolio_liquidity_floor_usd
    assert result.dominant_reserve_reason is ReserveComponent.PORTFOLIO_LIQUIDITY
    assert result.stable_reserve_deficit_usd == result.recommended_minimum_stables_usd - D("4000")
    assert result.deployable_stables_usd == 0


def test_reserve_deficit_buffer_and_exact_arithmetic():
    result = reserve(explicit_liquidity_buffer_usd=D("1500"))
    assert result.stable_reserve_deficit_usd == D("500")
    assert result.available_uncommitted_stables_usd == D("2500")
    with localcontext() as context:
        context.prec = 4
        result = reserve(
            total_portfolio_usd=D("100.123456789"),
            current_stables_usd=D("80.123456789"),
            monthly_expenses_usd=D("0.123456789"),
            target_expense_runway_months=D("12.5"),
            open_buy_commitments_usd=D("0.000000001"),
        )
        assert result.available_uncommitted_stables_usd == D("80.123456788")
        assert result.expense_reserve_floor_usd == D("1.5432098625")
        assert result.portfolio_liquidity_floor_usd == D("20.0246913578")
        assert result.deployable_stables_usd == D("60.0987654302")
        assert context.prec == 4
    with pytest.raises(FrozenInstanceError):
        result.deployable_stables_usd = D("999")


def test_reserve_zero_portfolio_tie_and_custom_fractions():
    empty = reserve(total_portfolio_usd=D("0"), current_stables_usd=D("0"))
    assert empty.stable_reserve_deficit_usd == D("3000")
    tie = reserve(total_portfolio_usd=D("15000"))
    assert tie.dominant_reserve_reason is ReserveComponent.EXPENSE
    config = StableReserveConfig(
        version=1, liquidity_fractions=tuple((r, D("0.4")) for r in MarketRegime)
    )
    value = StableReservePolicy(config).calculate(
        total_portfolio_usd=D("10000"),
        current_stables_usd=D("5000"),
        open_buy_commitments_usd=D("0"),
        monthly_expenses_usd=D("1"),
        market_regime=MarketRegime.NEUTRAL,
    )
    assert value.recommended_minimum_stables_usd == D("4000")


@pytest.mark.parametrize(
    "changes",
    [
        {"monthly_expenses_usd": D("0")},
        {"current_stables_usd": D("-1")},
        {"current_stables_usd": D("14001")},
        {"target_expense_runway_months": D("0")},
        {"open_buy_commitments_usd": D("-1")},
        {"total_portfolio_usd": D("NaN")},
        {"explicit_liquidity_buffer_usd": 1.5},
        {"market_regime": "neutral"},
    ],
)
def test_reserve_invalid_inputs(changes):
    with pytest.raises(DomainValidationError):
        reserve(**changes)


def test_risk_deficit_and_concentration_block_capacity():
    snapshot = state(orders=(order(quantity="6"),))
    result = PortfolioRiskPolicy().assess(
        snapshot, market(asset(), asset("USDC", "1"), asset("SOL")), now=NOW
    )
    assert result.reserve.stable_reserve_deficit_usd == D("200")
    assert RiskCondition.RESERVE_DEFICIT in result.blocking_conditions
    assert RiskCondition.CONCENTRATION in result.warnings
    assert not result.new_buy_capacity_available


def test_healthy_risk_and_incomplete_warning():
    snapshot = state((("BTC", "30"), ("ETH", "30"), ("USDC", "4000")))
    value = market(asset(), asset("ETH"), asset("USDC", "1"))
    result = PortfolioRiskPolicy().assess(snapshot, value, now=NOW)
    assert result.new_buy_capacity_available
    assert result.warnings == ()
    assert result.reserve.deployable_stables_usd == D("1000")
    assert result.valuation.stable_allocation_pct == D("40")
    partial = replace(
        value, assets=tuple(MarketAssetData(a.symbol, a.price_usd, NOW) for a in value.assets)
    )
    result = PortfolioRiskPolicy().assess(snapshot, partial, now=NOW)
    assert RiskCondition.INCOMPLETE_DATA in result.blocking_conditions
    assert not result.new_buy_capacity_available


def test_sell_stables_not_deployable_and_overcommitment_flags():
    value = market(asset(), asset("USDC", "1"), asset("SOL"))
    snapshot = state(orders=(order(TradingSide.SELL, "USDC", "1500", "1"),))
    result = PortfolioRiskPolicy().assess(snapshot, value, now=NOW)
    assert result.reserve.explicit_liquidity_buffer_usd == D("1500")
    assert result.reserve.available_uncommitted_stables_usd == D("2500")
    bad = state(orders=(order(quantity="30"), order(TradingSide.SELL, "BTC", "101")))
    result = PortfolioRiskPolicy().assess(bad, value, now=NOW)
    assert RiskCondition.BUY_OVERCOMMITMENT in result.blocking_conditions
    assert RiskCondition.SELL_OVERCOMMITMENT in result.blocking_conditions


def test_closed_orders_have_no_commitments():
    orders = tuple(order(status=s) for s in LimitOrderStatus if s is not LimitOrderStatus.OPEN)
    result = PortfolioCalculator().calculate(
        state(orders=orders), market(asset(), asset("USDC", "1")), now=NOW
    )
    assert result.open_buy_commitments_usd == 0
    assert result.open_sell_reservations == ()


def test_market_service_fetches_open_order_asset_and_checks_coverage():
    class Provider:
        requested = None
        omit_sol = False

        async def get_market_snapshot(self, symbols):
            self.requested = symbols
            return market(*(asset(s) for s in symbols if not (self.omit_sol and s == "SOL")))

    provider = Provider()
    snapshot = state(orders=(order(),))
    result = asyncio.run(MarketDataService(provider).for_snapshot(snapshot))
    assert provider.requested == ("BTC", "SOL", "USDC")
    assert {a.symbol for a in result.assets} == set(provider.requested)
    provider.omit_sol = True
    with pytest.raises(MarketDataError, match="coverage"):
        asyncio.run(MarketDataService(provider).for_snapshot(snapshot))
