import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from advanced_trading_advisor.domain.validation import identity, require, timestamp


class DeliveryStatus(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    AMBIGUOUS = "AMBIGUOUS"


class DeliveryFailureCategory(StrEnum):
    TELEGRAM_CONFIGURATION = "telegram_configuration"
    TELEGRAM_TIMEOUT = "telegram_timeout"
    TELEGRAM_TRANSPORT = "telegram_transport"
    TELEGRAM_RATE_LIMITED = "telegram_rate_limited"
    TELEGRAM_UNAUTHORIZED = "telegram_unauthorized"
    TELEGRAM_FORBIDDEN = "telegram_forbidden"
    TELEGRAM_BAD_REQUEST = "telegram_bad_request"
    TELEGRAM_PROVIDER = "telegram_provider"
    TELEGRAM_AMBIGUOUS = "telegram_ambiguous"


def utc(value):
    timestamp(value)
    require(value.utcoffset() == timedelta(0), "Delivery timestamps must be UTC.")


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    delivery_id: UUID
    analysis_run_id: UUID
    destination_key: str
    created_at: datetime
    status: DeliveryStatus = DeliveryStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    attempt_count: int = 0
    failure_category: DeliveryFailureCategory | None = None
    channel: str = "TELEGRAM"

    def __post_init__(self):
        identity(self.delivery_id)
        identity(self.analysis_run_id)
        require(self.delivery_id != self.analysis_run_id, "Delivery and analysis IDs must differ.")
        require(self.channel == "TELEGRAM", "Unsupported delivery channel.")
        require(
            isinstance(self.destination_key, str)
            and re.fullmatch(r"[0-9a-f]{64}", self.destination_key) is not None,
            "Invalid destination identity.",
        )
        utc(self.created_at)
        require(isinstance(self.status, DeliveryStatus), "Invalid delivery status.")
        require(type(self.attempt_count) is int and self.attempt_count >= 0, "Invalid attempts.")
        if self.started_at is not None:
            utc(self.started_at)
            require(self.started_at >= self.created_at, "Delivery start precedes creation.")
        if self.finished_at is not None:
            utc(self.finished_at)
            require(
                self.started_at is not None and self.finished_at >= self.started_at,
                "Delivery finish precedes start.",
            )
        if self.status is DeliveryStatus.PENDING:
            require(
                self.started_at is None
                and self.finished_at is None
                and self.attempt_count == 0
                and self.failure_category is None,
                "Pending delivery has an outcome.",
            )
        elif self.status is DeliveryStatus.SENDING:
            require(
                self.started_at is not None
                and self.finished_at is None
                and self.failure_category is None,
                "Invalid sending state.",
            )
        elif self.status is DeliveryStatus.DELIVERED:
            require(
                self.finished_at is not None
                and self.attempt_count > 0
                and self.failure_category is None,
                "Invalid delivered state.",
            )
        else:
            require(
                self.finished_at is not None
                and self.attempt_count > 0
                and isinstance(self.failure_category, DeliveryFailureCategory),
                "Failed delivery requires sanitized category.",
            )
            if self.status is DeliveryStatus.AMBIGUOUS:
                require(
                    self.failure_category is DeliveryFailureCategory.TELEGRAM_AMBIGUOUS,
                    "Ambiguous delivery requires its own category.",
                )
