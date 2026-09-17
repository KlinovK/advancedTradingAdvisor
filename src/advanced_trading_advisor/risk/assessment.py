from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from advanced_trading_advisor.domain.numeric import finite_decimal
from advanced_trading_advisor.domain.validation import require
from advanced_trading_advisor.market.models import DataCompleteness, MarketSnapshot
from advanced_trading_advisor.market.provider import MarketDataPolicy
from advanced_trading_advisor.market.regime import MarketRegimeAssessment, MarketRegimePolicy
from advanced_trading_advisor.portfolio.calculator import PortfolioCalculator, PortfolioValuation
from advanced_trading_advisor.portfolio.models import (
    ConfirmedPortfolioSnapshot,
    StablecoinClassification,
)

from .reserve import StableReserveAssessment, StableReservePolicy


class RiskCondition(StrEnum):
    RESERVE_DEFICIT = "stable_reserve_deficit"
    NO_DEPLOYABLE_STABLES = "no_deployable_stables"
    BUY_OVERCOMMITMENT = "buy_commitments_exceed_stables"
    SELL_OVERCOMMITMENT = "sell_quantity_exceeds_holdings"
    CONCENTRATION = "excessive_asset_concentration"
    INCOMPLETE_DATA = "incomplete_market_data"
    CORE_FLOOR_CONFLICT = "core_floor_conflicts_with_open_sell"


@dataclass(frozen=True, slots=True)
class PortfolioRiskAssessment:
    valuation: PortfolioValuation
    reserve: StableReserveAssessment
    market_regime: MarketRegimeAssessment
    data_completeness: DataCompleteness
    warnings: tuple[RiskCondition, ...]
    blocking_conditions: tuple[RiskCondition, ...]

    @property
    def new_buy_capacity_available(self) -> bool:
        """Necessary capital/data condition only; NOT approval of any recommendation plan."""
        return not self.blocking_conditions


class PortfolioRiskPolicy:
    def __init__(
        self,
        *,
        stables: StablecoinClassification | None = None,
        data_policy: MarketDataPolicy | None = None,
        reserve_policy: StableReservePolicy | None = None,
        concentration_threshold_pct: Decimal = Decimal("50"),
    ):
        finite_decimal(concentration_threshold_pct, positive=True)
        require(concentration_threshold_pct <= 100, "Concentration threshold must be <=100%.")
        self.calculator = PortfolioCalculator(stables=stables, data_policy=data_policy)
        self.regime_policy = MarketRegimePolicy(stables=stables, data_policy=data_policy)
        self.reserve_policy = reserve_policy or StableReservePolicy()
        self.concentration_threshold_pct = concentration_threshold_pct

    def assess(
        self, snapshot: ConfirmedPortfolioSnapshot, market: MarketSnapshot, *, now: datetime
    ) -> PortfolioRiskAssessment:
        valuation = self.calculator.calculate(snapshot, market, now=now)
        regime = self.regime_policy.assess(market, now=now)
        reserve = self.reserve_policy.calculate(
            total_portfolio_usd=valuation.total_portfolio_usd,
            current_stables_usd=valuation.current_stables_usd,
            open_buy_commitments_usd=valuation.open_buy_commitments_usd,
            monthly_expenses_usd=snapshot.financial_settings.monthly_expenses_usd,
            target_expense_runway_months=snapshot.financial_settings.target_expense_runway_months,
            market_regime=regime.regime,
            # Existing stable SELL reservations are not deployable liquidity either.
            explicit_liquidity_buffer_usd=valuation.open_sell_stables_reserved_usd,
        )
        blocks = []
        if reserve.stable_reserve_deficit_usd > 0:
            blocks.append(RiskCondition.RESERVE_DEFICIT)
        if reserve.deployable_stables_usd == 0:
            blocks.append(RiskCondition.NO_DEPLOYABLE_STABLES)
        if valuation.open_buy_commitments_usd > valuation.current_stables_usd:
            blocks.append(RiskCondition.BUY_OVERCOMMITMENT)
        if any(s.exceeds_holdings for s in valuation.open_sell_reservations):
            blocks.append(RiskCondition.SELL_OVERCOMMITMENT)
        cores = {c.symbol: c for c in snapshot.core_positions}
        if any(
            s.symbol in cores and s.held_quantity - s.reserved_quantity < cores[s.symbol].hard_floor
            for s in valuation.open_sell_reservations
        ):
            blocks.append(RiskCondition.CORE_FLOOR_CONFLICT)
        if regime.data_completeness is not DataCompleteness.COMPLETE:
            blocks.append(RiskCondition.INCOMPLETE_DATA)
        warnings = list(blocks)
        if any(a.allocation_pct > self.concentration_threshold_pct for a in valuation.assets):
            warnings.append(RiskCondition.CONCENTRATION)
        return PortfolioRiskAssessment(
            valuation, reserve, regime, regime.data_completeness, tuple(warnings), tuple(blocks)
        )
