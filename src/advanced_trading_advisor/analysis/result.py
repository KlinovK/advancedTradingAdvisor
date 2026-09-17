import json
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from advanced_trading_advisor.domain.numeric import finite_decimal, parse_decimal
from advanced_trading_advisor.domain.validation import identity, items, require, timestamp
from advanced_trading_advisor.market.models import (
    Candle,
    DailyContext,
    DailyTrend,
    DataCompleteness,
    MarketAssetData,
    MarketSnapshot,
    TechnicalFeatures,
)
from advanced_trading_advisor.market.regime import MarketRegime
from advanced_trading_advisor.portfolio.models import TradingSide

from .active import (
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
from .reasoning import AI_WARNING, ReasoningMode
from .recommendations import ActionType, Recommendation
from .traceability import DecisionConfiguration
from .triggers import (
    TriggerContext,
    TriggerReason,
    TriggerTimeframe,
    TriggerTransition,
)


@dataclass(frozen=True, slots=True)
class FinancialSummary:
    total_portfolio_usd: Decimal
    current_stables_usd: Decimal
    stable_allocation_percent: Decimal
    monthly_expenses_usd: Decimal
    target_expense_runway_months: Decimal
    recommended_minimum_stables_usd: Decimal
    deployable_stables_usd: Decimal
    stable_reserve_deficit_usd: Decimal
    actual_expense_runway_months: Decimal
    open_buy_commitments_usd: Decimal
    hard_expense_reserve_usd: Decimal = Decimal("0")
    market_buffer_usd: Decimal = Decimal("0")
    potential_trading_liquidity_usd: Decimal = Decimal("0")
    strategy_dry_powder_usd: Decimal = Decimal("0")
    recommended_deployment_usd: Decimal = Decimal("0")

    def __post_init__(self):
        for f in fields(self):
            value = getattr(self, f.name)
            finite_decimal(value)
            require(value >= 0, "Financial summary must be nonnegative.")


@dataclass(frozen=True, slots=True)
class AnalysisResult:
    run_id: UUID
    snapshot_id: UUID
    generated_at: datetime
    market: MarketSnapshot
    market_regime: MarketRegime
    regime_policy_version: int
    recommendation_policy_version: int
    financial: FinancialSummary
    warnings: tuple[str, ...]
    recommendations: tuple[Recommendation, ...]
    reasoning_mode: ReasoningMode = ReasoningMode.DETERMINISTIC
    reasoning_model: str | None = None
    ai_warning: str | None = None
    configuration: DecisionConfiguration | None = None
    active: ActiveAnalysis | None = None

    def __post_init__(self):
        identity(self.run_id)
        if self.configuration is not None:
            require(
                self.configuration.model_name == self.reasoning_model
                and self.configuration.market_regime_version == self.regime_policy_version,
                "Decision provenance mismatch.",
            )
            require(
                isinstance(self.configuration, DecisionConfiguration),
                "Invalid configuration snapshot.",
            )
            require(
                self.configuration.ai_enabled
                == (self.reasoning_mode is not ReasoningMode.DETERMINISTIC),
                "Reasoning provenance mismatch.",
            )
            require(
                parse_decimal(self.configuration.target_runway_months)
                == self.financial.target_expense_runway_months,
                "Runway provenance mismatch.",
            )
            require(
                self.configuration.market_source == ("live" if self.market.is_live else "static"),
                "Market provenance mismatch.",
            )
        identity(self.snapshot_id)
        timestamp(self.generated_at)
        require(isinstance(self.market, MarketSnapshot), "Invalid market evidence.")
        require(self.market.as_of <= self.generated_at, "Result precedes market evidence.")
        require(isinstance(self.market_regime, MarketRegime), "Invalid market regime.")
        require(
            type(self.regime_policy_version) is int
            and self.regime_policy_version in {1, 2}
            and type(self.recommendation_policy_version) is int
            and self.recommendation_policy_version == 1,
            "Unsupported policy version.",
        )
        require(isinstance(self.financial, FinancialSummary), "Invalid financial summary.")
        if self.active is not None:
            require(isinstance(self.active, ActiveAnalysis), "Invalid active analysis.")
            require(
                self.financial.recommended_deployment_usd == self.active.recommended_deployment_usd
                and self.financial.strategy_dry_powder_usd == self.active.strategy_dry_powder_usd,
                "Active capital summary disagrees with financial summary.",
            )
        items(self.warnings, str)
        items(self.recommendations, Recommendation)
        require(bool(self.recommendations), "Validated recommendations required.")
        require(isinstance(self.reasoning_mode, ReasoningMode), "Invalid reasoning mode.")
        if self.reasoning_mode is ReasoningMode.DETERMINISTIC:
            require(
                self.reasoning_model is None and self.ai_warning is None,
                "Deterministic result cannot claim an AI attempt.",
            )
        else:
            require(
                isinstance(self.reasoning_model, str)
                and 0 < len(self.reasoning_model) <= 100
                and all(c.isascii() and (c.isalnum() or c in "._:-") for c in self.reasoning_model),
                "Invalid reasoning model identifier.",
            )
            require(
                self.ai_warning
                == (AI_WARNING if self.reasoning_mode is ReasoningMode.AI_FALLBACK else None),
                "Invalid AI warning.",
            )

    @property
    def market_cutoff(self):
        return self.market.effective_market_cutoff


def _time(value):
    timestamp(value)
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _date(value):
    require(isinstance(value, str), "Timestamp must be text.")
    result = datetime.fromisoformat(value)
    timestamp(result)
    return result.astimezone(UTC)


def _uuid(value):
    require(isinstance(value, str), "UUID must be text.")
    result = UUID(value)
    require(str(result) == value, "Noncanonical UUID.")
    return result


def _object(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), "Unexpected result shape.")
    return value


