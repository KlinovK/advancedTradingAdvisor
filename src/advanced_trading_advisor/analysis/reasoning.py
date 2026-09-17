"""Provider-neutral optional reasoning boundary; never writes confirmed state."""

from dataclasses import replace
from enum import StrEnum
from typing import Protocol

from advanced_trading_advisor.domain.persistence import FailureCategory
from advanced_trading_advisor.market.models import MarketSnapshot
from advanced_trading_advisor.portfolio.models import ConfirmedPortfolioSnapshot
from advanced_trading_advisor.risk.assessment import PortfolioRiskAssessment

from .policy import RecommendationPolicy
from .recommendations import Recommendation


class ReasoningMode(StrEnum):
    DETERMINISTIC = "DETERMINISTIC"
    AI_ASSISTED = "AI_ASSISTED"
    AI_FALLBACK = "AI_FALLBACK"


AI_WARNING = "AI reasoning unavailable; deterministic recommendations used."


class ReasoningUnavailable(RuntimeError):
    def __init__(self, category=FailureCategory.AI_PROVIDER):
        self.category = category
        super().__init__(AI_WARNING)


class RecommendationReasoner(Protocol):
    @property
    def model_name(self) -> str: ...

    async def propose(
        self,
        snapshot: ConfirmedPortfolioSnapshot,
        market: MarketSnapshot,
        risk: PortfolioRiskAssessment,
        policy: RecommendationPolicy,
        active: object,
        baseline_actions: tuple[Recommendation, ...],
    ) -> tuple[Recommendation, ...]: ...


def safe_commentary(actions: tuple[Recommendation, ...]) -> tuple[Recommendation, ...]:
    # No model-authored prose survives, even for accepted actions: a benign HOLD reason
    # could otherwise smuggle an unrelated unsafe trade. Numeric terms are kept unchanged.
    return tuple(
        replace(
            a,
            reason=f"{a.action_type.value.upper().replace('_', ' ')} proposal passed whole-plan "
            "checks against the recorded snapshot. Review manually; nothing was executed.",
            invalidation_condition=None,
        )
        for a in actions
    )
