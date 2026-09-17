import asyncio
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

from advanced_trading_advisor.domain.persistence import FailureCategory, PersistenceError
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.market.errors import (
    MalformedMarketDataError,
    MarketCutoffMismatchError,
    MarketDataError,
    StaleMarketDataError,
    UnsupportedSymbolError,
)
from advanced_trading_advisor.market.provider import MarketDataProvider
from advanced_trading_advisor.market.service import MarketDataService
from advanced_trading_advisor.portfolio.repositories import SnapshotRepository
from advanced_trading_advisor.risk.assessment import PortfolioRiskPolicy
from advanced_trading_advisor.risk.reserve import StableReservePolicy

from .active import ActiveManagementEngine, ActiveManagementPolicy
from .models import AnalysisRun
from .plan import ActivePlanValidator, ValidatedPlan
from .policy import RecommendationPolicy
from .reasoning import AI_WARNING, ReasoningMode, RecommendationReasoner, safe_commentary
from .recommendations import canonical_action_terms
from .recovery import ANALYSIS_TIMEOUT_SECONDS
from .repositories import AnalysisRunRepository
from .result import AnalysisResult, AnalysisResultCodec, FinancialSummary
from .traceability import DecisionConfiguration, configuration_parameters, policy_parameters


class AnalysisFailed(RuntimeError):
    pass


