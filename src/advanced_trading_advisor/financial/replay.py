"""Pure, deterministic weighted-average replay over immutable fee-free operations."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from advanced_trading_advisor.domain.validation import require

from .ledger import (
    BuyFacts,
    Completeness,
    LedgerOperation,
    OpeningFacts,
    OperationType,
    SellFacts,
)
from .values import QUANTUM, USDT, AssetId, OperationId, exact_decimal

ZERO = Decimal(0)
# Two 60-digit inputs multiply exactly; only division is rounded, explicitly below.
ARITHMETIC_CONTEXT = Context(prec=120, rounding=ROUND_HALF_EVEN)


@dataclass(frozen=True, slots=True)
class AssetAccounting:
    asset: AssetId
    quantity: Decimal
    known_cost_usdt: Decimal
    incomplete_quantity: Decimal
    completeness: Completeness
    average_cost_usdt: Decimal | None


@dataclass(frozen=True, slots=True)
class SaleAccounting:
    operation_id: OperationId
    asset: AssetId
    quantity: Decimal
    proceeds_usdt: Decimal
    known_cost_removed_usdt: Decimal
    incomplete_quantity_removed: Decimal
    completeness: Completeness
    realized_pnl_usdt: Decimal | None


@dataclass(frozen=True, slots=True)
class AccountingState:
    assets: tuple[AssetAccounting, ...]
    sales: tuple[SaleAccounting, ...]
    ordered_operation_ids: tuple[OperationId, ...]
    realized_pnl_completeness: Completeness
    complete_sales_realized_pnl_usdt: Decimal
    total_realized_pnl_usdt: Decimal | None

    def asset(self, asset: AssetId) -> AssetAccounting | None:
        return next((position for position in self.assets if position.asset == asset), None)


def _checked(value: Decimal) -> Decimal:
    return exact_decimal(value, nonnegative=True)


def _add(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(ARITHMETIC_CONTEXT):
        return _checked(left + right)


def _subtract(left: Decimal, right: Decimal) -> Decimal:
    with localcontext(ARITHMETIC_CONTEXT):
        return _checked(left - right)


def _allocation(component: Decimal, sold: Decimal, held: Decimal, *, incomplete: bool) -> Decimal:
    if sold == held:
        return component  # Final disposal takes the exact residual, including rounding dust.
    with localcontext(ARITHMETIC_CONTEXT):
        # Remove only trailing zeros introduced by rounding this derived value.
        allocated = (component * sold / held).quantize(QUANTUM).normalize()
    _checked(allocated)
    # Rounding must not make an unknown-cost component disappear from either side.
    if incomplete and component > 0:
        require(0 < allocated < component, "Incomplete quantity exceeds allocation precision.")
    return allocated


def replay(operations: tuple[LedgerOperation, ...]) -> AccountingState:
    """Replay by execution time and immutable sequence; never alter caller-owned facts.

    Inputs permit 24 decimal places and 60 digits. Proportional allocations use
    half-even rounding to 24 places; the final disposal takes all residuals.
    An unrepresentable split of incomplete quantity fails rather than claiming
    its unknown cost has vanished. This is a domain limit, not a DB schema choice.
    """
    require(
        isinstance(operations, tuple) and all(isinstance(op, LedgerOperation) for op in operations),
        "Replay requires an immutable tuple of validated operations.",
    )
    require(len({op.id for op in operations}) == len(operations), "Duplicate operation ID.")
    require(
        len({op.sequence for op in operations}) == len(operations), "Duplicate ledger sequence."
    )
    ordered = tuple(sorted(operations, key=lambda op: (op.executed_at.value, op.sequence.value)))
    positions: dict[AssetId, tuple[Decimal, Decimal, Decimal]] = {}
    sales: list[SaleAccounting] = []
    complete_pnl = ZERO
    for operation in ordered:
        # A candidate operation is applied to a copy. A failure never exposes partial state.
        candidate = positions.copy()
        facts = operation.facts
        if operation.type is OperationType.OPENING_BALANCE:
            assert isinstance(facts, OpeningFacts)
            quantity, cost, incomplete = candidate.get(facts.asset, (ZERO, ZERO, ZERO))
            candidate[facts.asset] = (
                _add(quantity, facts.quantity.value),
                _add(cost, facts.known_cost_usdt.value if facts.known_cost_usdt else ZERO),
                _add(incomplete, facts.incomplete_quantity.value),
            )
        elif operation.type is OperationType.BUY:
            assert isinstance(facts, BuyFacts)
            cash, cash_cost, cash_incomplete = candidate.get(USDT, (ZERO, ZERO, ZERO))
            require(cash >= facts.principal_usdt.value, "Insufficient USDT balance.")
            require(cash_incomplete == 0 and cash_cost == cash, "USDT accounting is inconsistent.")
            candidate[USDT] = (
                _subtract(cash, facts.principal_usdt.value),
                _subtract(cash_cost, facts.principal_usdt.value),
                ZERO,
            )
            quantity, cost, incomplete = candidate.get(facts.asset, (ZERO, ZERO, ZERO))
            candidate[facts.asset] = (
                _add(quantity, facts.quantity.value),
                _add(cost, facts.principal_usdt.value),
                incomplete,
            )
        else:
            assert isinstance(facts, SellFacts)
            held, cost, incomplete = candidate.get(facts.asset, (ZERO, ZERO, ZERO))
            require(held >= facts.quantity.value, "Insufficient asset balance.")
            removed_cost = _allocation(cost, facts.quantity.value, held, incomplete=False)
            removed_incomplete = _allocation(
                incomplete, facts.quantity.value, held, incomplete=True
            )
            remaining_quantity = _subtract(held, facts.quantity.value)
            remaining_cost = _subtract(cost, removed_cost)
            remaining_incomplete = _subtract(incomplete, removed_incomplete)
            if held > incomplete and facts.quantity.value < held:
                require(
                    removed_incomplete < facts.quantity.value
                    and remaining_incomplete < remaining_quantity,
                    "Known quantity exceeds allocation precision.",
                )
            candidate[facts.asset] = (
                remaining_quantity,
                remaining_cost,
                remaining_incomplete,
            )
            cash, cash_cost, cash_incomplete = candidate.get(USDT, (ZERO, ZERO, ZERO))
            require(cash_incomplete == 0 and cash_cost == cash, "USDT accounting is inconsistent.")
            candidate[USDT] = (
                _add(cash, facts.proceeds_usdt.value),
                _add(cash_cost, facts.proceeds_usdt.value),
                ZERO,
            )
            status = (
                Completeness.COMPLETE
                if removed_incomplete == 0
                else Completeness.PARTIAL
                if removed_incomplete < facts.quantity.value or removed_cost > 0
                else Completeness.UNKNOWN
            )
            if status is Completeness.COMPLETE:
                with localcontext(ARITHMETIC_CONTEXT):
                    pnl = exact_decimal(facts.proceeds_usdt.value - removed_cost)
            else:
                pnl = None
            if pnl is not None:
                with localcontext(ARITHMETIC_CONTEXT):
                    complete_pnl += pnl
                exact_decimal(complete_pnl)
            sales.append(
                SaleAccounting(
                    operation.id,
                    facts.asset,
                    facts.quantity.value,
                    facts.proceeds_usdt.value,
                    removed_cost,
                    removed_incomplete,
                    status,
                    pnl,
                )
            )
        for quantity, cost, incomplete in candidate.values():
            _checked(quantity)
            _checked(cost)
            _checked(incomplete)
            require(incomplete <= quantity, "Invalid historical accounting state.")
            require(cost == 0 or incomplete < quantity, "Known cost requires known quantity.")
        positions = candidate

    assets = []
    for asset, (quantity, cost, incomplete) in sorted(
        positions.items(), key=lambda item: item[0].value
    ):
        status = (
            Completeness.COMPLETE
            if incomplete == 0
            else Completeness.PARTIAL
            if incomplete < quantity or cost > 0
            else Completeness.UNKNOWN
        )
        with localcontext(ARITHMETIC_CONTEXT):
            average = (
                (cost / quantity).quantize(QUANTUM).normalize()
                if quantity > 0 and status is Completeness.COMPLETE
                else None
            )
        if average is not None:
            _checked(average)
            require(average > 0 or cost == 0, "Average cost exceeds display precision.")
        assets.append(AssetAccounting(asset, quantity, cost, incomplete, status, average))
    has_incomplete = any(sale.completeness is not Completeness.COMPLETE for sale in sales)
    realized_status = (
        Completeness.COMPLETE
        if not has_incomplete
        else Completeness.PARTIAL
        if any(sale.completeness is not Completeness.UNKNOWN for sale in sales)
        else Completeness.UNKNOWN
    )
    return AccountingState(
        tuple(assets),
        tuple(sales),
        tuple(op.id for op in ordered),
        realized_status,
        complete_pnl,
        complete_pnl if not has_incomplete else None,
    )
