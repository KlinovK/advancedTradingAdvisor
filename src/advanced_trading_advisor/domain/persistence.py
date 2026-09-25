from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.domain.validation import identity, require, timestamp
from advanced_trading_advisor.portfolio.models import ConfirmedPortfolioSnapshot


class PersistenceError(RuntimeError):
    """Persistent state could not be stored or restored."""


class ConflictError(PersistenceError):
    """An immutable identity or current revision conflicts."""


class SchemaVersionError(PersistenceError):
    """Database schema is unsupported or incompatible."""


class PayloadError(PersistenceError):
    """Stored payload is malformed or has an unsupported version."""


class UpdateType(StrEnum):
    INITIAL_STATE = "initial_state"
    ACCOUNT_CREATED = "account_created"
    BALANCE_CORRECTION = "balance_correction"
    EXPENSE_SETTINGS_CHANGE = "expense_settings_change"
    CORE_CONFIGURATION_CHANGE = "core_configuration_change"
    ORDER_ADDED = "order_added"
    ORDER_FILLED = "order_filled"
    ORDER_CANCELLED = "order_cancelled"
    ORDER_EXPIRED = "order_expired"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    SPOT_TRADE_RECORDED = "spot_trade_recorded"


@dataclass(frozen=True, slots=True)
class ConfirmedUpdate:
    update_id: UUID
    update_type: UpdateType
    previous_snapshot_id: UUID | None
    new_snapshot_id: UUID
    confirmed_at: datetime

    def __post_init__(self) -> None:
        identity(self.update_id)
        identity(self.new_snapshot_id)
        timestamp(self.confirmed_at)
        require(isinstance(self.update_type, UpdateType), "Invalid update type.")
        if self.previous_snapshot_id is not None:
            identity(self.previous_snapshot_id)
            require(
                self.previous_snapshot_id != self.new_snapshot_id, "Update needs a new snapshot."
            )
        require(
            (self.previous_snapshot_id is None) == (self.update_type is UpdateType.INITIAL_STATE),
            "Only initial state has no previous snapshot.",
        )


@dataclass(frozen=True, slots=True)
class CurrentPortfolio:
    snapshot: ConfirmedPortfolioSnapshot
    revision: int

    def __post_init__(self) -> None:
        require(isinstance(self.snapshot, ConfirmedPortfolioSnapshot), "Invalid snapshot.")
        require(type(self.revision) is int and self.revision > 0, "Invalid revision.")


class FailureCategory(StrEnum):
    ANALYSIS_BUSY = "analysis_busy"
    CONFIGURATION = "configuration"
    DATABASE = "database"
    MARKET_VALIDATION = "market_validation"
    AI_TIMEOUT = "ai_timeout"
    AI_AUTHENTICATION_PERMISSION = "ai_authentication_permission"
    AI_RATE_LIMIT = "ai_rate_limit"
    AI_PROVIDER_SERVER = "ai_provider_server"
    AI_TRANSPORT = "ai_transport"
    AI_REFUSAL = "ai_refusal"
    AI_INCOMPLETE = "ai_incomplete"
    AI_PROVIDER = "ai_provider"
    AI_SCHEMA = "ai_schema"
    PLAN_VALIDATION = "plan_validation"
    AI_VALIDATOR_REJECTED = "validator_rejected"
    AI_EXACT_TERMS_MISMATCH = "exact_terms_mismatch"
    INTERRUPTED = "interrupted"
    INVALID_INPUT = "invalid_input"
    MARKET_UNAVAILABLE = "market_unavailable"
    TIMEOUT = "timeout"
    ANALYSIS_FAILED = "analysis_failed"


@dataclass(frozen=True, slots=True)
class StoredAnalysisRun:
    run: AnalysisRun
    result_json: str | None = None
    failure_category: FailureCategory | None = None

    def __post_init__(self) -> None:
        require(isinstance(self.run, AnalysisRun), "Invalid analysis run.")
        if self.result_json is not None:
            require(
                isinstance(self.result_json, str)
                and self.run.status is AnalysisRunStatus.COMPLETED,
                "Only completed runs may contain result JSON.",
            )
        require(
            (self.failure_category is not None) == (self.run.status is AnalysisRunStatus.FAILED),
            "Failed runs require a sanitized failure category.",
        )
        if self.failure_category is not None:
            require(isinstance(self.failure_category, FailureCategory), "Invalid failure category.")