class AnalyzeCurrentPortfolio:
    def __init__(
        self,
        snapshots: SnapshotRepository,
        runs: AnalysisRunRepository,
        provider_factory: Callable[[], MarketDataProvider],
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        policy: RecommendationPolicy | None = None,
        active_policy: ActiveManagementPolicy | None = None,
        reserve_policy: StableReservePolicy | None = None,
        reasoner: RecommendationReasoner | None = None,
        prepare_report: Callable[[AnalysisResult], object] | None = None,
        run_id: UUID | None = None,
    ):
        self.snapshots, self.runs, self.provider_factory = snapshots, runs, provider_factory
        self.now = now
        self.reasoner = reasoner
        self.prepare_report = prepare_report
        self.run_id = run_id
        policy = policy or RecommendationPolicy()
        self.engine = ActiveManagementEngine(active_policy, terms_policy=policy)
        self.validator = ActivePlanValidator(policy)
        self.legacy_policy = policy
        self.reserve_policy = reserve_policy or StableReservePolicy()

    async def execute(self) -> AnalysisResult:
        try:
            async with asyncio.timeout(ANALYSIS_TIMEOUT_SECONDS):
                return await self._execute()
        except TimeoutError:
            raise AnalysisFailed(
                "Analysis execution timed out; inspect status before retrying."
            ) from None

    async def _execute(self) -> AnalysisResult:
        current = await self.snapshots.get_current()
        if current is None:
            raise AnalysisFailed("No confirmed portfolio. Run setup first.")
        snapshot = current.snapshot
        run = AnalysisRun.start(snapshot.id, self.now())
        if self.run_id is not None:
            run = replace(run, run_id=self.run_id)
        stage = "database"
        try:
            await self.runs.acquire(run)
            previous_record = await self.runs.latest_completed()
            previous_result = (
                AnalysisResultCodec.decode(previous_record.result_json)
                if previous_record is not None
                else None
            )
            stage = "configuration"
            provider = self.provider_factory()
            stage = "market"
            market = await MarketDataService(provider).for_snapshot(snapshot)
            stage = "analysis"
            generated = self.now()
            risk_policy = PortfolioRiskPolicy(reserve_policy=self.reserve_policy)
            risk = risk_policy.assess(snapshot, market, now=generated)
            active, candidates = self.engine.evaluate(
                snapshot,
                market,
                risk,
                previous_result.active if previous_result else None,
                previous_result.market_regime if previous_result else None,
                previous_result.warnings if previous_result else None,
            )
            plan = self.validator.validate_active(candidates, snapshot, market, risk, active)
            mode, model, warning = ReasoningMode.DETERMINISTIC, None, None
            ai_category = None
            if self.reasoner is not None:
                model = self.reasoner.model_name
                mode, warning = ReasoningMode.AI_FALLBACK, AI_WARNING
                try:
                    proposals = await self.reasoner.propose(
                        snapshot,
                        market,
                        risk,
                        self.legacy_policy,
                        active,
                        plan.recommendations,
                    )
                    alternative = self.validator.validate_active(
                        proposals, snapshot, market, risk, active
                    )

                    if alternative.rejected:
                        ai_category = FailureCategory.AI_VALIDATOR_REJECTED.value
                    elif canonical_action_terms(proposals) != canonical_action_terms(
                        plan.recommendations
                    ):
                        ai_category = FailureCategory.AI_EXACT_TERMS_MISMATCH.value
                    else:
                        plan = ValidatedPlan(safe_commentary(alternative.recommendations), False)
                        mode, warning = ReasoningMode.AI_ASSISTED, None
                except Exception as error:
                    # Runtime AI failure does not discard a successful deterministic analysis.
                    # Cancellation still propagates to the normal failed-run lifecycle.
                    category = getattr(error, "category", FailureCategory.AI_PROVIDER)
                    ai_category = (
                        category.value
                        if category
                        in {
                            FailureCategory.AI_TIMEOUT,
                            FailureCategory.AI_AUTHENTICATION_PERMISSION,
                            FailureCategory.AI_RATE_LIMIT,
                            FailureCategory.AI_PROVIDER_SERVER,
                            FailureCategory.AI_TRANSPORT,
                            FailureCategory.AI_REFUSAL,
                            FailureCategory.AI_INCOMPLETE,
                            FailureCategory.AI_PROVIDER,
                            FailureCategory.AI_SCHEMA,
                        }
                        else FailureCategory.AI_PROVIDER.value
                    )
            generated = self.now()
            v, r = risk.valuation, risk.reserve
            result = AnalysisResult(
                run.run_id,
                snapshot.id,
                generated,
                market,
                risk.market_regime.regime,
                risk.market_regime.policy_version,
                self.legacy_policy.version,
                FinancialSummary(
                    v.total_portfolio_usd,
                    v.current_stables_usd,
                    v.stable_allocation_pct,
                    r.monthly_expenses_usd,
                    r.target_expense_runway_months,
                    r.recommended_minimum_stables_usd,
                    r.deployable_stables_usd,
                    r.stable_reserve_deficit_usd,
                    r.actual_expense_runway_months,
                    v.open_buy_commitments_usd,
                    r.hard_reserve_usd,
                    r.market_buffer_usd,
                    r.potential_trading_liquidity_usd,
                    active.strategy_dry_powder_usd,
                    active.recommended_deployment_usd,
                ),
                tuple(w.value for w in risk.warnings)
                + (("candidate_plan_rejected",) if plan.rejected else ()),
                plan.recommendations,
                mode,
                model,
                warning,
                DecisionConfiguration(
                    risk.market_regime.policy_version,
                    risk.reserve.policy_version,
                    self.validator.version,
                    format(r.target_expense_runway_months, "f"),
                    "live" if market.is_live else "static",
                    self.reasoner is not None,
                    model,
                    getattr(self.reasoner, "reasoning_effort", None),
                    *getattr(self.reasoner, "contract_versions", (None, None, None)),
                    ai_category,
                    policy_parameters(self.legacy_policy),
                    configuration_parameters(self.engine.policy),
                    configuration_parameters(risk_policy.reserve_policy.config),
                ),
                active,
            )
            if self.prepare_report is not None:
                self.prepare_report(result)
            stage = "database"
            await self.runs.mark_completed(
                run.run_id,
                completed_at=generated,
                result_json=AnalysisResultCodec.encode(result),
                market_cutoff=result.market_cutoff,
            )
            return result
        except (Exception, asyncio.CancelledError) as error:
            category = (
                FailureCategory.INTERRUPTED
                if isinstance(error, asyncio.CancelledError)
                else FailureCategory.CONFIGURATION
                if stage == "configuration"
                else FailureCategory.DATABASE
                if stage == "database" or isinstance(error, PersistenceError)
                else FailureCategory.MARKET_VALIDATION
                if isinstance(
                    error,
                    (
                        MalformedMarketDataError,
                        StaleMarketDataError,
                        MarketCutoffMismatchError,
                        UnsupportedSymbolError,
                    ),
                )
                else FailureCategory.MARKET_UNAVAILABLE
                if isinstance(error, MarketDataError)
                else FailureCategory.TIMEOUT
                if isinstance(error, TimeoutError)
                else FailureCategory.INVALID_INPUT
                if isinstance(error, DomainValidationError)
                else FailureCategory.ANALYSIS_FAILED
            )
            try:
                stored = await self.runs.get(run.run_id)
                if stored is not None and stored.run.status.value == "running":
                    await self.runs.mark_failed(
                        run.run_id, completed_at=self.now(), failure_category=category
                    )
            except Exception:
                raise AnalysisFailed(
                    "Analysis failed and run status could not be saved; inspect local storage."
                ) from None
            if isinstance(error, asyncio.CancelledError):
                raise
            if stored is None and isinstance(error, PersistenceError):
                raise error from None
            raise AnalysisFailed(
                f"Analysis failed ({category.value}); run {run.run_id}. "
                "Confirmed portfolio unchanged."
            ) from None
