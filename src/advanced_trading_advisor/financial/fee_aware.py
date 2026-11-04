"""Immutable fee-aware Spot facts; accounting replay and persistence are separate phases."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from advanced_trading_advisor.domain.validation import identity, require

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


def _reference(value: object, label: str) -> None:
    require(
        type(value) is str
        and 0 < len(value) <= 200
        and value == value.strip()
        and all(32 <= ord(character) <= 126 for character in value),
        f"Invalid {label}.",
    )


def _reason(value: object) -> None:
    require(
        type(value) is str
        and 0 < len(value) <= 1000
        and value == value.strip()
        and all(character.isprintable() for character in value),
        "Invalid correction reason.",
    )


class TradeSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class FeeCategory(StrEnum):
    BASE_ASSET = "base_asset"
    QUOTE_ASSET = "quote_asset"
    THIRD_ASSET = "third_asset"


class SourceProvider(StrEnum):
    BINANCE_SPOT = "binance_spot"


class SourceNamespace(StrEnum):
    SPOT_TRADE = "spot_trade"
    SOR_ALLOCATION = "sor_allocation"


class PriceRecordKind(StrEnum):
    SPOT_EXECUTION = "spot_execution"


class OrderingBasis(StrEnum):
    PROVIDER_EXECUTION_ORDER = "provider_execution_order"
    OWNER_RESOLUTION = "owner_resolution"
    IMMATERIAL_TIE = "immaterial_tie"


def classify_fee(base: AssetId, quote: AssetId, fee_asset: AssetId) -> FeeCategory:
    """Classify a fee once, before any later accounting treatment."""
    require(
        isinstance(base, AssetId)
        and isinstance(quote, AssetId)
        and isinstance(fee_asset, AssetId)
        and base != quote,
        "Invalid fee assets.",
    )
    if fee_asset == base:
        return FeeCategory.BASE_ASSET
    if fee_asset == quote:
        return FeeCategory.QUOTE_ASSET
    return FeeCategory.THIRD_ASSET


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    portfolio_id: UUID
    provider: SourceProvider
    exchange_account_key: str
    symbol: AssetId
    namespace: SourceNamespace
    external_execution_id: str

    def __post_init__(self) -> None:
        identity(self.portfolio_id)
        require(isinstance(self.provider, SourceProvider), "Invalid source provider.")
        _reference(self.exchange_account_key, "exchange account key")
        require(isinstance(self.symbol, AssetId), "Invalid source symbol.")
        require(isinstance(self.namespace, SourceNamespace), "Invalid source namespace.")
        _reference(self.external_execution_id, "external execution ID")


@dataclass(frozen=True, slots=True)
class EventProvenance:
    source: SourceIdentity
    order_id: str
    evidence_reference: str
    recorded_at: UTCTimestamp
    append_sequence: LedgerSequence | None

    def __post_init__(self) -> None:
        require(isinstance(self.source, SourceIdentity), "Source identity required.")
        _reference(self.order_id, "order ID")
        _reference(self.evidence_reference, "event evidence reference")
        require(isinstance(self.recorded_at, UTCTimestamp), "Recording timestamp required.")
        require(
            self.append_sequence is None or isinstance(self.append_sequence, LedgerSequence),
            "Invalid append sequence.",
        )


@dataclass(frozen=True, slots=True)
class GrossTradeExecution:
    side: TradeSide
    base_asset: AssetId
    quote_asset: AssetId
    gross_base_quantity: AssetQuantity
    gross_quote_quantity: USDTAmount
    executed_at: UTCTimestamp

    def __post_init__(self) -> None:
        require(isinstance(self.side, TradeSide), "Invalid trade side.")
        require(
            isinstance(self.base_asset, AssetId)
            and self.base_asset != USDT
            and self.quote_asset == USDT,
            "Fee-aware trades require a non-USDT base and USDT quote.",
        )
        require(
            isinstance(self.gross_base_quantity, AssetQuantity)
            and self.gross_base_quantity.value > 0,
            "Gross base quantity must be positive.",
        )
        require(
            isinstance(self.gross_quote_quantity, USDTAmount)
            and self.gross_quote_quantity.value > 0,
            "Gross quote quantity must be positive.",
        )
        require(isinstance(self.executed_at, UTCTimestamp), "Execution timestamp required.")


def _epoch_microseconds(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


@dataclass(frozen=True, slots=True)
class HistoricalUSDTPrice:
    asset: AssetId
    quote_asset: AssetId
    source_symbol: AssetId
    record_kind: PriceRecordKind
    valued_at: UTCTimestamp
    price_usdt: Decimal
    provider: SourceProvider
    dataset_reference: str
    source_record_id: str
    evidence_reference: str
    source_resolution_us: int

    def __post_init__(self) -> None:
        require(isinstance(self.asset, AssetId) and self.asset != USDT, "Invalid valued asset.")
        require(
            self.quote_asset == USDT
            and isinstance(self.source_symbol, AssetId)
            and self.source_symbol.value == self.asset.value + USDT.value,
            "Historical price requires the valued asset/USDT source pair.",
        )
        require(
            self.record_kind is PriceRecordKind.SPOT_EXECUTION,
            "Historical price requires a Spot execution record.",
        )
        require(isinstance(self.valued_at, UTCTimestamp), "Valuation timestamp required.")
        exact_decimal(self.price_usdt, positive=True)
        require(self.provider is SourceProvider.BINANCE_SPOT, "Unsupported valuation provider.")
        _reference(self.dataset_reference, "valuation dataset")
        _reference(self.source_record_id, "valuation source record")
        _reference(self.evidence_reference, "valuation evidence reference")
        require(
            type(self.source_resolution_us) is int and self.source_resolution_us > 0,
            "Invalid source timestamp resolution.",
        )
        require(
            _epoch_microseconds(self.valued_at.value) % self.source_resolution_us == 0,
            "Valuation timestamp is not aligned to source resolution.",
        )

    def matches(self, executed_at: UTCTimestamp) -> bool:
        """Source precision is metadata, not permission for approximate accounting."""
        require(isinstance(executed_at, UTCTimestamp), "Execution timestamp required.")
        return self.valued_at.value == executed_at.value


@dataclass(frozen=True, slots=True)
class HistoricalUSDTValue:
    """A separately archived source record that directly states the fee's USDT value."""

    asset: AssetId
    valued_at: UTCTimestamp
    value_usdt: USDTAmount
    provider: SourceProvider
    dataset_reference: str
    source_record_id: str
    evidence_reference: str
    source_resolution_us: int
    fee_quantity: AssetQuantity
    execution_source: SourceIdentity

    def __post_init__(self) -> None:
        require(isinstance(self.asset, AssetId) and self.asset != USDT, "Invalid valued asset.")
        require(isinstance(self.valued_at, UTCTimestamp), "Valuation timestamp required.")
        require(
            isinstance(self.value_usdt, USDTAmount) and self.value_usdt.value > 0,
            "Direct USDT value must be positive.",
        )
        require(
            isinstance(self.fee_quantity, AssetQuantity) and self.fee_quantity.value > 0,
            "Direct USDT value requires a positive fee quantity.",
        )
        require(isinstance(self.execution_source, SourceIdentity), "Execution identity required.")
        require(self.provider is SourceProvider.BINANCE_SPOT, "Unsupported valuation provider.")
        _reference(self.dataset_reference, "valuation dataset")
        _reference(self.source_record_id, "valuation source record")
        _reference(self.evidence_reference, "valuation evidence reference")
        require(
            type(self.source_resolution_us) is int and self.source_resolution_us > 0,
            "Invalid source timestamp resolution.",
        )
        require(
            _epoch_microseconds(self.valued_at.value) % self.source_resolution_us == 0,
            "Valuation timestamp is not aligned to source resolution.",
        )

    def matches(self, executed_at: UTCTimestamp) -> bool:
        """Source precision is metadata, not permission for approximate accounting."""
        require(isinstance(executed_at, UTCTimestamp), "Execution timestamp required.")
        return self.valued_at.value == executed_at.value