def _array(value):
    require(isinstance(value, list), "Expected array.")
    return value


def _encode_value(value):
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return _time(value)
    if isinstance(value, UUID):
        return str(value)
    return value


def _decode_active(raw, version: int) -> ActiveAnalysis:
    d = _object(raw, {f.name for f in fields(ActiveAnalysis)})
    setups = []
    for item in _array(d["setups"]):
        s = _object(
            item,
            {f.name for f in fields(TradeSetup)} - ({"trigger"} if version == 4 else set()),
        )
        score = _object(s["score"], {f.name for f in fields(SetupScore)})
        trigger = None
        if version >= 5 and s["trigger"] is not None:
            raw_trigger = _object(s["trigger"], {f.name for f in fields(TriggerContext)})
            raw_confirmation = None
            if raw_trigger["confirmation_candle"] is not None:
                confirmation = _object(
                    raw_trigger["confirmation_candle"],
                    {f.name for f in fields(Candle)},
                )
                raw_confirmation = Candle(
                    _date(confirmation["closed_at"]),
                    parse_decimal(confirmation["open"], positive=True),
                    parse_decimal(confirmation["high"], positive=True),
                    parse_decimal(confirmation["low"], positive=True),
                    parse_decimal(confirmation["close"], positive=True),
                )
            transitions = []
            for transition in _array(raw_trigger["transitions"]):
                t = _object(transition, {f.name for f in fields(TriggerTransition)})
                transitions.append(
                    TriggerTransition(
                        SetupStatus(t["previous_state"]),
                        SetupStatus(t["current_state"]),
                        _date(t["transitioned_at"]),
                        _date(t["market_cutoff"]),
                        TriggerTimeframe(t["timeframe"]),
                        TriggerReason(t["reason"]),
                        parse_decimal(t["reference_price"], positive=True),
                    )
                )
            trigger = TriggerContext(
                raw_trigger["version"],
                parse_decimal(raw_trigger["zone_low"], positive=True),
                parse_decimal(raw_trigger["zone_high"], positive=True),
                parse_decimal(raw_trigger["atr_at_creation"], positive=True),
                tuple(transitions),
                _date(raw_trigger["touched_at"]) if raw_trigger["touched_at"] else None,
                parse_decimal(raw_trigger["highest_observed_price"], positive=True)
                if raw_trigger["highest_observed_price"] is not None
                else None,
                parse_decimal(raw_trigger["lowest_observed_price"], positive=True)
                if raw_trigger["lowest_observed_price"] is not None
                else None,
                parse_decimal(raw_trigger["trigger_reference_price"], positive=True)
                if raw_trigger["trigger_reference_price"] is not None
                else None,
                _date(raw_trigger["confirmation_at"]) if raw_trigger["confirmation_at"] else None,
                raw_confirmation,
                _date(raw_trigger["expires_at"]) if raw_trigger["expires_at"] else None,
                _date(raw_trigger["last_processed_1h_candle_at"])
                if raw_trigger["last_processed_1h_candle_at"]
                else None,
                raw_trigger["execution_valid"],
            )
        setups.append(
            TradeSetup(
                _uuid(s["id"]),
                s["asset"],
                TradingSide(s["direction"]),
                SetupType(s["setup_type"]),
                _date(s["created_at"]),
                _date(s["originating_cutoff"]),
                SetupStatus(s["status"]),
                parse_decimal(s["limit_price"], positive=True),
                s["price_source"],
                parse_decimal(s["invalidation_price"], positive=True),
                s["invalidation_source"],
                parse_decimal(s["target_price"], positive=True),
                s["target_source"],
                parse_decimal(s["risk_reward"], positive=True),
                parse_decimal(s["maximum_capital_usd"]),
                SetupScore(
                    score["version"],
                    tuple(tuple(component) for component in _array(score["components"])),
                ),
                tuple(_uuid(value) for value in _array(s["related_order_ids"])),
                tuple(
                    SetupLevel(
                        parse_decimal(level["price"], positive=True),
                        level["source"],
                        level["priority"],
                    )
                    for item in _array(s["levels"])
                    for level in (_object(item, {f.name for f in fields(SetupLevel)}),)
                ),
                trigger,
            )
        )
    decisions = []
    for item in _array(d["asset_decisions"]):
        a = _object(item, {f.name for f in fields(AssetDecision)})
        decisions.append(
            AssetDecision(
                a["asset"],
                AssetDecisionType(a["decision"]),
                parse_decimal(a["quantity"]),
                parse_decimal(a["market_value_usd"]),
                parse_decimal(a["allocation_percent"]),
                parse_decimal(a["core_floor"], positive=True)
                if a["core_floor"] is not None
                else None,
                parse_decimal(a["preferred_core"], positive=True)
                if a["preferred_core"] is not None
                else None,
                parse_decimal(a["tradable_quantity"]),
                DailyTrend(a["daily_trend"]),
                _uuid(a["setup_id"]) if a["setup_id"] is not None else None,
            )
        )
    return ActiveAnalysis(
        d["policy_version"],
        tuple(setups),
        tuple(decisions),
        tuple(_uuid(value) for value in _array(d["ranked_setup_ids"])),
        parse_decimal(d["recommended_deployment_usd"]),
        parse_decimal(d["strategy_dry_powder_usd"]),
        TelegramSeverity(d["severity"]),
    )


