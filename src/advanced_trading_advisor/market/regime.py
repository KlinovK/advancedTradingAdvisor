from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from advanced_trading_advisor.domain.numeric import calculation_context, finite_decimal
from advanced_trading_advisor.domain.validation import require
from advanced_trading_advisor.portfolio.models import StablecoinClassification

from .errors import MarketDataError
from .models import DailyTrend, DataCompleteness, MarketSnapshot
from .provider import MarketDataPolicy
from .validation import validate_cutoffs, validate_snapshot_quotes


class MarketRegime(StrEnum):
    RISK_ON = "risk_on"
    CONSTRUCTIVE = "constructive"
    NEUTRAL = "neutral"
    ELEVATED_RISK = "elevated_risk"
    RISK_OFF = "risk_off"


@dataclass(frozen=True, slots=True)
class MarketRegimeConfig:
    version: int = 1
    risk_on_min_rsi: Decimal = Decimal("50")
    overbought_rsi: Decimal = Decimal("75")
    oversold_rsi: Decimal = Decimal("35")
    risk_off_change_7d_pct: Decimal = Decimal("-10")
    high_atr_pct: Decimal = Decimal("5")
    risk_on_breadth: Decimal = Decimal("0.60")

    def __post_init__(self):
        require(
            self.version == 1 and type(self.version) is int, "Unsupported regime policy version."
        )
        for value in (
            self.risk_on_min_rsi,
            self.overbought_rsi,
            self.oversold_rsi,
            self.risk_off_change_7d_pct,
            self.high_atr_pct,
            self.risk_on_breadth,
        ):
            finite_decimal(value)
        require(
            0 <= self.oversold_rsi < self.risk_on_min_rsi < self.overbought_rsi <= 100,
            "Invalid RSI thresholds.",
        )
        require(
            self.risk_off_change_7d_pct < 0
            and self.high_atr_pct > 0
            and 0 < self.risk_on_breadth <= 1,
            "Invalid regime thresholds.",
        )


@dataclass(frozen=True, slots=True)
class MarketRegimeAssessment:
    regime: MarketRegime
    policy_version: int
    reason: str
    data_completeness: DataCompleteness


class MarketRegimePolicy:
    """Version 1: ordered, explainable predicates; no weighted score.

    Invalid/stale data -> RISK_OFF. Missing features/BTC/common cutoff -> ELEVATED_RISK.
    BTC bearish EMA stack + (oversold RSI or large weekly loss) -> RISK_OFF.
    Bearish stack, high ATR or overbought RSI -> ELEVATED_RISK.
    Bullish BTC stack + nonnegative weekly change + RSI band + breadth -> RISK_ON.
    Bullish BTC stack + nonnegative weekly change -> CONSTRUCTIVE; otherwise NEUTRAL.
    Prices used for technical comparisons are CLOSED prices, never fresh quote prices.
    """

    def __init__(
        self,
        config: MarketRegimeConfig | None = None,
        *,
        data_policy: MarketDataPolicy | None = None,
        stables: StablecoinClassification | None = None,
    ):
        self.config = config or MarketRegimeConfig()
        self.data_policy = data_policy or MarketDataPolicy()
        self.stables = stables or StablecoinClassification()

    def assess(self, market: MarketSnapshot, *, now: datetime) -> MarketRegimeAssessment:
        config = self.config
        policy_version = config.version

        def result(regime, reason, completeness=market.completeness):
            return MarketRegimeAssessment(regime, policy_version, reason, completeness)

        try:
            validate_snapshot_quotes(market, now=now, policy=self.data_policy)
            validate_cutoffs(market, now=now, policy=self.data_policy)
        except MarketDataError:
            return result(
                MarketRegime.RISK_OFF, "invalid_or_stale_market_data", DataCompleteness.UNAVAILABLE
            )
        assets = tuple(a for a in market.assets if not self.stables.is_stable(a.symbol))
        btc = next((a for a in assets if a.symbol == "BTC"), None)
        if (
            market.completeness is not DataCompleteness.COMPLETE
            or btc is None
            or any(a.latest_closed_price_usd is None for a in assets)
        ):
            return result(
                MarketRegime.ELEVATED_RISK, "insufficient_regime_evidence", DataCompleteness.PARTIAL
            )
        f, close = btc.technical_features, btc.latest_closed_price_usd
        daily = btc.daily_context.trend
        if daily is not DailyTrend.INCOMPLETE:
            policy_version = 2
        with calculation_context():
            bearish = close < f.ema20_4h < f.ema50_4h
            bullish = close > f.ema20_4h > f.ema50_4h
            if bearish and (
                f.rsi_4h <= config.oversold_rsi or f.change_7d_pct <= config.risk_off_change_7d_pct
            ):
                return result(MarketRegime.RISK_OFF, "btc_bearish_with_downside_confirmation")
            if (
                bearish
                or f.atr_4h / close * 100 >= config.high_atr_pct
                or f.rsi_4h >= config.overbought_rsi
            ):
                return result(MarketRegime.ELEVATED_RISK, "btc_bearish_volatile_or_overbought")
            breadth = Decimal(
                sum(
                    a.latest_closed_price_usd
                    > a.technical_features.ema20_4h
                    > a.technical_features.ema50_4h
                    for a in assets
                )
            ) / Decimal(len(assets))
            if bullish and f.change_7d_pct >= 0:
                if daily is DailyTrend.BEARISH:
                    return result(MarketRegime.NEUTRAL, "daily_bearish_blocks_4h_bullish_upgrade")
                if f.rsi_4h >= config.risk_on_min_rsi and breadth >= config.risk_on_breadth:
                    if daily is DailyTrend.MIXED:
                        return result(MarketRegime.CONSTRUCTIVE, "daily_mixed_limits_risk_on")
                    return result(MarketRegime.RISK_ON, "btc_bullish_with_breadth")
                return result(MarketRegime.CONSTRUCTIVE, "btc_bullish_without_full_confirmation")
            return result(MarketRegime.NEUTRAL, "mixed_technical_evidence")