@dataclass(frozen=True, slots=True)
class TradeFee:
    asset: AssetId
    quantity: AssetQuantity
    category: FeeCategory
    valuation: HistoricalUSDTPrice | HistoricalUSDTValue | None = None

    def __post_init__(self) -> None:
        require(isinstance(self.asset, AssetId), "Fee asset required.")
        require(isinstance(self.quantity, AssetQuantity), "Fee quantity required.")
        require(isinstance(self.category, FeeCategory), "Invalid fee category.")
        require(
            self.valuation is None
            or isinstance(self.valuation, HistoricalUSDTPrice | HistoricalUSDTValue),
            "Invalid fee valuation.",
        )


@dataclass(frozen=True, slots=True)
class OwnerOrderResolution:
    authorized_actor_id: UUID
    reason: str
    compared_event_ids: tuple[OperationId, ...]
    predecessor_id: OperationId | None
    successor_id: OperationId | None

    def __post_init__(self) -> None:
        identity(self.authorized_actor_id)
        _reason(self.reason)
        require(
            isinstance(self.compared_event_ids, tuple)
            and len(self.compared_event_ids) >= 2
            and all(isinstance(event_id, OperationId) for event_id in self.compared_event_ids)
            and len(set(self.compared_event_ids)) == len(self.compared_event_ids),
            "Owner resolution requires distinct compared event records.",
        )
        require(
            (self.predecessor_id is None or isinstance(self.predecessor_id, OperationId))
            and (self.successor_id is None or isinstance(self.successor_id, OperationId))
            and (self.predecessor_id is not None or self.successor_id is not None)
            and self.predecessor_id != self.successor_id,
            "Owner resolution requires a valid predecessor/successor relation.",
        )


