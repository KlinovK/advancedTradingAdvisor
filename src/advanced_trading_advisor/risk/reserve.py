from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from advanced_trading_advisor.domain.numeric import calculation_context, finite_decimal
from advanced_trading_advisor.domain.validation import require
from advanced_trading_advisor.market.regime import MarketRegime

ZERO = Decimal("0")


class ReserveComponent(StrEnum):
    EXPENSE = "expense_floor"
    PORTFOLIO_LIQUIDITY = "portfolio_liquidity_floor"
    MARKET_RISK = "market_risk_reserve"


@dataclass(frozen=True, slots=True)
class StableReserveConfig:
    version: int = 2
    liquidity_fractions: tuple[tuple[MarketRegime, Decimal], ...] = (
        (MarketRegime.RISK_ON, Decimal("0.15")),
        (MarketRegime.CONSTRUCTIVE, Decimal("0.18")),
        (MarketRegime.NEUTRAL, Decimal("0.20")),
        (MarketRegime.ELEVATED_RISK, Decimal("0.25")),
        (MarketRegime.RISK_OFF, Decimal("0.30")),
    )
    market_buffer_fractions: tuple[tuple[MarketRegime, Decimal], ...] = (
        (MarketRegime.RISK_ON, Decimal("0")),
        (MarketRegime.CONSTRUCTIVE, Decimal("0.05")),
        (MarketRegime.NEUTRAL, Decimal("0.10")),
        (MarketRegime.ELEVATED_RISK, Decimal("0.20")),
        (MarketRegime.RISK_OFF, Decimal("0.35")),
    )

    def __post_init__(self):
        require(
            type(self.version) is int and self.version in {1, 2},
            "Unsupported reserve policy version.",
        )
        require(
            isinstance(self.liquidity_fractions, tuple), "Liquidity fractions must be immutable."
        )
        require(len(self.liquidity_fractions) == len(MarketRegime), "Each regime needs a fraction.")
        for entry in self.liquidity_fractions:
            require(isinstance(entry, tuple) and len(entry) == 2, "Invalid regime fraction.")
            regime, fraction = entry
            require(isinstance(regime, MarketRegime), "Invalid market regime.")
            finite_decimal(fraction)
            require(0 <= fraction <= 1, "Liquidity fraction must be 0–1.")
        require(
            {r for r, _ in self.liquidity_fractions} == set(MarketRegime),
            "Duplicate or missing regime fraction.",
        )
        require(
            isinstance(self.market_buffer_fractions, tuple)
            and len(self.market_buffer_fractions) == len(MarketRegime),
            "Each regime needs a market buffer.",
        )
        for regime, fraction in self.market_buffer_fractions:
            require(isinstance(regime, MarketRegime), "Invalid buffer regime.")
            finite_decimal(fraction)
            require(0 <= fraction <= 1, "Market buffer fraction must be 0–1.")
        require(
            {r for r, _ in self.market_buffer_fractions} == set(MarketRegime),
            "Duplicate or missing market buffer.",
        )


@dataclass(frozen=True, slots=True)
class StableReserveAssessment:
    policy_version: int
    market_regime: MarketRegime
    total_portfolio_usd: Decimal
    current_stables_usd: Decimal
    open_buy_commitments_usd: Decimal
    explicit_liquidity_buffer_usd: Decimal
    available_uncommitted_stables_usd: Decimal
    monthly_expenses_usd: Decimal
    target_expense_runway_months: Decimal
    actual_expense_runway_months: Decimal
    expense_reserve_floor_usd: Decimal
    portfolio_liquidity_floor_usd: Decimal
    market_risk_reserve_usd: Decimal
    recommended_minimum_stables_usd: Decimal
    deployable_stables_usd: Decimal
    stable_reserve_deficit_usd: Decimal
    dominant_reserve_reason: ReserveComponent
    hard_reserve_usd: Decimal = ZERO
    market_buffer_usd: Decimal = ZERO
    potential_trading_liquidity_usd: Decimal = ZERO


class StableReservePolicy:
    """V2 adds a market buffer to the hard expense floor, never total portfolio value.

    V1 remains decodable/callable for historical policy regression. Open commitments and
    explicitly reserved stable sells are subtracted once before measuring liquidity.
    """

    def __init__(self, config: StableReserveConfig | None = None):
        self.config = config or StableReserveConfig()

    def calculate(
        self,
        *,
        total_portfolio_usd: Decimal,
        current_stables_usd: Decimal,
        open_buy_commitments_usd: Decimal,
        monthly_expenses_usd: Decimal,
        market_regime: MarketRegime,
        target_expense_runway_months: Decimal = Decimal("12"),
        explicit_liquidity_buffer_usd: Decimal = ZERO,
    ) -> StableReserveAssessment:
        for value in (
            total_portfolio_usd,
            current_stables_usd,
            open_buy_commitments_usd,
            explicit_liquidity_buffer_usd,
        ):
            finite_decimal(value)
            require(value >= 0, "Reserve inputs must be nonnegative.")
        finite_decimal(monthly_expenses_usd, positive=True)
        finite_decimal(target_expense_runway_months, positive=True)
        require(current_stables_usd <= total_portfolio_usd, "Stable value exceeds total value.")
        require(isinstance(market_regime, MarketRegime), "Invalid market regime.")
        with calculation_context():
            available = max(
                current_stables_usd - open_buy_commitments_usd - explicit_liquidity_buffer_usd, ZERO
            )
            expenses = monthly_expenses_usd * target_expense_runway_months
            if self.config.version == 2:
                liquidity = ZERO
                market = expenses * dict(self.config.market_buffer_fractions)[market_regime]
                minimum = expenses + market
                dominant = ReserveComponent.MARKET_RISK if market else ReserveComponent.EXPENSE
            else:
                liquidity = (
                    total_portfolio_usd * dict(self.config.liquidity_fractions)[market_regime]
                )
                market = ZERO
                minimum = max(expenses, liquidity)
                dominant = (
                    ReserveComponent.EXPENSE
                    if expenses == minimum
                    else ReserveComponent.PORTFOLIO_LIQUIDITY
                )
            potential = max(available - minimum, ZERO)
            return StableReserveAssessment(
                self.config.version,
                market_regime,
                total_portfolio_usd,
                current_stables_usd,
                open_buy_commitments_usd,
                explicit_liquidity_buffer_usd,
                available,
                monthly_expenses_usd,
                target_expense_runway_months,
                available / monthly_expenses_usd,
                expenses,
                liquidity,
                market,
                minimum,
                potential,
                max(minimum - available, ZERO),
                dominant,
                expenses,
                market,
                potential,
            )
