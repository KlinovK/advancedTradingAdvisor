"""Immutable operations and exact asset postings for fee-free Spot trades."""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from advanced_trading_advisor.domain.validation import DomainValidationError, require

from .values import (
    USDT,
    AssetId,
    AssetQuantity,
    LedgerSequence,
    OperationId,
    USDTAmount,
    UTCTimestamp,
    exact_decimal,
)


class OperationType(StrEnum):
    OPENING_BALANCE = "opening_balance"
    BUY = "buy"
    SELL = "sell"


class PostingRole(StrEnum):
    OPENING = "opening"
    TRADED_ASSET = "traded_asset"
    SETTLEMENT = "settlement"


class Completeness(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class OpeningFacts:
    asset: AssetId
    quantity: AssetQuantity
    known_cost_usdt: USDTAmount | None
    incomplete_quantity: AssetQuantity
    completeness: Completeness

    def __post_init__(self) -> None:
        require(isinstance(self.asset, AssetId), "Opening asset required.")
        require(isinstance(self.quantity, AssetQuantity), "Opening quantity required.")
        require(self.quantity.value > 0, "Opening quantity must be positive.")
        require(
            isinstance(self.incomplete_quantity, AssetQuantity)
            and self.incomplete_quantity.value <= self.quantity.value,
            "Invalid incomplete quantity.",
        )
        require(isinstance(self.completeness, Completeness), "Invalid cost completeness.")
        if self.completeness is Completeness.COMPLETE:
            require(
                isinstance(self.known_cost_usdt, USDTAmount)
                and self.incomplete_quantity.value == 0,
                "Complete opening requires known cost and no incomplete quantity.",
            )
        elif self.completeness is Completeness.UNKNOWN:
            require(
                self.known_cost_usdt is None
                and self.incomplete_quantity.value == self.quantity.value,
                "Unknown opening requires fully incomplete quantity and no cost.",
            )
        else:
            require(
                isinstance(self.known_cost_usdt, USDTAmount) and self.incomplete_quantity.value > 0,
                "Partial opening requires known cost component and incomplete quantity.",
            )
            require(
                self.incomplete_quantity.value < self.quantity.value,
                "Partial opening must identify a known quantity.",
            )
        if self.asset == USDT:
            require(
                self.completeness is Completeness.COMPLETE
                and self.known_cost_usdt.value == self.quantity.value,
                "USDT opening cost must equal its quantity.",
            )


@dataclass(frozen=True, slots=True)
class BuyFacts:
    asset: AssetId
    quantity: AssetQuantity
    principal_usdt: USDTAmount

    def __post_init__(self) -> None:
        require(isinstance(self.asset, AssetId) and self.asset != USDT, "Invalid buy asset.")
        require(
            isinstance(self.quantity, AssetQuantity) and self.quantity.value > 0,
            "Invalid buy quantity.",
        )
        require(
            isinstance(self.principal_usdt, USDTAmount) and self.principal_usdt.value > 0,
            "Invalid USDT principal.",
        )


@dataclass(frozen=True, slots=True)
class SellFacts:
    asset: AssetId
    quantity: AssetQuantity
    proceeds_usdt: USDTAmount

    def __post_init__(self) -> None:
        require(isinstance(self.asset, AssetId) and self.asset != USDT, "Invalid sell asset.")
        require(
            isinstance(self.quantity, AssetQuantity) and self.quantity.value > 0,
            "Invalid sell quantity.",
        )
        require(
            isinstance(self.proceeds_usdt, USDTAmount) and self.proceeds_usdt.value > 0,
            "Invalid USDT proceeds.",
        )


@dataclass(frozen=True, slots=True)
class BalancePosting:
    ordinal: int
    asset: AssetId
    delta: Decimal
    role: PostingRole

    def __post_init__(self) -> None:
        require(type(self.ordinal) is int and self.ordinal > 0, "Invalid posting ordinal.")
        require(isinstance(self.asset, AssetId), "Posting asset required.")
        exact_decimal(self.delta)
        require(self.delta != 0, "Posting delta must be nonzero.")
        require(isinstance(self.role, PostingRole), "Invalid posting role.")


@dataclass(frozen=True, slots=True)
class LedgerOperation:
    id: OperationId
    sequence: LedgerSequence
    executed_at: UTCTimestamp
    recorded_at: UTCTimestamp
    type: OperationType
    facts: OpeningFacts | BuyFacts | SellFacts
    postings: tuple[BalancePosting, ...]

    def __post_init__(self) -> None:
        require(isinstance(self.id, OperationId), "Operation ID required.")
        require(isinstance(self.sequence, LedgerSequence), "Ledger sequence required.")
        require(
            isinstance(self.executed_at, UTCTimestamp)
            and isinstance(self.recorded_at, UTCTimestamp)
            and self.recorded_at.value >= self.executed_at.value,
            "Recording cannot precede execution.",
        )
        require(isinstance(self.type, OperationType), "Invalid operation type.")
        require(isinstance(self.postings, tuple), "Postings must be immutable tuple.")
        if self.type is OperationType.OPENING_BALANCE and isinstance(self.facts, OpeningFacts):
            expected = (
                BalancePosting(1, self.facts.asset, self.facts.quantity.value, PostingRole.OPENING),
            )
        elif self.type is OperationType.BUY and isinstance(self.facts, BuyFacts):
            expected = (
                BalancePosting(
                    1, self.facts.asset, self.facts.quantity.value, PostingRole.TRADED_ASSET
                ),
                BalancePosting(2, USDT, -self.facts.principal_usdt.value, PostingRole.SETTLEMENT),
            )
        elif self.type is OperationType.SELL and isinstance(self.facts, SellFacts):
            expected = (
                BalancePosting(
                    1, self.facts.asset, -self.facts.quantity.value, PostingRole.TRADED_ASSET
                ),
                BalancePosting(2, USDT, self.facts.proceeds_usdt.value, PostingRole.SETTLEMENT),
            )
        else:
            raise DomainValidationError("Operation type and facts disagree.")
        require(
            self.postings == expected,
            "Postings contradict operation facts or include unsupported fees.",
        )
