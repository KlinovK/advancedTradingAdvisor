"""Versioned spot recommendation predicates. No networking or state writes."""

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from advanced_trading_advisor.domain.numeric import calculation_context, finite_decimal
from advanced_trading_advisor.domain.validation import require
from advanced_trading_advisor.market.models import DataCompleteness, MarketAssetData
from advanced_trading_advisor.market.regime import MarketRegime


@dataclass(frozen=True, slots=True)
class RecommendationPolicy:
    version: int = 1
    max_asset_allocation: Decimal = Decimal("0.40")
    rebalance_allocation: Decimal = Decimal("0.60")
    max_rsi: Decimal = Decimal("65")
    elevated_max_rsi: Decimal = Decimal("55")
    max_24h_gain_pct: Decimal = Decimal("5")
    elevated_max_24h_gain_pct: Decimal = Decimal("2")
    normal_atr_depth: Decimal = Decimal("1")
    elevated_atr_depth: Decimal = Decimal("2")
    replacement_atr_distance: Decimal = Decimal("1")
    quantity_quantum: Decimal = Decimal("0.00000001")
    minimum_order_usd: Decimal = Decimal("10")
    budget_fractions: tuple[tuple[MarketRegime, Decimal], ...] = (
        (MarketRegime.RISK_ON, Decimal("0.25")),
        (MarketRegime.CONSTRUCTIVE, Decimal("0.20")),
        (MarketRegime.NEUTRAL, Decimal("0.15")),
        (MarketRegime.ELEVATED_RISK, Decimal("0.05")),
        (MarketRegime.RISK_OFF, Decimal("0")),
    )

    def __post_init__(self):
        require(
            type(self.version) is int and self.version == 1, "Unsupported recommendation policy."
        )
        for value in (
            self.max_asset_allocation,
            self.rebalance_allocation,
            self.max_rsi,
            self.elevated_max_rsi,
            self.max_24h_gain_pct,
            self.elevated_max_24h_gain_pct,
            self.normal_atr_depth,
            self.elevated_atr_depth,
            self.replacement_atr_distance,
            self.quantity_quantum,
            self.minimum_order_usd,
        ):
            finite_decimal(value, positive=True)
        require(
            0 < self.max_asset_allocation <= self.rebalance_allocation <= 1,
            "Invalid allocation bounds.",
        )
        require(self.elevated_max_rsi <= self.max_rsi <= 100, "Invalid RSI limits.")
        require(self.elevated_atr_depth >= self.normal_atr_depth, "Elevated entry must be deeper.")
        require(self.elevated_max_24h_gain_pct <= self.max_24h_gain_pct, "Invalid no-chase bounds.")
        require(
            isinstance(self.budget_fractions, tuple)
            and len(self.budget_fractions) == len(MarketRegime),
            "One immutable budget fraction per regime required.",
        )
        for regime, fraction in self.budget_fractions:
            require(isinstance(regime, MarketRegime), "Invalid regime.")
            finite_decimal(fraction)
            require(0 <= fraction <= 1, "Invalid budget fraction.")
        require({r for r, _ in self.budget_fractions} == set(MarketRegime), "Duplicate regime.")
        require(
            dict(self.budget_fractions)[MarketRegime.RISK_OFF] == 0, "Risk-off budget must be zero."
        )

    def fraction(self, regime: MarketRegime) -> Decimal:
        return dict(self.budget_fractions)[regime]

    def buy_target(self, asset: MarketAssetData, regime: MarketRegime) -> Decimal | None:
        if (
            regime is MarketRegime.RISK_OFF
            or asset.symbol in {"USDT", "USDC"}
            or asset.completeness is not DataCompleteness.COMPLETE
            or asset.latest_closed_price_usd is None
        ):
            return None
        f = asset.technical_features
        elevated = regime is MarketRegime.ELEVATED_RISK
        rsi_limit = self.elevated_max_rsi if elevated else self.max_rsi
        gain_limit = self.elevated_max_24h_gain_pct if elevated else self.max_24h_gain_pct
        if (
            not asset.latest_closed_price_usd > f.ema20_4h > f.ema50_4h
            or f.rsi_4h > rsi_limit
            or f.change_24h_pct > gain_limit
            or f.change_7d_pct < 0
            or f.atr_4h <= 0
        ):
            return None
        with calculation_context():
            depth = self.elevated_atr_depth if elevated else self.normal_atr_depth
            target = min(f.support, f.ema20_4h, f.ema50_4h, asset.price_usd) - depth * f.atr_4h
            return target if 0 < target < asset.price_usd else None

    def quantity(self, budget: Decimal, price: Decimal) -> Decimal:
        with calculation_context():
            # Floor rather than round up into reserved cash. Quantum is application precision,
            # not an exchange tick/lot-size promise.
            return (budget / price / self.quantity_quantum).to_integral_value(
                rounding=ROUND_DOWN
            ) * self.quantity_quantum