@dataclass(frozen=True, slots=True)
class EffectiveOrder:
    path: tuple[int, ...]
    basis: OrderingBasis
    evidence_reference: str
    owner_resolution: OwnerOrderResolution | None = None

    def __post_init__(self) -> None:
        require(
            isinstance(self.path, tuple)
            and bool(self.path)
            and all(type(part) is int for part in self.path),
            "Effective path must be a nonempty tuple of signed integers.",
        )
        require(isinstance(self.basis, OrderingBasis), "Invalid ordering basis.")
        _reference(self.evidence_reference, "ordering evidence reference")
        require(
            (self.basis is OrderingBasis.OWNER_RESOLUTION)
            == isinstance(self.owner_resolution, OwnerOrderResolution),
            "Owner ordering requires structured resolution evidence.",
        )


@dataclass(frozen=True, slots=True)
class CorrectionProvenance:
    original_event_id: OperationId
    target_version_id: OperationId
    new_version_id: OperationId
    target_version: int
    version: int
    reason: str
    authorized_actor_id: UUID
    recorded_at: UTCTimestamp

    def __post_init__(self) -> None:
        require(isinstance(self.original_event_id, OperationId), "Original event ID required.")
        require(isinstance(self.target_version_id, OperationId), "Target version ID required.")
        require(isinstance(self.new_version_id, OperationId), "New version ID required.")
        require(
            self.new_version_id not in (self.original_event_id, self.target_version_id),
            "Correction version cannot refer to itself or reuse the original event ID.",
        )
        require(
            type(self.target_version) is int
            and self.target_version > 0
            and type(self.version) is int
            and self.version == self.target_version + 1,
            "Correction version must follow its target version.",
        )
        require(
            (self.target_version == 1) == (self.target_version_id == self.original_event_id),
            "Correction target does not match its version relationship.",
        )
        _reason(self.reason)
        identity(self.authorized_actor_id)
        require(isinstance(self.recorded_at, UTCTimestamp), "Recording timestamp required.")


@dataclass(frozen=True, slots=True)
class FeeAwareTrade:
    operation_id: OperationId
    execution: GrossTradeExecution
    fee: TradeFee
    provenance: EventProvenance
    effective_order: EffectiveOrder | None

    def __post_init__(self) -> None:
        require(isinstance(self.operation_id, OperationId), "Operation ID required.")
        require(isinstance(self.execution, GrossTradeExecution), "Gross execution required.")
        require(isinstance(self.fee, TradeFee), "Trade fee required.")
        require(isinstance(self.provenance, EventProvenance), "Event provenance required.")
        require(
            self.effective_order is None or isinstance(self.effective_order, EffectiveOrder),
            "Invalid effective order.",
        )
        require(
            (self.effective_order is None) == (self.provenance.append_sequence is None),
            "Effective order and append sequence must both be present or absent.",
        )
        if self.effective_order is not None:
            require(
                self.provenance.source.namespace is SourceNamespace.SPOT_TRADE,
                "SOR allocations cannot become active financial trades.",
            )
            resolution = self.effective_order.owner_resolution
            if resolution is not None:
                require(
                    self.operation_id in resolution.compared_event_ids
                    and self.operation_id
                    not in (
                        resolution.predecessor_id,
                        resolution.successor_id,
                    ),
                    "Owner resolution does not identify the trade and its neighbors.",
                )
        execution = self.execution
        fee = self.fee
        require(
            self.provenance.recorded_at.value >= execution.executed_at.value,
            "Recording cannot precede execution.",
        )
        require(
            self.provenance.source.symbol.value
            == execution.base_asset.value + execution.quote_asset.value,
            "Source symbol disagrees with gross execution.",
        )
        require(
            fee.category is classify_fee(execution.base_asset, execution.quote_asset, fee.asset),
            "Fee classification disagrees with fee asset.",
        )
        if fee.category is FeeCategory.BASE_ASSET and execution.side is TradeSide.BUY:
            require(
                fee.quantity.value < execution.gross_base_quantity.value,
                "Base fee must leave positive acquired quantity.",
            )
        if fee.category is FeeCategory.QUOTE_ASSET and execution.side is TradeSide.SELL:
            require(
                fee.quantity.value <= execution.gross_quote_quantity.value,
                "Quote fee exceeds gross proceeds.",
            )
        if fee.category is FeeCategory.THIRD_ASSET:
            if fee.valuation is not None:
                require(
                    fee.valuation.asset == fee.asset
                    and fee.valuation.matches(execution.executed_at),
                    "Fee valuation does not match the fee asset or execution time.",
                )
                if isinstance(fee.valuation, HistoricalUSDTValue):
                    require(
                        fee.valuation.fee_quantity == fee.quantity
                        and fee.valuation.execution_source == self.provenance.source,
                        "Direct USDT evidence does not match this fee or execution.",
                    )
        else:
            require(fee.valuation is None, "Base or quote fee cannot carry third-asset valuation.")
        if fee.quantity.value == 0:
            require(fee.valuation is None, "Zero fee cannot carry a valuation.")
