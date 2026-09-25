"""Explicit HTTP projection of typed analysis history; no persistence payloads."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from advanced_trading_advisor.analysis.active import (
    ActiveAnalysis,
    AssetDecision,
    SetupLevel,
    TradeSetup,
)
from advanced_trading_advisor.analysis.history_query import AnalysisRunDetail
from advanced_trading_advisor.analysis.models import AnalysisRun
from advanced_trading_advisor.analysis.recommendations import Recommendation
from advanced_trading_advisor.analysis.result import AnalysisResult, FinancialSummary
from advanced_trading_advisor.analysis.traceability import DecisionConfiguration
from advanced_trading_advisor.analysis.triggers import TriggerContext, TriggerTransition
from advanced_trading_advisor.market.models import (
    Candle,
    DailyContext,
    MarketAssetData,
    MarketSnapshot,
    TechnicalFeatures,
)

from .dtos import decimal_text, utc_timestamp


def optional_decimal(value: Decimal | None) -> str | None:
    return decimal_text(value) if value is not None else None


def optional_timestamp(value: datetime | None) -> str | None:
    return utc_timestamp(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class AnalysisRunDTO:
    run_id: str
    snapshot_id: str
    started_at: str
    status: str
    completed_at: str | None
    market_cutoff: str | None

    @classmethod
    def from_domain(cls, run: AnalysisRun) -> "AnalysisRunDTO":
        return cls(
            str(run.run_id),
            str(run.snapshot_id),
            utc_timestamp(run.started_at),
            run.status.value,
            optional_timestamp(run.completed_at),
            optional_timestamp(run.market_cutoff),
        )


@dataclass(frozen=True, slots=True)
class RecentAnalysesDTO:
    analyses: tuple[AnalysisRunDTO, ...]

    @classmethod
    def from_domain(cls, runs: tuple[AnalysisRun, ...]) -> "RecentAnalysesDTO":
        return cls(tuple(AnalysisRunDTO.from_domain(run) for run in runs))


@dataclass(frozen=True, slots=True)
class CandleDTO:
    closed_at: str
    open: str
    high: str
    low: str
    close: str

    @classmethod
    def from_domain(cls, candle: Candle) -> "CandleDTO":
        return cls(
            utc_timestamp(candle.closed_at),
            decimal_text(candle.open),
            decimal_text(candle.high),
            decimal_text(candle.low),
            decimal_text(candle.close),
        )


@dataclass(frozen=True, slots=True)
class TechnicalFeaturesDTO:
    change_24h_pct: str | None
    change_7d_pct: str | None
    rsi_4h: str | None
    ema20_4h: str | None
    ema50_4h: str | None
    atr_4h: str | None
    support: str | None
    resistance: str | None

    @classmethod
    def from_domain(cls, features: TechnicalFeatures) -> "TechnicalFeaturesDTO":
        return cls(
            optional_decimal(features.change_24h_pct),
            optional_decimal(features.change_7d_pct),
            optional_decimal(features.rsi_4h),
            optional_decimal(features.ema20_4h),
            optional_decimal(features.ema50_4h),
            optional_decimal(features.atr_4h),
            optional_decimal(features.support),
            optional_decimal(features.resistance),
        )


@dataclass(frozen=True, slots=True)
class DailyContextDTO:
    latest_closed_at: str | None
    closed_price_usd: str | None
    ema20: str | None
    ema50: str | None
    support: str | None
    resistance: str | None
    trend: str

    @classmethod
    def from_domain(cls, daily: DailyContext) -> "DailyContextDTO":
        return cls(
            optional_timestamp(daily.latest_closed_at),
            optional_decimal(daily.closed_price_usd),
            optional_decimal(daily.ema20),
            optional_decimal(daily.ema50),
            optional_decimal(daily.support),
            optional_decimal(daily.resistance),
            daily.trend.value,
        )


@dataclass(frozen=True, slots=True)
class MarketAssetDTO:
    symbol: str
    price_usd: str
    quoted_at: str
    latest_closed_candle_at: str | None
    technical_features: TechnicalFeaturesDTO
    completeness: str
    latest_closed_price_usd: str | None
    daily_context: DailyContextDTO
    closed_1h_candles: tuple[CandleDTO, ...]

    @classmethod
    def from_domain(cls, asset: MarketAssetData) -> "MarketAssetDTO":
        return cls(
            asset.symbol,
            decimal_text(asset.price_usd),
            utc_timestamp(asset.quoted_at),
            optional_timestamp(asset.latest_closed_candle_at),
            TechnicalFeaturesDTO.from_domain(asset.technical_features),
            asset.completeness.value,
            optional_decimal(asset.latest_closed_price_usd),
            DailyContextDTO.from_domain(asset.daily_context),
            tuple(CandleDTO.from_domain(candle) for candle in asset.closed_1h_candles),
        )


@dataclass(frozen=True, slots=True)
class MarketSnapshotDTO:
    as_of: str
    assets: tuple[MarketAssetDTO, ...]
    is_live: bool

    @classmethod
    def from_domain(cls, market: MarketSnapshot) -> "MarketSnapshotDTO":
        return cls(
            utc_timestamp(market.as_of),
            tuple(MarketAssetDTO.from_domain(asset) for asset in market.assets),
            market.is_live,
        )


@dataclass(frozen=True, slots=True)
class FinancialSummaryDTO:
    total_portfolio_usd: str
    current_stables_usd: str
    stable_allocation_percent: str
    monthly_expenses_usd: str
    target_expense_runway_months: str
    recommended_minimum_stables_usd: str
    deployable_stables_usd: str
    stable_reserve_deficit_usd: str
    actual_expense_runway_months: str
    open_buy_commitments_usd: str
    hard_expense_reserve_usd: str
    market_buffer_usd: str
    potential_trading_liquidity_usd: str
    strategy_dry_powder_usd: str
    recommended_deployment_usd: str

    @classmethod
    def from_domain(cls, financial: FinancialSummary) -> "FinancialSummaryDTO":
        return cls(
            decimal_text(financial.total_portfolio_usd),
            decimal_text(financial.current_stables_usd),
            decimal_text(financial.stable_allocation_percent),
            decimal_text(financial.monthly_expenses_usd),
            decimal_text(financial.target_expense_runway_months),
            decimal_text(financial.recommended_minimum_stables_usd),
            decimal_text(financial.deployable_stables_usd),
            decimal_text(financial.stable_reserve_deficit_usd),
            decimal_text(financial.actual_expense_runway_months),
            decimal_text(financial.open_buy_commitments_usd),
            decimal_text(financial.hard_expense_reserve_usd),
            decimal_text(financial.market_buffer_usd),
            decimal_text(financial.potential_trading_liquidity_usd),
            decimal_text(financial.strategy_dry_powder_usd),
            decimal_text(financial.recommended_deployment_usd),
        )


@dataclass(frozen=True, slots=True)
class RecommendationDTO:
    recommendation_id: str
    asset: str | None
    action_type: str
    priority: int
    reason: str
    existing_order_id: str | None
    side: str | None
    target_price: str | None
    quantity_asset: str | None
    invalidation_condition: str | None
    review_at: str | None
    expires_at: str | None
    setup_id: str | None

    @classmethod
    def from_domain(cls, recommendation: Recommendation) -> "RecommendationDTO":
        return cls(
            str(recommendation.recommendation_id),
            recommendation.asset,
            recommendation.action_type.value,
            recommendation.priority,
            recommendation.reason,
            str(recommendation.existing_order_id) if recommendation.existing_order_id else None,
            recommendation.side.value if recommendation.side else None,
            optional_decimal(recommendation.target_price),
            optional_decimal(recommendation.quantity_asset),
            recommendation.invalidation_condition,
            optional_timestamp(recommendation.review_at),
            optional_timestamp(recommendation.expires_at),
            str(recommendation.setup_id) if recommendation.setup_id else None,
        )


@dataclass(frozen=True, slots=True)
class DecisionConfigurationDTO:
    market_regime_version: int
    stable_reserve_version: int
    whole_plan_version: int
    target_runway_months: str
    market_source: str
    ai_enabled: bool
    model_name: str | None
    reasoning_effort: str | None
    ai_context_version: int | None
    ai_schema_version: int | None
    ai_prompt_version: int | None
    ai_failure_category: str | None
    recommendation_parameters: str
    active_policy_parameters: str | None
    reserve_policy_parameters: str | None

    @classmethod
    def from_domain(cls, config: DecisionConfiguration) -> "DecisionConfigurationDTO":
        return cls(
            config.market_regime_version,
            config.stable_reserve_version,
            config.whole_plan_version,
            config.target_runway_months,
            config.market_source,
            config.ai_enabled,
            config.model_name,
            config.reasoning_effort,
            config.ai_context_version,
            config.ai_schema_version,
            config.ai_prompt_version,
            config.ai_failure_category,
            config.recommendation_parameters,
            config.active_policy_parameters,
            config.reserve_policy_parameters,
        )


@dataclass(frozen=True, slots=True)
class SetupLevelDTO:
    price: str
    source: str
    priority: int

    @classmethod
    def from_domain(cls, level: SetupLevel) -> "SetupLevelDTO":
        return cls(decimal_text(level.price), level.source, level.priority)


@dataclass(frozen=True, slots=True)
class SetupScoreDTO:
    version: int
    components: tuple[tuple[str, int, int], ...]


@dataclass(frozen=True, slots=True)
class TriggerTransitionDTO:
    previous_state: str
    current_state: str
    transitioned_at: str
    market_cutoff: str
    timeframe: str
    reason: str
    reference_price: str

    @classmethod
    def from_domain(cls, transition: TriggerTransition) -> "TriggerTransitionDTO":
        return cls(
            transition.previous_state.value,
            transition.current_state.value,
            utc_timestamp(transition.transitioned_at),
            utc_timestamp(transition.market_cutoff),
            transition.timeframe.value,
            transition.reason.value,
            decimal_text(transition.reference_price),
        )


@dataclass(frozen=True, slots=True)
class TriggerContextDTO:
    version: int
    zone_low: str
    zone_high: str
    atr_at_creation: str
    transitions: tuple[TriggerTransitionDTO, ...]
    touched_at: str | None
    highest_observed_price: str | None
    lowest_observed_price: str | None
    trigger_reference_price: str | None
    confirmation_at: str | None
    confirmation_candle: CandleDTO | None
    expires_at: str | None
    last_processed_1h_candle_at: str | None
    execution_valid: bool

    @classmethod
    def from_domain(cls, trigger: TriggerContext) -> "TriggerContextDTO":
        return cls(
            trigger.version,
            decimal_text(trigger.zone_low),
            decimal_text(trigger.zone_high),
            decimal_text(trigger.atr_at_creation),
            tuple(TriggerTransitionDTO.from_domain(t) for t in trigger.transitions),
            optional_timestamp(trigger.touched_at),
            optional_decimal(trigger.highest_observed_price),
            optional_decimal(trigger.lowest_observed_price),
            optional_decimal(trigger.trigger_reference_price),
            optional_timestamp(trigger.confirmation_at),
            CandleDTO.from_domain(trigger.confirmation_candle)
            if trigger.confirmation_candle is not None
            else None,
            optional_timestamp(trigger.expires_at),
            optional_timestamp(trigger.last_processed_1h_candle_at),
            trigger.execution_valid,
        )


@dataclass(frozen=True, slots=True)
class TradeSetupDTO:
    id: str
    asset: str
    direction: str
    setup_type: str
    created_at: str
    originating_cutoff: str
    status: str
    limit_price: str
    price_source: str
    invalidation_price: str
    invalidation_source: str
    target_price: str
    target_source: str
    risk_reward: str
    maximum_capital_usd: str
    score: SetupScoreDTO
    related_order_ids: tuple[str, ...]
    levels: tuple[SetupLevelDTO, ...]
    trigger: TriggerContextDTO | None

    @classmethod
    def from_domain(cls, setup: TradeSetup) -> "TradeSetupDTO":
        return cls(
            str(setup.id),
            setup.asset,
            setup.direction.value,
            setup.setup_type.value,
            utc_timestamp(setup.created_at),
            utc_timestamp(setup.originating_cutoff),
            setup.status.value,
            decimal_text(setup.limit_price),
            setup.price_source,
            decimal_text(setup.invalidation_price),
            setup.invalidation_source,
            decimal_text(setup.target_price),
            setup.target_source,
            decimal_text(setup.risk_reward),
            decimal_text(setup.maximum_capital_usd),
            SetupScoreDTO(setup.score.version, setup.score.components),
            tuple(str(order_id) for order_id in setup.related_order_ids),
            tuple(SetupLevelDTO.from_domain(level) for level in setup.levels),
            TriggerContextDTO.from_domain(setup.trigger) if setup.trigger is not None else None,
        )


@dataclass(frozen=True, slots=True)
class AssetDecisionDTO:
    asset: str
    decision: str
    quantity: str
    market_value_usd: str
    allocation_percent: str
    core_floor: str | None
    preferred_core: str | None
    tradable_quantity: str
    daily_trend: str
    setup_id: str | None

    @classmethod
    def from_domain(cls, decision: AssetDecision) -> "AssetDecisionDTO":
        return cls(
            decision.asset,
            decision.decision.value,
            decimal_text(decision.quantity),
            decimal_text(decision.market_value_usd),
            decimal_text(decision.allocation_percent),
            optional_decimal(decision.core_floor),
            optional_decimal(decision.preferred_core),
            decimal_text(decision.tradable_quantity),
            decision.daily_trend.value,
            str(decision.setup_id) if decision.setup_id else None,
        )


@dataclass(frozen=True, slots=True)
class ActiveAnalysisDTO:
    policy_version: int
    setups: tuple[TradeSetupDTO, ...]
    asset_decisions: tuple[AssetDecisionDTO, ...]
    ranked_setup_ids: tuple[str, ...]
    recommended_deployment_usd: str
    strategy_dry_powder_usd: str
    severity: str

    @classmethod
    def from_domain(cls, active: ActiveAnalysis) -> "ActiveAnalysisDTO":
        return cls(
            active.policy_version,
            tuple(TradeSetupDTO.from_domain(setup) for setup in active.setups),
            tuple(AssetDecisionDTO.from_domain(decision) for decision in active.asset_decisions),
            tuple(str(setup_id) for setup_id in active.ranked_setup_ids),
            decimal_text(active.recommended_deployment_usd),
            decimal_text(active.strategy_dry_powder_usd),
            active.severity.value,
        )


@dataclass(frozen=True, slots=True)
class AnalysisResultDTO:
    run_id: str
    snapshot_id: str
    generated_at: str
    market_cutoff: str | None
    market: MarketSnapshotDTO
    market_regime: str
    regime_policy_version: int
    recommendation_policy_version: int
    financial: FinancialSummaryDTO
    warnings: tuple[str, ...]
    recommendations: tuple[RecommendationDTO, ...]
    reasoning_mode: str
    reasoning_model: str | None
    ai_warning: str | None
    configuration: DecisionConfigurationDTO | None
    active: ActiveAnalysisDTO | None

    @classmethod
    def from_domain(cls, result: AnalysisResult) -> "AnalysisResultDTO":
        return cls(
            str(result.run_id),
            str(result.snapshot_id),
            utc_timestamp(result.generated_at),
            optional_timestamp(result.market_cutoff),
            MarketSnapshotDTO.from_domain(result.market),
            result.market_regime.value,
            result.regime_policy_version,
            result.recommendation_policy_version,
            FinancialSummaryDTO.from_domain(result.financial),
            result.warnings,
            tuple(RecommendationDTO.from_domain(action) for action in result.recommendations),
            result.reasoning_mode.value,
            result.reasoning_model,
            result.ai_warning,
            DecisionConfigurationDTO.from_domain(result.configuration)
            if result.configuration is not None
            else None,
            ActiveAnalysisDTO.from_domain(result.active) if result.active is not None else None,
        )


@dataclass(frozen=True, slots=True)
class AnalysisDetailDTO:
    run: AnalysisRunDTO
    result: AnalysisResultDTO | None
    failure_category: str | None

    @classmethod
    def from_domain(cls, detail: AnalysisRunDetail) -> "AnalysisDetailDTO":
        return cls(
            AnalysisRunDTO.from_domain(detail.run),
            AnalysisResultDTO.from_domain(detail.result) if detail.result is not None else None,
            detail.failure_category.value if detail.failure_category is not None else None,
        )