class AnalysisResultCodec:
    """Application-owned versioned result envelope; no SQLite dependency or raw provider data."""

    @staticmethod
    def encode(result: AnalysisResult) -> str:
        assets = []
        for a in result.market.assets:
            asset_data = {
                **{
                    f.name: _encode_value(getattr(a, f.name))
                    for f in fields(a)
                    if f.name not in {"technical_features", "daily_context", "closed_1h_candles"}
                },
                "technical_features": {
                    f.name: _encode_value(getattr(a.technical_features, f.name))
                    for f in fields(TechnicalFeatures)
                },
            }
            if result.active is not None:
                asset_data["daily_context"] = {
                    f.name: _encode_value(getattr(a.daily_context, f.name))
                    for f in fields(DailyContext)
                }
                if result.active.policy_version >= 2:
                    asset_data["closed_1h_candles"] = [
                        {f.name: _encode_value(getattr(c, f.name)) for f in fields(c)}
                        for c in a.closed_1h_candles
                    ]
            assets.append(asset_data)
        data = {
            "run_id": str(result.run_id),
            "reasoning_mode": result.reasoning_mode,
            "reasoning_model": result.reasoning_model,
            "ai_warning": result.ai_warning,
            "snapshot_id": str(result.snapshot_id),
            "generated_at": _time(result.generated_at),
            "market_cutoff": _time(result.market_cutoff) if result.market_cutoff else None,
            "market_as_of": _time(result.market.as_of),
            "is_live": result.market.is_live,
            "market_regime": result.market_regime,
            "regime_policy_version": result.regime_policy_version,
            "recommendation_policy_version": result.recommendation_policy_version,
            "financial": {
                f.name: _encode_value(getattr(result.financial, f.name))
                for f in fields(FinancialSummary)
                if result.active is not None
                or f.default is not None
                and f.name
                not in {
                    "hard_expense_reserve_usd",
                    "market_buffer_usd",
                    "potential_trading_liquidity_usd",
                    "strategy_dry_powder_usd",
                    "recommended_deployment_usd",
                }
            },
            "warnings": list(result.warnings),
            "analyzed_assets": assets,
            "recommendations": [
                {f.name: _encode_value(getattr(r, f.name)) for f in fields(Recommendation)}
                for r in result.recommendations
            ],
        }
        if result.configuration is not None:
            config = asdict(result.configuration)
            if result.active is None:
                config.pop("active_policy_parameters")
                config.pop("reserve_policy_parameters")
            data["configuration"] = config
        if result.active is not None:
            data["active"] = asdict(result.active)
        return json.dumps(
            {
                "schema_version": 5
                if result.active is not None and result.active.policy_version >= 2
                else 4
                if result.active is not None
                else 3
                if result.configuration is not None
                else 2,
                "data": data,
            },
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_encode_value,
        )

    @staticmethod
    def decode(text: str) -> AnalysisResult:
        def reject(_value):
            raise ValueError("Non-string financial value.")

        def pairs(values):
            result = {}
            for key, value in values:
                require(key not in result, "Duplicate JSON key.")
                result[key] = value
            return result

        try:
            value = _object(
                json.loads(
                    text, parse_float=reject, parse_constant=reject, object_pairs_hook=pairs
                ),
                {"schema_version", "data"},
            )
            require(
                type(value["schema_version"]) is int and value["schema_version"] in {1, 2, 3, 4, 5},
                "Unsupported result version.",
            )
            d = _object(
                value["data"],
                {
                    "run_id",
                    "snapshot_id",
                    "generated_at",
                    "market_cutoff",
                    "market_as_of",
                    "is_live",
                    "market_regime",
                    "regime_policy_version",
                    "recommendation_policy_version",
                    "financial",
                    "warnings",
                    "analyzed_assets",
                    "recommendations",
                }
                | (
                    {"reasoning_mode", "reasoning_model", "ai_warning"}
                    if value["schema_version"] >= 2
                    else set()
                )
                | ({"configuration"} if value["schema_version"] >= 3 else set())
                | ({"active"} if value["schema_version"] >= 4 else set()),
            )
            assets = []
            for raw in _array(d["analyzed_assets"]):
                a = _object(
                    raw,
                    {f.name for f in fields(MarketAssetData)}
                    - ({"daily_context"} if value["schema_version"] < 4 else set())
                    - ({"closed_1h_candles"} if value["schema_version"] < 5 else set()),
                )
                f = _object(a["technical_features"], {f.name for f in fields(TechnicalFeatures)})
                daily = (
                    _object(a["daily_context"], {f.name for f in fields(DailyContext)})
                    if value["schema_version"] >= 4
                    else None
                )
                assets.append(
                    MarketAssetData(
                        a["symbol"],
                        parse_decimal(a["price_usd"], positive=True),
                        _date(a["quoted_at"]),
                        _date(a["latest_closed_candle_at"])
                        if a["latest_closed_candle_at"]
                        else None,
                        TechnicalFeatures(
                            **{k: parse_decimal(v) if v is not None else None for k, v in f.items()}
                        ),
                        DataCompleteness(a["completeness"]),
                        parse_decimal(a["latest_closed_price_usd"], positive=True)
                        if a["latest_closed_price_usd"] is not None
                        else None,
                        DailyContext(
                            _date(daily["latest_closed_at"]) if daily["latest_closed_at"] else None,
                            *(
                                parse_decimal(daily[name], positive=True)
                                if daily[name] is not None
                                else None
                                for name in (
                                    "closed_price_usd",
                                    "ema20",
                                    "ema50",
                                    "support",
                                    "resistance",
                                )
                            ),
                            DailyTrend(daily["trend"]),
                        )
                        if value["schema_version"] >= 4
                        else DailyContext(),
                        tuple(
                            Candle(
                                _date(c["closed_at"]),
                                parse_decimal(c["open"], positive=True),
                                parse_decimal(c["high"], positive=True),
                                parse_decimal(c["low"], positive=True),
                                parse_decimal(c["close"], positive=True),
                            )
                            for item in _array(a.get("closed_1h_candles", []))
                            for c in (
                                _object(
                                    item,
                                    {"closed_at", "open", "high", "low", "close"},
                                ),
                            )
                        ),
                    )
                )
            recommendations = []
            for raw in _array(d["recommendations"]):
                r = dict(
                    _object(
                        raw,
                        {f.name for f in fields(Recommendation)}
                        - ({"setup_id"} if value["schema_version"] < 5 else set()),
                    )
                )
                r.setdefault("setup_id", None)
                r["recommendation_id"] = _uuid(r["recommendation_id"])
                r["action_type"] = ActionType(r["action_type"])
                if r["existing_order_id"] is not None:
                    r["existing_order_id"] = _uuid(r["existing_order_id"])
                if r["setup_id"] is not None:
                    r["setup_id"] = _uuid(r["setup_id"])
                if r["side"] is not None:
                    r["side"] = TradingSide(r["side"])
                for k in ("target_price", "quantity_asset"):
                    if r[k] is not None:
                        r[k] = parse_decimal(r[k], positive=True)
                for k in ("review_at", "expires_at"):
                    if r[k] is not None:
                        r[k] = _date(r[k])
                recommendations.append(Recommendation(**r))
            financial = _object(
                d["financial"],
                {f.name for f in fields(FinancialSummary)}
                - (
                    {
                        "hard_expense_reserve_usd",
                        "market_buffer_usd",
                        "potential_trading_liquidity_usd",
                        "strategy_dry_powder_usd",
                        "recommended_deployment_usd",
                    }
                    if value["schema_version"] < 4
                    else set()
                ),
            )
            result = AnalysisResult(
                _uuid(d["run_id"]),
                _uuid(d["snapshot_id"]),
                _date(d["generated_at"]),
                MarketSnapshot(_date(d["market_as_of"]), tuple(assets), d["is_live"]),
                MarketRegime(d["market_regime"]),
                d["regime_policy_version"],
                d["recommendation_policy_version"],
                FinancialSummary(**{k: parse_decimal(v) for k, v in financial.items()}),
                tuple(_array(d["warnings"])),
                tuple(recommendations),
                ReasoningMode(d["reasoning_mode"])
                if value["schema_version"] >= 2
                else ReasoningMode.DETERMINISTIC,
                d.get("reasoning_model"),
                d.get("ai_warning"),
                DecisionConfiguration(
                    **_object(
                        d["configuration"],
                        {f.name for f in fields(DecisionConfiguration)}
                        - (
                            {"active_policy_parameters", "reserve_policy_parameters"}
                            if value["schema_version"] == 3
                            else set()
                        ),
                    )
                )
                if value["schema_version"] >= 3
                else None,
                _decode_active(d["active"], value["schema_version"])
                if value["schema_version"] >= 4
                else None,
            )
            require(
                (_date(d["market_cutoff"]) if d["market_cutoff"] is not None else None)
                == result.market_cutoff,
                "Inconsistent result cutoff.",
            )
            return result
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError):
            raise ValueError("Malformed or unsupported analysis result.") from None
