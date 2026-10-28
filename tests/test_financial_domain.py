"""Offline contracts for the fee-free, single-portfolio financial foundation."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.financial.ledger import (
    BalancePosting,
    BuyFacts,
    Completeness,
    LedgerOperation,
    OpeningFacts,
    OperationType,
    PostingRole,
    SellFacts,
)
from advanced_trading_advisor.financial.replay import replay
from advanced_trading_advisor.financial.values import (
    USDT,
    AssetId,
    AssetQuantity,
    LedgerSequence,
    OperationId,
    USDTAmount,
    UTCTimestamp,
)


def d(value: str) -> Decimal:
    return Decimal(value)


T0 = datetime(2026, 1, 1, tzinfo=UTC)


def metadata(number: int, *, day: int = 1, recorded_day: int | None = None):
    return (
        OperationId(UUID(int=number)),
        LedgerSequence(number),
        UTCTimestamp(T0 + timedelta(days=day - 1)),
        UTCTimestamp(T0 + timedelta(days=(recorded_day or day) - 1)),
    )


def opening(
    number: int,
    asset: str,
    quantity: str,
    cost: str | None,
    *,
    incomplete: str = "0",
    status: Completeness | None = None,
    day: int = 1,
) -> LedgerOperation:
    symbol = AssetId(asset)
    facts = OpeningFacts(
        symbol,
        AssetQuantity(d(quantity)),
        USDTAmount(d(cost)) if cost is not None else None,
        AssetQuantity(d(incomplete)),
        status or (Completeness.UNKNOWN if cost is None else Completeness.COMPLETE),
    )
    return LedgerOperation(
        *metadata(number, day=day),
        OperationType.OPENING_BALANCE,
        facts,
        (BalancePosting(1, symbol, d(quantity), PostingRole.OPENING),),
    )


def buy(
    number: int,
    asset: str,
    quantity: str,
    cost: str,
    *,
    day: int = 1,
    recorded_day: int | None = None,
) -> LedgerOperation:
    symbol = AssetId(asset)
    facts = BuyFacts(symbol, AssetQuantity(d(quantity)), USDTAmount(d(cost)))
    return LedgerOperation(
        *metadata(number, day=day, recorded_day=recorded_day),
        OperationType.BUY,
        facts,
        (
            BalancePosting(1, symbol, d(quantity), PostingRole.TRADED_ASSET),
            BalancePosting(2, USDT, -d(cost), PostingRole.SETTLEMENT),
        ),
    )


def sell(number: int, asset: str, quantity: str, proceeds: str, *, day: int = 1) -> LedgerOperation:
    symbol = AssetId(asset)
    facts = SellFacts(symbol, AssetQuantity(d(quantity)), USDTAmount(d(proceeds)))
    return LedgerOperation(
        *metadata(number, day=day),
        OperationType.SELL,
        facts,
        (
            BalancePosting(1, symbol, -d(quantity), PostingRole.TRADED_ASSET),
            BalancePosting(2, USDT, d(proceeds), PostingRole.SETTLEMENT),
        ),
    )


def position(state, asset: str):
    return state.asset(AssetId(asset))


def test_opening_and_simple_purchase() -> None:
    initial = replay((opening(1, "USDT", "2000", "2000"),))
    assert position(initial, "USDT").quantity == d("2000")
    assert initial.sales == ()
    assert initial.total_realized_pnl_usdt == d("0")
    result = replay((opening(1, "USDT", "2000", "2000"), buy(2, "BTC", "1", "1000")))
    assert position(result, "USDT").quantity == d("1000")
    assert position(result, "BTC").quantity == d("1")
    assert position(result, "BTC").known_cost_usdt == d("1000")
    assert position(result, "BTC").average_cost_usdt == d("1000")


def test_multiple_purchases_and_partial_sale() -> None:
    ledger = (
        opening(1, "USDT", "5000", "5000"),
        buy(2, "BTC", "1", "1000"),
        buy(3, "BTC", "1", "2000"),
    )
    before = replay(ledger)
    assert position(before, "BTC").quantity == d("2")
    assert position(before, "BTC").known_cost_usdt == d("3000")
    assert position(before, "BTC").average_cost_usdt == d("1500")
    after = replay((*ledger, sell(4, "BTC", "0.5", "1000")))
    assert position(after, "BTC").quantity == d("1.5")
    assert position(after, "BTC").known_cost_usdt == d("2250")
    assert after.sales[0].known_cost_removed_usdt == d("750")
    assert after.total_realized_pnl_usdt == d("250")


def test_unknown_opening_cost_keeps_realized_pnl_unavailable() -> None:
    result = replay((opening(1, "ETH", "1", None, incomplete="1"), sell(2, "ETH", "0.5", "1000")))
    assert position(result, "ETH").quantity == d("0.5")
    assert position(result, "ETH").incomplete_quantity == d("0.5")
    assert position(result, "ETH").known_cost_usdt == d("0")
    assert position(result, "ETH").average_cost_usdt is None
    assert result.sales[0].completeness is Completeness.UNKNOWN
    assert result.sales[0].realized_pnl_usdt is None
    assert result.total_realized_pnl_usdt is None


def test_mixed_known_unknown_cost_is_proportional_and_incomplete() -> None:
    result = replay(
        (
            opening(1, "ETH", "1", "1000"),
            opening(2, "ETH", "1", None, incomplete="1"),
            sell(3, "ETH", "0.5", "750"),
        )
    )
    eth = position(result, "ETH")
    assert (eth.quantity, eth.known_cost_usdt, eth.incomplete_quantity) == (
        d("1.5"),
        d("750"),
        d("0.75"),
    )
    assert eth.completeness is Completeness.PARTIAL
    assert eth.average_cost_usdt is None
    assert result.sales[0].known_cost_removed_usdt == d("250")
    assert result.sales[0].incomplete_quantity_removed == d("0.25")
    assert result.sales[0].proceeds_usdt == d("750")
    assert result.sales[0].realized_pnl_usdt is None
    assert result.realized_pnl_completeness is Completeness.PARTIAL


def test_partially_known_single_opening_keeps_total_pnl_unavailable() -> None:
    result = replay(
        (
            opening(1, "ETH", "2", "500", incomplete="1", status=Completeness.PARTIAL),
            sell(2, "ETH", "0.5", "750"),
        )
    )
    eth = position(result, "ETH")
    assert (eth.quantity, eth.known_cost_usdt, eth.incomplete_quantity) == (
        d("1.5"),
        d("375"),
        d("0.75"),
    )
    assert eth.average_cost_usdt is None
    assert result.sales[0].known_cost_removed_usdt == d("125")
    assert result.sales[0].realized_pnl_usdt is None


def test_zero_cost_known_quantity_stays_partial_when_mixed_with_unknown() -> None:
    result = replay(
        (
            opening(1, "ETH", "2", "0", incomplete="1", status=Completeness.PARTIAL),
            sell(2, "ETH", "0.5", "750"),
        )
    )
    assert position(result, "ETH").completeness is Completeness.PARTIAL
    assert result.sales[0].completeness is Completeness.PARTIAL
    assert result.realized_pnl_completeness is Completeness.PARTIAL
    assert result.total_realized_pnl_usdt is None
    with pytest.raises(DomainValidationError, match="known quantity"):
        opening(1, "ETH", "1", "0", incomplete="1", status=Completeness.PARTIAL)


@pytest.mark.parametrize("cost", ["0", "100"])
def test_partial_opening_requires_known_quantity_even_with_positive_cost(cost: str) -> None:
    with pytest.raises(DomainValidationError, match="known quantity"):
        opening(1, "ETH", "1", cost, incomplete="1", status=Completeness.PARTIAL)


@pytest.mark.parametrize(
    "ledger",
    [
        (opening(1, "BTC", "0.4", "400"), sell(2, "BTC", "0.5", "750")),
        (opening(1, "USDT", "500", "500"), buy(2, "BTC", "1", "1000")),
    ],
)
def test_insufficient_balance_rejects_without_changing_inputs(ledger) -> None:
    original = ledger
    with pytest.raises(DomainValidationError, match="Insufficient"):
        replay(ledger)
    assert ledger == original
    assert replay((ledger[0],)).assets == replay((ledger[0],)).assets


def test_backdated_purchase_replays_before_later_sale() -> None:
    ledger = (
        opening(1, "USDT", "5000", "5000"),
        buy(2, "BTC", "1", "1000", day=1),
        sell(3, "BTC", "0.5", "750", day=3),
        buy(4, "BTC", "1", "2000", day=2, recorded_day=4),
    )
    result = replay(ledger)
    assert result.ordered_operation_ids == tuple(ledger[i].id for i in (0, 1, 3, 2))
    assert position(result, "BTC").quantity == d("1.5")
    assert position(result, "BTC").known_cost_usdt == d("2250")
    assert position(result, "USDT").quantity == d("2750")
    assert result.sales[0].known_cost_removed_usdt == d("750")
    assert result.total_realized_pnl_usdt == d("0")


def test_historical_negative_prefix_rejected_even_if_final_cash_is_positive() -> None:
    ledger = (
        opening(1, "USDT", "1000", "1000"),
        buy(2, "BTC", "1", "1500", day=2),
        opening(3, "USDT", "1000", "1000", day=3),
    )
    with pytest.raises(DomainValidationError, match="Insufficient USDT"):
        replay(ledger)


def test_determinism_and_input_immutability() -> None:
    ledger = (
        opening(1, "USDT", "1.000000000000000000000001", "1.000000000000000000000001"),
        buy(2, "BTC", "0.00000001", "0.123456789012345678901234"),
    )
    original = tuple(ledger)
    with localcontext() as context:
        context.prec = 2
        first = replay(ledger)
    assert first == replay(ledger)
    assert position(first, "USDT").quantity == d("0.876543210987654321098767")
    assert ledger == original
    assert ledger[1].postings[0].delta == d("0.00000001")
    assert ledger[1].executed_at.value == T0
    with pytest.raises(FrozenInstanceError):
        ledger[1].facts = None


@pytest.mark.parametrize("duplicate", ["id", "sequence"])
def test_duplicate_identity_or_sequence_rejected(duplicate: str) -> None:
    first = opening(1, "USDT", "2000", "2000")
    second = buy(2, "BTC", "1", "1000")
    second = replace(second, **{duplicate: getattr(first, duplicate)})
    with pytest.raises(DomainValidationError, match="Duplicate"):
        replay((first, second))


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "1e-25", "1e60"])
def test_decimal_inputs_outside_policy_rejected(value: str) -> None:
    with pytest.raises(DomainValidationError):
        AssetQuantity(d(value))


def test_value_objects_reject_invalid_signs_identity_sequence_and_times() -> None:
    with pytest.raises(DomainValidationError):
        AssetQuantity(d("-1"))
    with pytest.raises(DomainValidationError):
        USDTAmount(d("-1"))
    with pytest.raises(DomainValidationError):
        AssetId("btc")
    with pytest.raises(DomainValidationError):
        OperationId(UUID(int=0))
    for value in (0, -1, True):
        with pytest.raises(DomainValidationError):
            LedgerSequence(value)
    for value in (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=1)))):
        with pytest.raises(DomainValidationError):
            UTCTimestamp(value)
    with pytest.raises(DomainValidationError):
        opening(1, "BTC", "0", "0")
    with pytest.raises(DomainValidationError, match="Recording cannot precede"):
        replace(buy(2, "BTC", "1", "1000", day=2), recorded_at=UTCTimestamp(T0))


def test_full_disposal_takes_exact_known_and_unknown_residuals() -> None:
    known = replay(
        (
            opening(1, "USDT", "10", "10"),
            buy(2, "BTC", "3", "1"),
            sell(3, "BTC", "1", "1"),
            sell(4, "BTC", "2", "2"),
        )
    )
    assert known.sales[0].known_cost_removed_usdt == d("0.333333333333333333333333")
    assert known.sales[1].known_cost_removed_usdt == d("0.666666666666666666666667")
    assert position(known, "BTC").quantity == 0
    assert position(known, "BTC").known_cost_usdt == 0
    assert position(known, "BTC").incomplete_quantity == 0
    unknown = replay(
        (
            opening(1, "ETH", "3", None, incomplete="3"),
            sell(2, "ETH", "1", "1"),
            sell(3, "ETH", "2", "2"),
        )
    )
    assert position(unknown, "ETH").quantity == 0
    assert position(unknown, "ETH").incomplete_quantity == 0
    assert unknown.total_realized_pnl_usdt is None


def test_fractional_quantities_and_precision_limit_on_unknown_split() -> None:
    result = replay((opening(1, "USDT", "1", "1"), buy(2, "BTC", "0.00000001", "0.0002")))
    assert position(result, "BTC").quantity == d("0.00000001")
    assert position(result, "BTC").average_cost_usdt == d("20000")
    with pytest.raises(DomainValidationError, match="allocation precision"):
        replay(
            (
                opening(
                    1,
                    "ETH",
                    "1",
                    "1",
                    incomplete="0.000000000000000000000001",
                    status=Completeness.PARTIAL,
                ),
                sell(2, "ETH", "0.6", "1"),
            )
        )


@pytest.mark.parametrize("sold", ["0.4", "0.6"])
@pytest.mark.parametrize("cost", ["0", "1"])
def test_mixed_cost_split_rejects_unrepresentable_known_quantity(sold: str, cost: str) -> None:
    ledger = (
        opening(
            1,
            "ETH",
            "1",
            cost,
            incomplete="0.999999999999999999999999",
            status=Completeness.PARTIAL,
        ),
        sell(2, "ETH", sold, "1"),
    )
    original = repr(ledger)
    with pytest.raises(DomainValidationError, match="Known quantity exceeds allocation precision"):
        replay(ledger)
    assert repr(ledger) == original
    assert position(replay((ledger[0],)), "ETH").known_cost_usdt == d(cost)


@pytest.mark.parametrize("sold", ["0.4", "0.6"])
def test_fully_unknown_position_stays_unknown_near_split_boundary(sold: str) -> None:
    result = replay((opening(1, "ETH", "1", None, incomplete="1"), sell(2, "ETH", sold, "1")))
    eth = position(result, "ETH")
    assert result.sales[0].incomplete_quantity_removed == d(sold)
    assert eth.incomplete_quantity == eth.quantity == d("1") - d(sold)
    assert result.sales[0].completeness is Completeness.UNKNOWN
    assert result.total_realized_pnl_usdt is None


def test_representable_one_quantum_known_component_survives_mixed_sale() -> None:
    result = replay(
        (
            opening(
                1,
                "ETH",
                "1",
                "1",
                incomplete="0.999999999999999999999998",
                status=Completeness.PARTIAL,
            ),
            sell(2, "ETH", "0.5", "1"),
        )
    )
    sale = result.sales[0]
    eth = position(result, "ETH")
    assert sale.quantity - sale.incomplete_quantity_removed == d("1e-24")
    assert eth.quantity - eth.incomplete_quantity == d("1e-24")
    assert sale.known_cost_removed_usdt == d("0.5")
    assert eth.known_cost_usdt == d("0.5")
    assert sale.incomplete_quantity_removed + eth.incomplete_quantity == d(
        "0.999999999999999999999998"
    )
    assert sale.realized_pnl_usdt is None


def test_repeated_mixed_sales_conserve_components_and_final_disposal() -> None:
    ledger = (
        opening(1, "ETH", "1", "1000"),
        opening(2, "ETH", "1", None, incomplete="1"),
        sell(3, "ETH", "0.25", "100"),
        sell(4, "ETH", "0.25", "100"),
        sell(5, "ETH", "1.5", "600"),
    )
    result = replay(tuple(reversed(ledger)))
    eth = position(result, "ETH")
    assert result.ordered_operation_ids == tuple(operation.id for operation in ledger)
    assert (eth.quantity, eth.known_cost_usdt, eth.incomplete_quantity) == (0, 0, 0)
    assert [sale.known_cost_removed_usdt for sale in result.sales] == [
        d("125"),
        d("125"),
        d("750"),
    ]
    assert [sale.incomplete_quantity_removed for sale in result.sales] == [
        d("0.125"),
        d("0.125"),
        d("0.75"),
    ]
    assert result.total_realized_pnl_usdt is None


def test_sub_quantum_average_cost_is_rejected_instead_of_reported_as_zero() -> None:
    with pytest.raises(DomainValidationError, match="Average cost exceeds"):
        replay((opening(1, "BTC", "10", "0.000000000000000000000001"),))


def test_large_exact_average_does_not_fail_due_to_quantized_trailing_zeros() -> None:
    result = replay((opening(1, "BTC", "1", "1e36"),))
    assert position(result, "BTC").average_cost_usdt == d("1e36")
    assert position(result, "BTC").known_cost_usdt == d("1e36")


def test_derived_average_beyond_supported_magnitude_is_still_rejected() -> None:
    with pytest.raises(DomainValidationError, match="too many digits"):
        replay((opening(1, "BTC", "1e-24", "1e36"),))


def test_large_exact_cost_allocation_conserves_basis() -> None:
    result = replay((opening(1, "BTC", "1", "1e37"), sell(2, "BTC", "0.5", "1")))
    assert result.sales[0].known_cost_removed_usdt == d("5e36")
    assert position(result, "BTC").known_cost_usdt == d("5e36")
    assert result.sales[0].known_cost_removed_usdt + position(result, "BTC").known_cost_usdt == d(
        "1e37"
    )


def test_large_exact_unknown_quantity_allocation_preserves_uncertainty() -> None:
    result = replay(
        (
            opening(1, "ETH", "2e37", None, incomplete="2e37"),
            sell(2, "ETH", "1e37", "1"),
        )
    )
    assert result.sales[0].incomplete_quantity_removed == d("1e37")
    assert position(result, "ETH").incomplete_quantity == d("1e37")
    assert result.sales[0].realized_pnl_usdt is None


@pytest.mark.parametrize(
    ("cost", "expected_removed"),
    [
        ("0.000000000000000000000003", "0.000000000000000000000002"),
        ("0.000000000000000000000005", "0.000000000000000000000002"),
    ],
)
def test_partial_cost_allocation_keeps_half_even_ties(cost: str, expected_removed: str) -> None:
    result = replay((opening(1, "BTC", "2", cost), sell(2, "BTC", "1", "1")))
    assert result.sales[0].known_cost_removed_usdt == d(expected_removed)
    assert result.sales[0].known_cost_removed_usdt + position(result, "BTC").known_cost_usdt == d(
        cost
    )


def test_equal_execution_times_use_ledger_sequence() -> None:
    initial = opening(1, "USDT", "1000", "1000")
    trade = buy(2, "BTC", "1", "1000")
    assert replay((trade, initial)).ordered_operation_ids == (initial.id, trade.id)
    with pytest.raises(DomainValidationError, match="Insufficient USDT"):
        replay(
            (
                replace(initial, sequence=LedgerSequence(2)),
                replace(trade, sequence=LedgerSequence(1)),
            )
        )


def test_fee_cannot_be_silently_encoded_in_fee_free_trade() -> None:
    with pytest.raises(TypeError):
        BuyFacts(AssetId("BTC"), AssetQuantity(d("1")), USDTAmount(d("1000")), fee=d("1"))
    trade = buy(1, "BTC", "1", "1000")
    extra = BalancePosting(3, USDT, d("-1"), PostingRole.SETTLEMENT)
    with pytest.raises(DomainValidationError, match="unsupported fees"):
        replace(trade, postings=(*trade.postings, extra))


def test_posting_order_amount_and_operation_type_must_match_facts() -> None:
    trade = buy(2, "BTC", "1", "1000")
    bad_settlement = replace(trade.postings[1], delta=d("-999"))
    for postings in (
        (trade.postings[1], trade.postings[0]),
        (trade.postings[0], bad_settlement),
    ):
        with pytest.raises(DomainValidationError, match="Postings contradict"):
            replace(trade, postings=postings)
    with pytest.raises(DomainValidationError, match="type and facts disagree"):
        replace(trade, type=OperationType.SELL)


@pytest.mark.parametrize("operation", [buy(2, "BTC", "1", "1000"), sell(3, "BTC", "0.5", "750")])
def test_postings_conserve_fee_free_trade_facts(operation: LedgerOperation) -> None:
    facts = operation.facts
    assert len(operation.postings) == 2
    if isinstance(facts, BuyFacts):
        assert operation.postings[0].delta == facts.quantity.value
        assert operation.postings[1].delta == -facts.principal_usdt.value
    else:
        assert isinstance(facts, SellFacts)
        assert operation.postings[0].delta == -facts.quantity.value
        assert operation.postings[1].delta == facts.proceeds_usdt.value


def test_golden_ledger_complete_state_and_partial_pnl() -> None:
    ledger = (
        sell(5, "ETH", "0.5", "750", day=3),
        opening(1, "USDT", "5000", "5000"),
        opening(2, "ETH", "1", "1000"),
        opening(3, "ETH", "1", None, incomplete="1"),
        buy(4, "BTC", "1", "2000", day=2),
    )
    result = replay(ledger)
    assert result.ordered_operation_ids == tuple(ledger[i].id for i in (1, 2, 3, 4, 0))
    assert [
        (p.asset.value, p.quantity, p.known_cost_usdt, p.incomplete_quantity, p.completeness)
        for p in result.assets
    ] == [
        ("BTC", d("1"), d("2000"), d("0"), Completeness.COMPLETE),
        ("ETH", d("1.5"), d("750"), d("0.75"), Completeness.PARTIAL),
        ("USDT", d("3750"), d("3750"), d("0"), Completeness.COMPLETE),
    ]
    assert result.sales[0].known_cost_removed_usdt == d("250")
    assert result.sales[0].realized_pnl_usdt is None
    assert result.total_realized_pnl_usdt is None
