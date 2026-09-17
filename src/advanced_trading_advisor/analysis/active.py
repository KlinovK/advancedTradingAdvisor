"""Versioned, deterministic spot-only portfolio decisions; no execution or state mutation."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from uuid import NAMESPACE_URL, UUID, uuid5

from advanced_trading_advisor.domain.numeric import calculation_context, finite_decimal
from advanced_trading_advisor.domain.validation import identity, require, symbol, timestamp
from advanced_trading_advisor.market.models import DailyTrend, MarketAssetData, MarketSnapshot
from advanced_trading_advisor.market.regime import MarketRegime
from advanced_trading_advisor.portfolio.models import (
    ConfirmedPortfolioSnapshot,
    LimitOrderStatus,
    TradingSide,
)
from advanced_trading_advisor.risk.assessment import PortfolioRiskAssessment

from .engine import advice
from .policy import RecommendationPolicy
from .recommendations import ActionType, Recommendation
from .triggers import (
    SetupStatus,
    SwingTriggerEngine,
    SwingTriggerPolicy,
    TriggerContext,
)

ZERO = Decimal("0")


class AssetDecisionType(StrEnum):
    ADD = "add"
    HOLD = "hold"
    WAIT = "wait"
    PARTIAL_SELL = "partial_sell"
    SELL = "sell"
    REBALANCE_INFORMATIONAL = "rebalance_informational"


class SetupType(StrEnum):
    PULLBACK_BUY = "pullback_buy"
    PROFIT_TAKING_SELL = "profit_taking_sell"
    SUPPORT_HOLD = "support_hold"
    RESISTANCE_REJECTION = "resistance_rejection"


class TelegramSeverity(StrEnum):
    QUIET = "quiet"
    UPDATE = "update"
    ACTION = "action"


@dataclass(frozen=True, slots=True)
class SetupScore:
    version: int
    components: tuple[tuple[str, int, int], ...]

    def __post_init__(self):
        require(type(self.version) is int and self.version == 1, "Unsupported score version.")
        require(
            len({name for name, _, _ in self.components}) == len(self.components),
            "Duplicate score component.",
        )
        require(
            all(
                type(points) is int and type(maximum) is int and 0 <= points <= maximum
                for _, points, maximum in self.components
            ),
            "Invalid score component.",
        )
        require(
            all(
                isinstance(name, str) and bool(name.strip()) and maximum > 0
                for name, _, maximum in self.components
            ),
            "Invalid score component name or weight.",
        )
        require(sum(maximum for _, _, maximum in self.components) == 100, "Score must total 100.")

    @property
    def total(self) -> int:
        return sum(points for _, points, _ in self.components)


@dataclass(frozen=True, slots=True)
class SetupLevel:
    price: Decimal
    source: str
    priority: int

    def __post_init__(self):
        finite_decimal(self.price, positive=True)
        require(
            isinstance(self.source, str) and bool(self.source.strip()), "Level source required."
        )
        require(
            type(self.priority) is int and 1 <= self.priority <= 3, "Level priority must be 1–3."
        )


@dataclass(frozen=True, slots=True)
class TradeSetup:
    id: UUID
    asset: str
    direction: TradingSide
    setup_type: SetupType
    created_at: datetime
    originating_cutoff: datetime
    status: SetupStatus
    limit_price: Decimal
    price_source: str
    invalidation_price: Decimal
    invalidation_source: str
    target_price: Decimal
    target_source: str
    risk_reward: Decimal
    maximum_capital_usd: Decimal
    score: SetupScore
    related_order_ids: tuple[UUID, ...] = ()
    levels: tuple[SetupLevel, ...] = ()
    trigger: TriggerContext | None = None

    def __post_init__(self):
        identity(self.id)
        symbol(self.asset)
        timestamp(self.created_at)
        timestamp(self.originating_cutoff)
        require(isinstance(self.direction, TradingSide), "Invalid setup direction.")
        require(isinstance(self.setup_type, SetupType), "Invalid setup type.")
        require(isinstance(self.status, SetupStatus), "Invalid setup status.")
        for value in (
            self.limit_price,
            self.invalidation_price,
            self.target_price,
            self.risk_reward,
        ):
            finite_decimal(value, positive=True)
        finite_decimal(self.maximum_capital_usd)
        require(self.maximum_capital_usd >= 0, "Invalid setup capital ceiling.")
        for source in (self.price_source, self.invalidation_source, self.target_source):
            require(isinstance(source, str) and bool(source.strip()), "Level needs a source.")
        require(isinstance(self.score, SetupScore), "Invalid setup score.")
        require(
            len(set(self.related_order_ids)) == len(self.related_order_ids),
            "Duplicate related order ID.",
        )
        for order_id in self.related_order_ids:
            identity(order_id)
        require(
            1 <= len(self.levels) <= 3 and all(isinstance(x, SetupLevel) for x in self.levels),
            "A setup requires one to three deterministic levels.",
        )
        require(
            self.levels[0].price == self.limit_price
            and len({x.price for x in self.levels}) == len(self.levels),
            "Setup levels must be distinct and start at the primary limit.",
        )
        if self.trigger is not None:
            require(isinstance(self.trigger, TriggerContext), "Invalid trigger context.")
            if self.trigger.transitions:
                require(
                    self.trigger.transitions[-1].current_state is self.status,
                    "Setup status disagrees with trigger history.",
                )
            else:
                require(
                    self.status is SetupStatus.CANDIDATE, "Unstarted trigger must be candidate."
                )
            require(
                self.setup_type in {SetupType.SUPPORT_HOLD, SetupType.RESISTANCE_REJECTION},
                "Temporal trigger attached to unsupported setup type.",
            )
            require(
                self.trigger.execution_valid == (self.status is SetupStatus.CONFIRMED),
                "Execution validity disagrees with trigger state.",
            )


@dataclass(frozen=True, slots=True)
class AssetDecision:
    asset: str
    decision: AssetDecisionType
    quantity: Decimal
    market_value_usd: Decimal
    allocation_percent: Decimal
    core_floor: Decimal | None
    preferred_core: Decimal | None
    tradable_quantity: Decimal
    daily_trend: DailyTrend
    setup_id: UUID | None

    def __post_init__(self):
        symbol(self.asset)
        require(isinstance(self.decision, AssetDecisionType), "Invalid asset decision.")
        require(isinstance(self.daily_trend, DailyTrend), "Invalid daily trend.")
        for value in (
            self.quantity,
            self.market_value_usd,
            self.allocation_percent,
            self.tradable_quantity,
        ):
            finite_decimal(value)
            require(value >= 0, "Asset decision values must be nonnegative.")
        require(
            self.allocation_percent <= 100 and self.tradable_quantity <= self.quantity,
            "Invalid position allocation or tradable quantity.",
        )
        for value in (self.core_floor, self.preferred_core):
            if value is not None:
                finite_decimal(value, positive=True)
        require(
            (self.core_floor is None) == (self.preferred_core is None),
            "Core floor and preference must appear together.",
        )
        if self.core_floor is not None:
            require(
                self.core_floor <= self.quantity and self.core_floor <= self.preferred_core,
                "Invalid confirmed core position.",
            )
        if self.setup_id is not None:
            identity(self.setup_id)


@dataclass(frozen=True, slots=True)
class ActiveAnalysis:
    policy_version: int
    setups: tuple[TradeSetup, ...]
    asset_decisions: tuple[AssetDecision, ...]
    ranked_setup_ids: tuple[UUID, ...]
    recommended_deployment_usd: Decimal
    strategy_dry_powder_usd: Decimal
    severity: TelegramSeverity

    def __post_init__(self):
        require(
            type(self.policy_version) is int and self.policy_version in {1, 2},
            "Unsupported active policy version.",
        )
        require(all(isinstance(s, TradeSetup) for s in self.setups), "Invalid setups.")
        require(
            all(isinstance(d, AssetDecision) for d in self.asset_decisions),
            "Invalid asset decisions.",
        )
        require(len({s.id for s in self.setups}) == len(self.setups), "Duplicate setup identity.")
        require(
            len({d.asset for d in self.asset_decisions}) == len(self.asset_decisions),
            "Duplicate asset decision.",
        )
        active_ids = {
            s.id for s in self.setups if s.status in {SetupStatus.ACTIVE, SetupStatus.CONFIRMED}
        }
        require(
            len(set(self.ranked_setup_ids)) == len(self.ranked_setup_ids)
            and set(self.ranked_setup_ids) <= active_ids,
            "Invalid setup ranking.",
        )
        for value in (self.recommended_deployment_usd, self.strategy_dry_powder_usd):
            finite_decimal(value)
            require(value >= 0, "Capital decisions must be nonnegative.")
        require(isinstance(self.severity, TelegramSeverity), "Invalid message severity.")


@dataclass(frozen=True, slots=True)
class ActiveManagementPolicy:
    version: int = 2
    minimum_risk_reward: Decimal = Decimal("1.5")
    material_price_change_fraction: Decimal = Decimal("0.02")
    estimated_fee_fraction: Decimal = Decimal("0.001")
    estimated_slippage_fraction: Decimal = Decimal("0.001")
    max_asset_allocation: Decimal = Decimal("0.40")
    minimum_order_usd: Decimal = Decimal("10")
    strong_score: int = 80
    actionable_score: int = 65
    monitor_score: int = 50
    deployment_fractions: tuple[tuple[MarketRegime, Decimal], ...] = (
        (MarketRegime.RISK_ON, Decimal("0.25")),
        (MarketRegime.CONSTRUCTIVE, Decimal("0.20")),
        (MarketRegime.NEUTRAL, Decimal("0.15")),
        (MarketRegime.ELEVATED_RISK, Decimal("0.05")),
        (MarketRegime.RISK_OFF, Decimal("0")),
    )
    score_weights: tuple[tuple[str, int], ...] = (
        ("1d_trend", 15),
        ("4h_structure", 15),
        ("level_proximity", 15),
        ("risk_reward", 20),
        ("market_regime", 10),
        ("portfolio_fit", 10),
        ("trigger_confirmation", 15),
    )
    trigger_policy: SwingTriggerPolicy = SwingTriggerPolicy()
    trigger_score_fractions: tuple[tuple[SetupStatus, Decimal], ...] = (
        (SetupStatus.CANDIDATE, Decimal("0")),
        (SetupStatus.APPROACHING_ZONE, Decimal("0.25")),
        (SetupStatus.ZONE_TOUCHED, Decimal("0.50")),
        (SetupStatus.AWAITING_CONFIRMATION, Decimal("0.50")),
        (SetupStatus.CONFIRMED, Decimal("1")),
    )

    def __post_init__(self):
        require(type(self.version) is int and self.version in {1, 2}, "Unsupported active policy.")
        for value in (
            self.minimum_risk_reward,
            self.material_price_change_fraction,
            self.estimated_fee_fraction,
            self.estimated_slippage_fraction,
            self.max_asset_allocation,
            self.minimum_order_usd,
        ):
            finite_decimal(value, positive=True)
        require(self.max_asset_allocation <= 1, "Invalid allocation cap.")
        require(
            0 < self.monitor_score < self.actionable_score < self.strong_score <= 100,
            "Invalid score thresholds.",
        )
        require(
            {regime for regime, _ in self.deployment_fractions} == set(MarketRegime),
            "Missing regime deployment fraction.",
        )
        for _, fraction in self.deployment_fractions:
            finite_decimal(fraction)
            require(0 <= fraction <= 1, "Invalid deployment fraction.")
        require(
            {name for name, _ in self.score_weights}
            == {
                "1d_trend",
                "4h_structure",
                "level_proximity",
                "risk_reward",
                "market_regime",
                "portfolio_fit",
                "trigger_confirmation",
            }
            and sum(weight for _, weight in self.score_weights) == 100
            and all(type(weight) is int and weight > 0 for _, weight in self.score_weights),
            "Score weights must be positive, complete and total 100.",
        )
        require(isinstance(self.trigger_policy, SwingTriggerPolicy), "Invalid trigger policy.")
        require(
            {state for state, _ in self.trigger_score_fractions}
            == {
                SetupStatus.CANDIDATE,
                SetupStatus.APPROACHING_ZONE,
                SetupStatus.ZONE_TOUCHED,
                SetupStatus.AWAITING_CONFIRMATION,
                SetupStatus.CONFIRMED,
            },
            "Missing trigger score state.",
        )
        for state, fraction in self.trigger_score_fractions:
            require(isinstance(state, SetupStatus), "Invalid trigger score state.")
            finite_decimal(fraction)
            require(0 <= fraction <= 1, "Invalid trigger score fraction.")

    def fraction(self, regime: MarketRegime) -> Decimal:
        return dict(self.deployment_fractions)[regime]


def _score(
    asset: MarketAssetData,
    regime: MarketRegime,
    allocation: Decimal,
    rr: Decimal,
    policy: ActiveManagementPolicy,
    *,
    selling: bool = False,
    trigger_state: SetupStatus = SetupStatus.CANDIDATE,
) -> SetupScore:
    weights = dict(policy.score_weights)
    f = asset.technical_features
    closed = asset.latest_closed_price_usd
    daily = asset.daily_context.trend
    daily_points = (
        weights["1d_trend"]
        if daily is DailyTrend.BULLISH
        else weights["1d_trend"] // 2
        if daily is DailyTrend.MIXED
        else 0
    )
    tactical = (
        weights["4h_structure"]
        if (selling and f.rsi_4h is not None and f.rsi_4h >= 65)
        or (
            not selling
            and closed is not None
            and f.ema20_4h is not None
            and f.ema50_4h is not None
            and closed > f.ema20_4h > f.ema50_4h
            and f.rsi_4h is not None
            and f.rsi_4h <= 65
        )
        else 0
    )
    proximity = (
        weights["level_proximity"]
        if f.atr_4h is not None
        and f.atr_4h > 0
        and (
            selling
            and f.resistance is not None
            and 0 <= f.resistance - asset.price_usd <= f.atr_4h
            or not selling
            and f.support is not None
            and 0 <= asset.price_usd - f.support <= 4 * f.atr_4h
        )
        else 0
    )
    reward = (
        weights["risk_reward"]
        if rr >= 2
        else weights["risk_reward"] * 3 // 4
        if rr >= policy.minimum_risk_reward
        else 0
    )
    regime_fraction = {
        MarketRegime.RISK_ON: Decimal("1"),
        MarketRegime.CONSTRUCTIVE: Decimal("0.8"),
        MarketRegime.NEUTRAL: Decimal("0.5"),
        MarketRegime.ELEVATED_RISK: Decimal("0.2"),
        MarketRegime.RISK_OFF: Decimal("0"),
    }[regime]
    if selling:
        regime_fraction = Decimal("1") - regime_fraction / 2
    regime_points = int(weights["market_regime"] * regime_fraction)
    if selling:
        fit = weights["portfolio_fit"] if allocation >= 50 else weights["portfolio_fit"] // 2
    else:
        fit = (
            weights["portfolio_fit"]
            if allocation <= 30
            else weights["portfolio_fit"] // 2
            if allocation <= 40
            else 0
        )
    trigger_fraction = dict(policy.trigger_score_fractions).get(trigger_state, Decimal("0"))
    trigger_points = int(weights["trigger_confirmation"] * trigger_fraction)
    return SetupScore(
        1,
        (
            ("1d_trend", daily_points, weights["1d_trend"]),
            ("4h_structure", tactical, weights["4h_structure"]),
            (
                "resistance_proximity" if selling else "support_proximity",
                proximity,
                weights["level_proximity"],
            ),
            ("risk_reward", reward, weights["risk_reward"]),
            ("market_regime", regime_points, weights["market_regime"]),
            ("portfolio_fit", fit, weights["portfolio_fit"]),
            (
                "trigger_confirmation",
                trigger_points,
                weights["trigger_confirmation"],
            ),
        ),
    )


def _with_trigger_score(
    score: SetupScore, state: SetupStatus, policy: ActiveManagementPolicy
) -> SetupScore:
    fraction = dict(policy.trigger_score_fractions).get(state, Decimal("0"))
    return SetupScore(
        score.version,
        tuple(
            (name, int(maximum * fraction), maximum)
            if name == "trigger_confirmation"
            else (name, points, maximum)
            for name, points, maximum in score.components
        ),
    )


def _severity(
    actions: tuple[Recommendation, ...],
    risk: PortfolioRiskAssessment,
    setups: tuple[TradeSetup, ...],
    previous_regime: MarketRegime | None,
    previous_warnings: tuple[str, ...] | None,
) -> TelegramSeverity:
    active = {
        ActionType.BUY,
        ActionType.SELL,
        ActionType.PLACE_LIMIT_ORDER,
        ActionType.CANCEL_LIMIT_ORDER,
        ActionType.REPLACE_LIMIT_ORDER,
    }
    if risk.reserve.stable_reserve_deficit_usd > 0 or any(a.action_type in active for a in actions):
        return TelegramSeverity.ACTION
    if previous_regime is not None and previous_regime is not risk.market_regime.regime:
        return TelegramSeverity.UPDATE
    if any(
        s.trigger is not None
        and s.trigger.transitions
        and s.trigger.transitions[-1].current_state
        in {SetupStatus.ZONE_TOUCHED, SetupStatus.AWAITING_CONFIRMATION, SetupStatus.EXPIRED}
        for s in setups
    ):
        return TelegramSeverity.UPDATE
    if previous_warnings is not None and set(previous_warnings) - {"candidate_plan_rejected"} != {
        w.value for w in risk.warnings
    }:
        return TelegramSeverity.UPDATE
    return TelegramSeverity.QUIET


class ActiveManagementEngine:
    """Generate/rank all setups before allocating a shared, intentionally partial cash budget."""

    def __init__(
        self,
        policy: ActiveManagementPolicy | None = None,
        *,
        terms_policy: RecommendationPolicy | None = None,
    ):
        self.policy = policy or ActiveManagementPolicy()
        self.legacy_terms = terms_policy or RecommendationPolicy()
        self.trigger_engine = SwingTriggerEngine(self.policy.trigger_policy)

    def evaluate(
        self,
        snapshot: ConfirmedPortfolioSnapshot,
        market: MarketSnapshot,
        risk: PortfolioRiskAssessment,
        previous: ActiveAnalysis | None = None,
        previous_regime: MarketRegime | None = None,
        previous_warnings: tuple[str, ...] | None = None,
    ) -> tuple[ActiveAnalysis, tuple[Recommendation, ...]]:
        with calculation_context():
            return self._evaluate(
                snapshot, market, risk, previous, previous_regime, previous_warnings
            )

    def _evaluate(self, snapshot, market, risk, previous, previous_regime, previous_warnings):
        p = self.policy
        regime = risk.market_regime.regime
        values = {v.symbol: v for v in risk.valuation.assets}
        holdings = {v.symbol: v.quantity for v in risk.valuation.assets}
        cores = {c.symbol: c for c in snapshot.core_positions}
        prior_candidates = (
            {
                (s.asset, s.setup_type): s
                for s in previous.setups
                if s.status
                in {
                    SetupStatus.CANDIDATE,
                    SetupStatus.APPROACHING_ZONE,
                    SetupStatus.ZONE_TOUCHED,
                    SetupStatus.AWAITING_CONFIRMATION,
                    SetupStatus.CONFIRMED,
                    SetupStatus.ACTIVE,
                }
            }
            if previous
            else {}
        )
        terminal_prior = []
        terminal_keys = set()
        prior = {}
        for key, old in prior_candidates.items():
            confirmed_status = next(
                (
                    o.status
                    for o in snapshot.orders
                    if o.id in old.related_order_ids
                    and o.status in {LimitOrderStatus.FILLED, LimitOrderStatus.CANCELLED}
                ),
                None,
            )
            if confirmed_status is LimitOrderStatus.FILLED:
                terminal_status = SetupStatus.FILLED_OR_CONFIRMED_MANUALLY
                if old.trigger is not None:
                    _, trigger = self.trigger_engine.record_manual_transition(
                        status=old.status,
                        context=old.trigger,
                        current=terminal_status,
                        confirmed_at=snapshot.confirmed_at,
                        reference_price=old.limit_price,
                    )
                    old = replace(old, status=terminal_status, trigger=trigger)
                else:
                    old = replace(old, status=terminal_status)
                terminal_prior.append(old)
                terminal_keys.add(key)
            elif confirmed_status is LimitOrderStatus.CANCELLED:
                terminal_status = SetupStatus.CANCELLED
                if old.trigger is not None:
                    _, trigger = self.trigger_engine.record_manual_transition(
                        status=old.status,
                        context=old.trigger,
                        current=terminal_status,
                        confirmed_at=snapshot.confirmed_at,
                        reference_price=old.limit_price,
                    )
                    old = replace(old, status=terminal_status, trigger=trigger)
                else:
                    old = replace(old, status=terminal_status)
                terminal_prior.append(old)
                terminal_keys.add(key)
            else:
                prior[key] = old
        orders = tuple(o for o in snapshot.orders if o.status is LimitOrderStatus.OPEN)
        setups = list(terminal_prior)
        for asset in market.assets:
            if asset.symbol in {"USDT", "USDC"}:
                continue
            f = asset.technical_features
            allocation = values[asset.symbol].allocation_pct if asset.symbol in values else ZERO
            cutoff = asset.latest_closed_candle_at
            if cutoff is None or f.atr_4h is None or f.atr_4h <= 0:
                continue
            buy = self.legacy_terms.buy_target(asset, regime)
            if (
                (asset.symbol, SetupType.SUPPORT_HOLD) not in terminal_keys
                and buy is not None
                and f.support is not None
                and f.ema50_4h is not None
                and f.resistance is not None
            ):
                stop = min(f.support, f.ema50_4h) - 2 * f.atr_4h
                target = f.resistance
                if 0 < stop < buy < target:
                    rr = (target - buy) / (buy - stop)
                    score = _score(asset, regime, allocation, rr, p)
                    levels = (SetupLevel(buy, "4h_support_ema_minus_atr", 1),)
                    if (
                        score.total + dict(p.score_weights)["trigger_confirmation"]
                        >= p.strong_score
                        and buy == f.support - f.atr_4h
                        and 0 <= asset.price_usd - f.support <= 4 * f.atr_4h
                        and f.support - 4 * f.atr_4h > 0
                    ):
                        wider_stop = f.support - 4 * f.atr_4h
                        wider_rr = (target - buy) / (buy - wider_stop)
                        wider_score = _score(asset, regime, allocation, wider_rr, p)
                        if (
                            wider_rr >= p.minimum_risk_reward
                            and wider_score.total + dict(p.score_weights)["trigger_confirmation"]
                            >= p.strong_score
                        ):
                            stop, rr, score = wider_stop, wider_rr, wider_score
                            levels += (SetupLevel(buy - f.atr_4h, "4h_support_minus_2atr", 2),)
                            if (
                                score.total + dict(p.score_weights)["trigger_confirmation"] >= 90
                                and buy - 2 * f.atr_4h > stop
                            ):
                                levels += (
                                    SetupLevel(buy - 2 * f.atr_4h, "4h_support_minus_3atr", 3),
                                )
                    if (
                        rr >= p.minimum_risk_reward
                        and score.total + dict(p.score_weights)["trigger_confirmation"]
                        >= p.monitor_score
                        and (target - buy) / buy
                        > 2 * p.estimated_fee_fraction + p.estimated_slippage_fraction
                    ):
                        setups.append(
                            self._setup(
                                snapshot,
                                market.as_of,
                                asset,
                                SetupType.SUPPORT_HOLD,
                                TradingSide.BUY,
                                buy,
                                "4h_support_ema_minus_atr",
                                stop,
                                "4h_support_ema_minus_2atr",
                                target,
                                "4h_resistance",
                                rr,
                                score,
                                prior,
                                orders,
                                risk.reserve.potential_trading_liquidity_usd,
                                levels,
                                f.support,
                            )
                        )
            quantity = holdings.get(asset.symbol, ZERO)
            floor = cores[asset.symbol].preferred_quantity if asset.symbol in cores else ZERO
            reserved = sum(
                (
                    o.quantity_asset
                    for o in orders
                    if o.asset == asset.symbol and o.side is TradingSide.SELL
                ),
                ZERO,
            )
            tradable = max(quantity - floor - reserved, ZERO)
            if (
                (asset.symbol, SetupType.RESISTANCE_REJECTION) not in terminal_keys
                and tradable > 0
                and f.support is not None
                and f.resistance is not None
                and f.rsi_4h is not None
                and f.resistance > asset.price_usd > f.support
                and f.resistance - asset.price_usd <= f.atr_4h
                and (f.rsi_4h >= 65 or (f.change_24h_pct or ZERO) >= 5)
            ):
                invalidation = f.resistance + p.trigger_policy.breakout_invalidation_atr * f.atr_4h
                rr = (f.resistance - f.support) / (invalidation - f.resistance)
                score = _score(asset, regime, allocation, rr, p, selling=True)
                if (
                    rr >= p.minimum_risk_reward
                    and score.total + dict(p.score_weights)["trigger_confirmation"]
                    >= p.monitor_score
                ):
                    levels = (SetupLevel(f.resistance, "4h_resistance", 1),)
                    if (
                        score.total + dict(p.score_weights)["trigger_confirmation"]
                        >= p.strong_score
                        and tradable * f.resistance >= 500
                    ):
                        levels += (
                            SetupLevel(f.resistance + f.atr_4h, "4h_resistance_plus_atr", 2),
                        )
                        if (
                            score.total + dict(p.score_weights)["trigger_confirmation"] >= 90
                            and tradable * f.resistance >= 1000
                        ):
                            levels += (
                                SetupLevel(
                                    f.resistance + 2 * f.atr_4h, "4h_resistance_plus_2atr", 3
                                ),
                            )
                    setups.append(
                        self._setup(
                            snapshot,
                            market.as_of,
                            asset,
                            SetupType.RESISTANCE_REJECTION,
                            TradingSide.SELL,
                            f.resistance,
                            "4h_resistance",
                            invalidation,
                            "4h_resistance_plus_trigger_atr",
                            f.support,
                            "4h_support_target",
                            rr,
                            score,
                            prior,
                            orders,
                            tradable * levels[-1].price,
                            levels,
                            f.resistance,
                        )
                    )
        for key, old in prior.items():
            successor = next((s for s in setups if (s.asset, s.setup_type) == key), None)
            if successor is None or successor.id != old.id:
                status = (
                    SetupStatus.FILLED_OR_CONFIRMED_MANUALLY
                    if any(
                        o.id in old.related_order_ids and o.status is LimitOrderStatus.FILLED
                        for o in snapshot.orders
                    )
                    else SetupStatus.CANCELLED
                    if any(
                        o.id in old.related_order_ids and o.status is LimitOrderStatus.CANCELLED
                        for o in snapshot.orders
                    )
                    else SetupStatus.INVALIDATED
                )
                if status is SetupStatus.INVALIDATED and old.trigger is not None:
                    evidence = next(a for a in market.assets if a.symbol == old.asset)
                    _, trigger = self.trigger_engine.invalidate(
                        status=old.status,
                        context=old.trigger,
                        direction=old.direction,
                        cutoff=evidence.latest_closed_candle_at or market.as_of,
                        reference_price=evidence.latest_closed_price_usd or evidence.price_usd,
                    )
                    setups.append(replace(old, status=status, trigger=trigger))
                else:
                    setups.append(replace(old, status=status))
        ranked = sorted(
            (
                s
                for s in setups
                if s.status in {SetupStatus.ACTIVE, SetupStatus.CONFIRMED}
                and s.score.total >= p.actionable_score
            ),
            key=lambda s: (-s.score.total, s.asset, s.setup_type.value),
        )
        setup_by_asset = {s.asset: s for s in ranked if s.direction is TradingSide.BUY}
        budget = (
            risk.reserve.potential_trading_liquidity_usd * p.fraction(regime)
            if risk.new_buy_capacity_available
            else ZERO
        )
        actions = []
        open_assets = {o.asset for o in orders}
        by_symbol = {a.symbol: a for a in market.assets}
        for o in sorted(orders, key=lambda item: str(item.id)):
            asset = by_symbol[o.asset]
            f = asset.technical_features
            core = cores.get(o.asset)
            over_core = (
                o.side is TradingSide.SELL
                and core is not None
                and holdings.get(o.asset, ZERO)
                - sum(
                    (
                        x.quantity_asset
                        for x in orders
                        if x.asset == o.asset and x.side is TradingSide.SELL
                    ),
                    ZERO,
                )
                < core.hard_floor
            )
            terminal_setup = next(
                (
                    s
                    for s in setups
                    if s.status in {SetupStatus.INVALIDATED, SetupStatus.EXPIRED}
                    and o.id in s.related_order_ids
                ),
                None,
            )
            invalid = (
                over_core
                or terminal_setup is not None
                or risk.reserve.stable_reserve_deficit_usd > 0
                and o.side is TradingSide.BUY
                or o.side is TradingSide.BUY
                and f.support is not None
                and f.atr_4h is not None
                and asset.price_usd < f.support - f.atr_4h
                or o.side is TradingSide.SELL
                and f.support is not None
                and asset.price_usd < f.support
            )
            if invalid:
                actions.append(
                    advice(
                        snapshot.id,
                        o.asset,
                        ActionType.CANCEL_LIMIT_ORDER,
                        "Material setup, reserve or core-floor invalidation; "
                        "confirm cancellation manually.",
                        order_id=o.id,
                        side=o.side,
                        setup_id=terminal_setup.id if terminal_setup else None,
                    )
                )
                continue
            setup = (
                setup_by_asset.get(o.asset)
                if o.side is TradingSide.BUY
                else next(
                    (s for s in ranked if s.asset == o.asset and s.direction is TradingSide.SELL),
                    None,
                )
            )
            material = (
                setup is not None
                and abs(setup.limit_price - o.target_price) / o.target_price
                >= p.material_price_change_fraction
                and f.atr_4h is not None
                and abs(setup.limit_price - o.target_price) >= f.atr_4h
            )
            if (
                material
                and o.side is TradingSide.BUY
                and budget >= o.quantity_asset * setup.limit_price
            ):
                replacement = advice(
                    snapshot.id,
                    o.asset,
                    ActionType.REPLACE_LIMIT_ORDER,
                    "Structural level changed materially; old commitment remains "
                    "reserved until manual cancellation.",
                    order_id=o.id,
                    side=o.side,
                    price=setup.limit_price,
                    quantity=o.quantity_asset,
                    setup_id=setup.id,
                )
                actions.append(replacement)
                budget -= o.quantity_asset * setup.limit_price
            else:
                actions.append(
                    advice(
                        snapshot.id,
                        o.asset,
                        ActionType.KEEP_LIMIT_ORDER,
                        "Original setup remains valid; avoid chasing a small level change.",
                        order_id=o.id,
                        side=o.side,
                    )
                )
        invested = sum(
            (
                a.target_price * a.quantity_asset
                for a in actions
                if a.action_type is ActionType.REPLACE_LIMIT_ORDER and a.side is TradingSide.BUY
            ),
            ZERO,
        )
        sell_assets = {s.asset for s in ranked if s.direction is TradingSide.SELL}
        fundable = [
            s
            for s in ranked
            if s.direction is TradingSide.BUY
            and s.asset not in open_assets
            and s.asset not in sell_assets
        ]
        slot_cap = budget / Decimal(len(fundable)) if fundable else ZERO
        allocations = {}
        for setup in fundable:
            valuation = values.get(setup.asset)
            room = max(
                risk.valuation.total_portfolio_usd * p.max_asset_allocation
                - (valuation.market_value_usd if valuation else ZERO),
                ZERO,
            )
            capital = min(budget, slot_cap, room, setup.maximum_capital_usd)
            levels = list(setup.levels)
            while len(levels) > 1 and capital / Decimal(len(levels)) < p.minimum_order_usd:
                levels.pop()
            per_level = capital / Decimal(len(levels))
            selected = []
            for level in levels:
                qty = self.legacy_terms.quantity(per_level, level.price)
                if qty * level.price >= p.minimum_order_usd:
                    selected.append((level, qty))
            if selected:
                allocations[setup.asset] = tuple(selected)
                budget -= sum((level.price * qty for level, qty in selected), ZERO)
        decisions = []
        for asset in market.assets:
            if asset.symbol in {"USDT", "USDC"}:
                continue
            valuation = values.get(asset.symbol)
            quantity = holdings.get(asset.symbol, ZERO)
            core = cores.get(asset.symbol)
            reserved = sum(
                (
                    o.quantity_asset
                    for o in orders
                    if o.asset == asset.symbol and o.side is TradingSide.SELL
                ),
                ZERO,
            )
            tradable = max(quantity - (core.preferred_quantity if core else ZERO) - reserved, ZERO)
            buy = setup_by_asset.get(asset.symbol)
            sell = next(
                (s for s in ranked if s.asset == asset.symbol and s.direction is TradingSide.SELL),
                None,
            )
            decision = AssetDecisionType.HOLD if quantity else AssetDecisionType.WAIT
            chosen = None
            if sell is not None and tradable > 0 and asset.symbol not in open_assets:
                sell_quantity = min(tradable, quantity * Decimal("0.20"))
                levels = list(sell.levels)
                while len(levels) > 1 and (
                    sell_quantity * sell.limit_price / Decimal(len(levels)) < p.minimum_order_usd
                ):
                    levels.pop()
                per_level_quantity = sell_quantity / Decimal(len(levels))
                for level in levels:
                    level_quantity = self.legacy_terms.quantity(
                        per_level_quantity * level.price, level.price
                    )
                    if level_quantity * level.price < p.minimum_order_usd:
                        continue
                    actions.append(
                        advice(
                            snapshot.id,
                            asset.symbol,
                            ActionType.PLACE_LIMIT_ORDER,
                            "Partial profit-taking at deterministic resistance; "
                            "protected core remains untouched.",
                            side=TradingSide.SELL,
                            price=level.price,
                            quantity=level_quantity,
                            priority=level.priority,
                            setup_id=sell.id,
                        )
                    )
                if any(
                    a.asset == asset.symbol
                    and a.side is TradingSide.SELL
                    and a.action_type is ActionType.PLACE_LIMIT_ORDER
                    for a in actions
                ):
                    decision, chosen = AssetDecisionType.PARTIAL_SELL, sell.id
            if decision is not AssetDecisionType.PARTIAL_SELL and buy is not None:
                selected = allocations.get(asset.symbol, ())
                for level, qty in selected:
                    actions.append(
                        advice(
                            snapshot.id,
                            asset.symbol,
                            ActionType.PLACE_LIMIT_ORDER,
                            "Ranked pullback setup; a bounded amount of liquidity is deployed.",
                            side=TradingSide.BUY,
                            price=level.price,
                            quantity=qty,
                            priority=level.priority,
                            setup_id=buy.id,
                        )
                    )
                    spent = qty * level.price
                    invested += spent
                if selected:
                    decision, chosen = AssetDecisionType.ADD, buy.id
            if (
                decision in {AssetDecisionType.HOLD, AssetDecisionType.WAIT}
                and valuation is not None
                and valuation.allocation_pct > 50
            ):
                decision = AssetDecisionType.REBALANCE_INFORMATIONAL
                actions.append(
                    advice(
                        snapshot.id,
                        asset.symbol,
                        ActionType.REBALANCE_INFORMATIONAL,
                        "Concentration warrants review, but no favorable exit is confirmed.",
                    )
                )
            elif decision in {AssetDecisionType.HOLD, AssetDecisionType.WAIT}:
                actions.append(
                    advice(
                        snapshot.id,
                        asset.symbol,
                        ActionType.HOLD if quantity else ActionType.WAIT,
                        "No validated opportunity justifies fees, risk and liquidity use.",
                    )
                )
            decisions.append(
                AssetDecision(
                    asset.symbol,
                    decision,
                    quantity,
                    valuation.market_value_usd if valuation else ZERO,
                    valuation.allocation_pct if valuation else ZERO,
                    core.hard_floor if core else None,
                    core.preferred_quantity if core else None,
                    tradable,
                    asset.daily_context.trend,
                    chosen,
                )
            )
        if not actions:
            actions.append(advice(snapshot.id, None, ActionType.WAIT, "No trade is justified."))
        actions = tuple(actions)
        return ActiveAnalysis(
            p.version,
            tuple(setups),
            tuple(decisions),
            tuple(s.id for s in ranked),
            invested,
            max(risk.reserve.potential_trading_liquidity_usd - invested, ZERO),
            _severity(actions, risk, tuple(setups), previous_regime, previous_warnings),
        ), actions

    def _setup(
        self,
        snapshot,
        analyzed_at,
        asset,
        kind,
        side,
        limit,
        limit_source,
        invalidation,
        invalidation_source,
        target,
        target_source,
        rr,
        score,
        prior,
        orders,
        max_capital,
        levels,
        trigger_level,
    ):
        old = prior.get((asset.symbol, kind))
        if old is not None:
            movement = abs(limit - old.limit_price)
            atr = asset.technical_features.atr_4h
            if (
                movement / old.limit_price >= self.policy.material_price_change_fraction
                and atr is not None
                and movement >= atr
            ):
                old = None
        created = old.created_at if old else analyzed_at
        cutoff = old.originating_cutoff if old else asset.latest_closed_candle_at
        setup_id = (
            old.id
            if old
            else uuid5(
                NAMESPACE_URL,
                f"{snapshot.id}:{asset.symbol}:{kind}:{cutoff}:{format(limit, 'f')}",
            )
        )
        related = tuple(o.id for o in orders if o.asset == asset.symbol and o.side is side)
        trigger = (
            old.trigger
            if old is not None and old.trigger is not None
            else self.trigger_engine.initial_context(
                level=trigger_level,
                atr=asset.technical_features.atr_4h,
            )
        )
        status, trigger = self.trigger_engine.evaluate(
            status=old.status
            if old is not None and old.trigger is not None
            else SetupStatus.CANDIDATE,
            context=trigger,
            direction=side,
            candles=tuple(
                c for c in asset.closed_1h_candles if c.closed_at >= cutoff - timedelta(hours=3)
            ),
            latest_4h_close=asset.latest_closed_price_usd,
            latest_4h_cutoff=asset.latest_closed_candle_at,
            invalidation_price=invalidation,
        )
        score = _with_trigger_score(score, status, self.policy)
        return TradeSetup(
            setup_id,
            asset.symbol,
            side,
            kind,
            created,
            cutoff,
            status,
            limit,
            limit_source,
            invalidation,
            invalidation_source,
            target,
            target_source,
            rr,
            max_capital,
            score,
            related,
            levels,
            trigger,
        )
