"""Exact financial inputs with an explicit, provisional domain precision policy."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from advanced_trading_advisor.domain.validation import identity, require, symbol

MAX_DIGITS = 60
MAX_SCALE = 24
QUANTUM = Decimal("1e-24")


def exact_decimal(value: object, *, nonnegative: bool = False, positive: bool = False) -> Decimal:
    require(isinstance(value, Decimal) and value.is_finite(), "Finite Decimal required.")
    require(
        len(value.as_tuple().digits) <= MAX_DIGITS and value.adjusted() < MAX_DIGITS,
        "Decimal has too many digits.",
    )
    require(value.as_tuple().exponent >= -MAX_SCALE, "Decimal scale exceeds 24 places.")
    if nonnegative or positive:
        require(value >= 0, "Negative amount is not allowed.")
    if positive:
        require(value > 0, "Amount must be positive.")
    return value


@dataclass(frozen=True, slots=True)
class AssetId:
    value: str

    def __post_init__(self) -> None:
        symbol(self.value)


USDT = AssetId("USDT")


@dataclass(frozen=True, slots=True)
class AssetQuantity:
    value: Decimal

    def __post_init__(self) -> None:
        exact_decimal(self.value, nonnegative=True)


@dataclass(frozen=True, slots=True)
class USDTAmount:
    value: Decimal

    def __post_init__(self) -> None:
        exact_decimal(self.value, nonnegative=True)


@dataclass(frozen=True, slots=True)
class OperationId:
    value: UUID

    def __post_init__(self) -> None:
        identity(self.value)


@dataclass(frozen=True, slots=True)
class LedgerSequence:
    value: int

    def __post_init__(self) -> None:
        require(type(self.value) is int and self.value > 0, "Ledger sequence must be positive.")


@dataclass(frozen=True, slots=True)
class UTCTimestamp:
    value: datetime

    def __post_init__(self) -> None:
        require(
            isinstance(self.value, datetime)
            and self.value.tzinfo is not None
            and self.value.utcoffset() == timedelta(0),
            "Timestamp must be timezone-aware UTC.",
        )
