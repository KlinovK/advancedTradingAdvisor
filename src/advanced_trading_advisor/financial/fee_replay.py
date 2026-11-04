"""Pure fee-aware replay. The caller must attest external valuation and order evidence."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum

from advanced_trading_advisor.domain.validation import require

from .fee_aware import (
    FeeAwareTrade,
    FeeCategory,
    HistoricalUSDTPrice,
    HistoricalUSDTValue,
    SourceIdentity,
    SourceNamespace,
    TradeSide,
)
from .ledger import BuyFacts, LedgerOperation, OpeningFacts, OperationType, SellFacts
from .replay import ARITHMETIC_CONTEXT, _allocation
from .values import USDT, AssetId, OperationId, UTCTimestamp, exact_decimal

ZERO = Decimal(0)


class CalculationStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class VerifiedValuation:
    """Trusted caller assertion; construction alone does not verify a Binance archive."""

    operation_id: OperationId
    evidence: HistoricalUSDTPrice | HistoricalUSDTValue

    def __post_init__(self) -> None:
        require(isinstance(self.operation_id, OperationId), "Invalid valuation operation ID.")
        require(
            isinstance(self.evidence, HistoricalUSDTPrice | HistoricalUSDTValue),
            "Invalid verified valuation evidence.",
        )


@dataclass(frozen=True, slots=True)
class VerifiedOrder:
    """Trusted caller assertion of an independently resolved equal-time order."""

    executed_at: UTCTimestamp
    operation_ids: tuple[OperationId, ...]

    def __post_init__(self) -> None:
        require(isinstance(self.executed_at, UTCTimestamp), "Invalid order timestamp.")
        require(
            isinstance(self.operation_ids, tuple)
            and len(self.operation_ids) >= 2
            and all(isinstance(value, OperationId) for value in self.operation_ids)
            and len(set(self.operation_ids)) == len(self.operation_ids),
            "Invalid resolved operation order.",
        )


@dataclass(frozen=True, slots=True)
class FeeAssetState:
    asset: AssetId
    quantity: Decimal
    known_cost_usdt: Decimal
    unknown_basis_quantity: Decimal
    unknown_additive_cost: bool
    calculation_status: CalculationStatus


@dataclass(frozen=True, slots=True)
class FeeEventResult:
    operation_id: OperationId
    trade_pnl_usdt: Decimal | None
    fee_disposal_pnl_usdt: Decimal | None
    combined_pnl_usdt: Decimal | None
    trade_known_cost_removed_usdt: Decimal
    fee_known_cost_removed_usdt: Decimal
    calculation_status: CalculationStatus


@dataclass(frozen=True, slots=True)
class FeeReplayResult:
    assets: tuple[FeeAssetState, ...]
    events: tuple[FeeEventResult, ...]
    ordered_operation_ids: tuple[OperationId, ...]
    calculation_status: CalculationStatus
    complete_component_subtotal_usdt: Decimal
    total_realized_pnl_usdt: Decimal | None

    def asset(self, asset: AssetId) -> FeeAssetState | None:
        return next((item for item in self.assets if item.asset == asset), None)


@dataclass(frozen=True, slots=True)
class _Position:
    quantity: Decimal = ZERO
    cost: Decimal = ZERO
    unknown_quantity: Decimal = ZERO
    unknown_additive: bool = False


def _exact(value: Decimal, *, nonnegative: bool = False) -> Decimal:
    with localcontext(ARITHMETIC_CONTEXT):
        return exact_decimal(value, nonnegative=nonnegative)


def _add(*values: Decimal, nonnegative: bool = False) -> Decimal:
    with localcontext(ARITHMETIC_CONTEXT):
        return exact_decimal(sum(values, ZERO), nonnegative=nonnegative)


def _take(position: _Position, quantity: Decimal) -> tuple[_Position, Decimal, Decimal, bool]:
    require(position.quantity >= quantity, "Insufficient asset balance.")
    removed_cost = _allocation(position.cost, quantity, position.quantity, incomplete=False)
    removed_unknown = _allocation(
        position.unknown_quantity, quantity, position.quantity, incomplete=True
    )
    remaining = _Position(
        _add(position.quantity, -quantity, nonnegative=True),
        _add(position.cost, -removed_cost, nonnegative=True),
        _add(position.unknown_quantity, -removed_unknown, nonnegative=True),
        position.unknown_additive and quantity != position.quantity,
    )
    require(
        removed_unknown <= quantity and remaining.unknown_quantity <= remaining.quantity,
        "Unknown basis allocation exceeds quantity.",
    )
    if position.quantity > position.unknown_quantity and quantity < position.quantity:
        require(
            removed_unknown < quantity and remaining.unknown_quantity < remaining.quantity,
            "Known quantity exceeds allocation precision.",
        )
    return remaining, removed_cost, removed_unknown, position.unknown_additive


def _fee_value(
    trade: FeeAwareTrade, verified: dict[OperationId, VerifiedValuation]
) -> Decimal | None:
    evidence = trade.fee.valuation
    attestation = verified.get(trade.operation_id)
    if evidence is None or attestation is None:
        return None
    require(attestation.evidence == evidence, "Verified valuation does not match trade evidence.")
    if isinstance(evidence, HistoricalUSDTValue):
        return evidence.value_usdt.value
    with localcontext(ARITHMETIC_CONTEXT):
        return exact_decimal(trade.fee.quantity.value * evidence.price_usdt, nonnegative=True)


def _status(
    quantity: Decimal, unknown_quantity: Decimal, unknown_additive: bool
) -> CalculationStatus:
    if unknown_quantity == 0 and not unknown_additive:
        return CalculationStatus.COMPLETE
    return (
        CalculationStatus.PARTIAL if unknown_quantity < quantity else CalculationStatus.UNAVAILABLE
    )


def replay_fee_aware(
    operations: tuple[LedgerOperation | FeeAwareTrade, ...],
    *,
    verified_valuations: tuple[VerifiedValuation, ...] = (),
    verified_orders: tuple[VerifiedOrder, ...] = (),
) -> FeeReplayResult:
    """Replay committed events; external attestations must come from a trusted caller.

    Source-history coverage is deliberately absent from this result. A complete
    calculation is not a claim that the exchange history is complete.
    """
    require(
        isinstance(operations, tuple)
        and all(isinstance(op, LedgerOperation | FeeAwareTrade) for op in operations),
        "Replay requires immutable validated operations.",
    )
    require(
        isinstance(verified_valuations, tuple)
        and all(isinstance(item, VerifiedValuation) for item in verified_valuations),
        "Invalid verified valuation set.",
    )
    require(
        isinstance(verified_orders, tuple)
        and all(isinstance(item, VerifiedOrder) for item in verified_orders),
        "Invalid verified order set.",
    )
    ids = [op.id if isinstance(op, LedgerOperation) else op.operation_id for op in operations]
    require(len(set(ids)) == len(ids), "Duplicate operation ID.")
    sequences = [
        op.sequence.value
        if isinstance(op, LedgerOperation)
        else op.provenance.append_sequence.value
        for op in operations
        if isinstance(op, LedgerOperation) or op.provenance.append_sequence is not None
    ]
    require(len(sequences) == len(operations), "Candidate event cannot enter replay.")
    require(len(set(sequences)) == len(sequences), "Duplicate append sequence.")
    sources: list[SourceIdentity] = []
    for op in operations:
        if isinstance(op, FeeAwareTrade):
            require(op.effective_order is not None, "Candidate event cannot enter replay.")
            require(
                op.provenance.source.namespace is SourceNamespace.SPOT_TRADE,
                "Unsupported source namespace.",
            )
            sources.append(op.provenance.source)
    require(
        len({source.portfolio_id for source in sources}) <= 1,
        "Fee-aware replay cannot mix portfolio identities.",
    )
    require(len(set(sources)) == len(sources), "Duplicate active source identity.")
    valuations = {item.operation_id: item for item in verified_valuations}
    require(len(valuations) == len(verified_valuations), "Duplicate valuation attestation.")
    require(set(valuations).issubset(ids), "Valuation attestation has no active event.")
    for op in operations:
        if (op.id if isinstance(op, LedgerOperation) else op.operation_id) in valuations:
            require(
                isinstance(op, FeeAwareTrade)
                and op.fee.category is FeeCategory.THIRD_ASSET
                and op.fee.quantity.value > 0,
                "Valuation attestation is not for a third-asset fee.",
            )
    order_by_time = {item.executed_at.value: item for item in verified_orders}
    require(len(order_by_time) == len(verified_orders), "Duplicate order attestation.")

    def stamp(op: LedgerOperation | FeeAwareTrade) -> UTCTimestamp:
        return op.executed_at if isinstance(op, LedgerOperation) else op.execution.executed_at

    groups: dict[object, list[LedgerOperation | FeeAwareTrade]] = {}
    for op in operations:
        groups.setdefault(stamp(op).value, []).append(op)
    ordered: list[LedgerOperation | FeeAwareTrade] = []
    for timestamp, group in sorted(groups.items()):
        attestation = order_by_time.get(timestamp)
        if len(group) > 1 and any(isinstance(op, FeeAwareTrade) for op in group):
            require(attestation is not None, "Unresolved equal-time financial order.")
            lookup = {
                op.id if isinstance(op, LedgerOperation) else op.operation_id: op for op in group
            }
            require(
                set(attestation.operation_ids) == set(lookup),
                "Resolved order does not match equal-time events.",
            )
            paths = {
                identity: (
                    (op.sequence.value,)
                    if isinstance(op, LedgerOperation)
                    else op.effective_order.path
                )
                for identity, op in lookup.items()
            }
            require(len(set(paths.values())) == len(paths), "Duplicate active effective path.")
            require(
                attestation.operation_ids
                == tuple(sorted(paths, key=lambda identity: paths[identity])),
                "Resolved order disagrees with effective paths.",
            )
            ordered.extend(lookup[identity] for identity in attestation.operation_ids)
        else:
            require(attestation is None, "Extraneous order attestation.")
            ordered.extend(
                sorted(group, key=lambda op: op.sequence.value) if len(group) > 1 else group
            )
    require(
        all(key in groups for key in order_by_time),
        "Order attestation has no matching event time.",
    )
    positions: dict[AssetId, _Position] = {}
    events: list[FeeEventResult] = []
    for op in ordered:
        candidate = positions.copy()
        if isinstance(op, LedgerOperation):
            facts = op.facts
            if op.type is OperationType.OPENING_BALANCE:
                assert isinstance(facts, OpeningFacts)
                old = candidate.get(facts.asset, _Position())
                candidate[facts.asset] = _Position(
                    _add(old.quantity, facts.quantity.value, nonnegative=True),
                    _add(
                        old.cost,
                        facts.known_cost_usdt.value if facts.known_cost_usdt else ZERO,
                        nonnegative=True,
                    ),
                    _add(old.unknown_quantity, facts.incomplete_quantity.value, nonnegative=True),
                    old.unknown_additive,
                )
            elif op.type is OperationType.BUY:
                assert isinstance(facts, BuyFacts)
                _spend_cash(candidate, facts.principal_usdt.value)
                _acquire(candidate, facts.asset, facts.quantity.value, facts.principal_usdt.value)
            else:
                assert isinstance(facts, SellFacts)
                cost, unknown, additive = _dispose(candidate, facts.asset, facts.quantity.value)
                _receive_cash(candidate, facts.proceeds_usdt.value)
                pnl = (
                    _add(facts.proceeds_usdt.value, -cost)
                    if unknown == 0 and not additive
                    else None
                )
                events.append(
                    FeeEventResult(
                        op.id,
                        pnl,
                        None,
                        pnl,
                        cost,
                        ZERO,
                        CalculationStatus.COMPLETE
                        if pnl is not None
                        else _status(facts.quantity.value, unknown, additive),
                    )
                )
        else:
            _apply_trade(candidate, op, valuations, events)
        for asset, position in candidate.items():
            for value in (position.quantity, position.cost, position.unknown_quantity):
                _exact(value, nonnegative=True)
            require(
                position.unknown_quantity <= position.quantity
                and (position.cost == 0 or position.unknown_quantity < position.quantity),
                "Invalid historical accounting state.",
            )
            if asset == USDT:
                require(
                    position.cost == position.quantity
                    and position.unknown_quantity == 0
                    and not position.unknown_additive,
                    "USDT accounting is inconsistent.",
                )
        positions = candidate
    assets = tuple(
        FeeAssetState(
            asset,
            pos.quantity,
            pos.cost,
            pos.unknown_quantity,
            pos.unknown_additive,
            _status(pos.quantity, pos.unknown_quantity, pos.unknown_additive),
        )
        for asset, pos in sorted(positions.items(), key=lambda item: item[0].value)
    )
    incomplete = [
        event for event in events if event.calculation_status is not CalculationStatus.COMPLETE
    ]
    subtotal = _add(
        *(event.combined_pnl_usdt for event in events if event.combined_pnl_usdt is not None)
    )
    status = (
        CalculationStatus.COMPLETE
        if not incomplete
        else CalculationStatus.PARTIAL
        if len(incomplete) < len(events)
        else CalculationStatus.UNAVAILABLE
    )
    return FeeReplayResult(
        assets,
        tuple(events),
        tuple(op.id if isinstance(op, LedgerOperation) else op.operation_id for op in ordered),
        status,
        subtotal,
        subtotal if not incomplete else None,
    )


def _spend_cash(positions: dict[AssetId, _Position], amount: Decimal) -> None:
    cash = positions.get(USDT, _Position())
    require(cash.quantity >= amount, "Insufficient USDT balance.")
    require(cash.cost == cash.quantity and cash.unknown_quantity == 0, "Invalid USDT basis.")
    positions[USDT] = _Position(
        _add(cash.quantity, -amount, nonnegative=True), _add(cash.cost, -amount, nonnegative=True)
    )


def _receive_cash(positions: dict[AssetId, _Position], amount: Decimal) -> None:
    cash = positions.get(USDT, _Position())
    require(cash.cost == cash.quantity and cash.unknown_quantity == 0, "Invalid USDT basis.")
    positions[USDT] = _Position(
        _add(cash.quantity, amount, nonnegative=True), _add(cash.cost, amount, nonnegative=True)
    )


def _acquire(
    positions: dict[AssetId, _Position],
    asset: AssetId,
    quantity: Decimal,
    cost: Decimal,
    *,
    unknown_additive: bool = False,
) -> None:
    old = positions.get(asset, _Position())
    positions[asset] = _Position(
        _add(old.quantity, quantity, nonnegative=True),
        _add(old.cost, cost, nonnegative=True),
        old.unknown_quantity,
        old.unknown_additive or unknown_additive,
    )


def _dispose(
    positions: dict[AssetId, _Position], asset: AssetId, quantity: Decimal
) -> tuple[Decimal, Decimal, bool]:
    old = positions.get(asset, _Position())
    remaining, cost, unknown, additive = _take(old, quantity)
    positions[asset] = remaining
    return cost, unknown, additive


def _apply_trade(
    positions: dict[AssetId, _Position],
    trade: FeeAwareTrade,
    verified: dict[OperationId, VerifiedValuation],
    events: list[FeeEventResult],
) -> None:
    execution, fee = trade.execution, trade.fee
    q, principal, f = (
        execution.gross_base_quantity.value,
        execution.gross_quote_quantity.value,
        fee.quantity.value,
    )
    value = _fee_value(trade, verified) if fee.category is FeeCategory.THIRD_ASSET and f else None
    fee_cost = ZERO
    fee_unknown = ZERO
    fee_additive = False
    if execution.side is TradeSide.BUY:
        cash_out = _add(
            principal, f if fee.category is FeeCategory.QUOTE_ASSET else ZERO, nonnegative=True
        )
        _spend_cash(positions, cash_out)
        acquired = _add(q, -f if fee.category is FeeCategory.BASE_ASSET else ZERO, nonnegative=True)
        basis = _add(
            principal,
            f if fee.category is FeeCategory.QUOTE_ASSET else ZERO,
            value or ZERO,
            nonnegative=True,
        )
        _acquire(
            positions,
            execution.base_asset,
            acquired,
            basis,
            unknown_additive=fee.category is FeeCategory.THIRD_ASSET and f > 0 and value is None,
        )
        if fee.category is FeeCategory.THIRD_ASSET and f:
            fee_cost, fee_unknown, fee_additive = _dispose(positions, fee.asset, f)
        fee_pnl = (
            (
                _add(value, -fee_cost)
                if value is not None and fee_unknown == 0 and not fee_additive
                else None
            )
            if fee.category is FeeCategory.THIRD_ASSET and f
            else None
        )
        combined = fee_pnl if fee.category is FeeCategory.THIRD_ASSET and f else None
        status = (
            CalculationStatus.COMPLETE
            if fee.category is not FeeCategory.THIRD_ASSET or f == 0
            else CalculationStatus.COMPLETE
            if value is not None and fee_pnl is not None
            else CalculationStatus.PARTIAL
        )
        events.append(
            FeeEventResult(trade.operation_id, None, fee_pnl, combined, ZERO, fee_cost, status)
        )
        return
    base_out = _add(q, f if fee.category is FeeCategory.BASE_ASSET else ZERO, nonnegative=True)
    cost, unknown, additive = _dispose(positions, execution.base_asset, base_out)
    proceeds = _add(
        principal, -f if fee.category is FeeCategory.QUOTE_ASSET else ZERO, nonnegative=True
    )
    _receive_cash(positions, proceeds)
    if fee.category is FeeCategory.THIRD_ASSET and f:
        fee_cost, fee_unknown, fee_additive = _dispose(positions, fee.asset, f)
    complete_base = unknown == 0 and not additive
    complete_fee = fee_unknown == 0 and not fee_additive
    trade_pnl = (
        _add(proceeds, -(value or ZERO), -cost)
        if complete_base
        and (fee.category is not FeeCategory.THIRD_ASSET or f == 0 or value is not None)
        else None
    )
    fee_pnl = (
        (_add(value, -fee_cost) if value is not None and complete_fee else None)
        if fee.category is FeeCategory.THIRD_ASSET and f
        else None
    )
    combined = _add(proceeds, -cost, -fee_cost) if complete_base and complete_fee else None
    status = (
        CalculationStatus.COMPLETE
        if combined is not None
        else CalculationStatus.PARTIAL
        if (
            trade_pnl is not None
            or fee_pnl is not None
            or _status(base_out, unknown, additive) is CalculationStatus.PARTIAL
            or (
                fee.category is FeeCategory.THIRD_ASSET
                and f > 0
                and _status(f, fee_unknown, fee_additive) is CalculationStatus.PARTIAL
            )
        )
        else CalculationStatus.UNAVAILABLE
    )
    events.append(
        FeeEventResult(trade.operation_id, trade_pnl, fee_pnl, combined, cost, fee_cost, status)
    )
