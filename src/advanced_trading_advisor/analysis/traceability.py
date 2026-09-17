"""Allowlisted non-secret decision provenance, never an environment/settings dump."""

import json
from dataclasses import asdict, dataclass
from decimal import Decimal

from advanced_trading_advisor.domain.numeric import parse_decimal
from advanced_trading_advisor.domain.persistence import FailureCategory
from advanced_trading_advisor.domain.validation import require


@dataclass(frozen=True, slots=True)
class DecisionConfiguration:
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
    active_policy_parameters: str | None = None
    reserve_policy_parameters: str | None = None

    def __post_init__(self):
        require(
            type(self.market_regime_version) is int and self.market_regime_version in {1, 2},
            "Unsupported market regime policy version.",
        )
        require(
            type(self.whole_plan_version) is int and self.whole_plan_version in {1, 2, 3},
            "Unsupported whole-plan version.",
        )
        require(
            type(self.stable_reserve_version) is int and self.stable_reserve_version in {1, 2},
            "Unsupported reserve policy version.",
        )
        parse_decimal(self.target_runway_months, positive=True)
        require(self.market_source in {"live", "static"}, "Invalid market source.")
        require(type(self.ai_enabled) is bool, "Invalid AI flag.")
        require((self.model_name is not None) == self.ai_enabled, "Invalid model provenance.")
        if self.model_name is not None:
            require(
                isinstance(self.model_name, str)
                and 0 < len(self.model_name) <= 100
                and all(c.isascii() and (c.isalnum() or c in "._:-") for c in self.model_name),
                "Invalid model identifier.",
            )
        require(
            self.reasoning_effort in {None, "none", "low", "medium", "high", "xhigh", "max"},
            "Invalid reasoning effort.",
        )
        for version in (self.ai_context_version, self.ai_prompt_version):
            require(
                version is None or type(version) is int and version in {1, 2, 3, 4},
                "Unsupported AI contract version.",
            )
        require(
            self.ai_schema_version is None
            or type(self.ai_schema_version) is int
            and self.ai_schema_version in {1, 2, 3},
            "Unsupported AI schema version.",
        )
        require(
            self.ai_failure_category
            in {
                None,
                FailureCategory.AI_TIMEOUT.value,
                FailureCategory.AI_AUTHENTICATION_PERMISSION.value,
                FailureCategory.AI_RATE_LIMIT.value,
                FailureCategory.AI_PROVIDER_SERVER.value,
                FailureCategory.AI_TRANSPORT.value,
                FailureCategory.AI_REFUSAL.value,
                FailureCategory.AI_INCOMPLETE.value,
                FailureCategory.AI_PROVIDER.value,
                FailureCategory.AI_SCHEMA.value,
                FailureCategory.PLAN_VALIDATION.value,
                FailureCategory.AI_VALIDATOR_REJECTED.value,
                FailureCategory.AI_EXACT_TERMS_MISMATCH.value,
            },
            "Invalid AI failure category.",
        )
        if not self.ai_enabled:
            require(
                all(
                    v is None
                    for v in (
                        self.reasoning_effort,
                        self.ai_context_version,
                        self.ai_schema_version,
                        self.ai_prompt_version,
                        self.ai_failure_category,
                    )
                ),
                "Disabled AI cannot claim provenance.",
            )
        from advanced_trading_advisor.market.regime import MarketRegime

        from .policy import RecommendationPolicy

        params = json.loads(self.recommendation_parameters)
        require(
            isinstance(params, dict) and set(params) == set(asdict(RecommendationPolicy())),
            "Invalid policy parameters.",
        )
        values = {
            k: parse_decimal(v, positive=True)
            for k, v in params.items()
            if k not in {"version", "budget_fractions"}
        }
        policy = RecommendationPolicy(
            version=params["version"],
            **values,
            budget_fractions=tuple(
                (MarketRegime(r), parse_decimal(v)) for r, v in params["budget_fractions"]
            ),
        )
        require(
            policy_parameters(policy) == self.recommendation_parameters,
            "Policy parameters must use canonical serialization.",
        )
        for encoded in (self.active_policy_parameters, self.reserve_policy_parameters):
            if encoded is not None:
                require(
                    isinstance(encoded, str) and len(encoded) <= 10000, "Invalid policy parameters."
                )
                parsed = json.loads(encoded)
                require(
                    isinstance(parsed, dict) and parsed.get("version") in {1, 2},
                    "Invalid policy parameters.",
                )
                require(
                    json.dumps(parsed, sort_keys=True, separators=(",", ":")) == encoded,
                    "Noncanonical policy parameters.",
                )

    def serialize(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    def fingerprint_material(self):
        """Canonical configuration/policy evidence, excluding this run's outcome."""
        value = asdict(self)
        value.pop("ai_failure_category")
        return json.dumps(value, sort_keys=True, separators=(",", ":"))


def policy_parameters(policy):
    def encode(v):
        if isinstance(v, Decimal):
            return format(v, "f")
        raise TypeError("Unsupported policy parameter.")

    return json.dumps(asdict(policy), default=encode, sort_keys=True, separators=(",", ":"))


def configuration_parameters(policy):
    """Canonical, non-secret configuration evidence for newer deterministic policies."""
    return json.dumps(
        asdict(policy),
        default=lambda value: format(value, "f") if isinstance(value, Decimal) else str(value),
        sort_keys=True,
        separators=(",", ":"),
    )
