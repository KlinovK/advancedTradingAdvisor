"""Offline fee-aware replay against approved Decimal accounting fixtures."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.financial.fee_aware import (
    EffectiveOrder,
    EventProvenance,
    FeeAwareTrade,
    FeeCategory,
    GrossTradeExecution,
    HistoricalUSDTPrice,
    HistoricalUSDTValue,
    OrderingBasis,
    PriceRecordKind,
    SourceIdentity,
    SourceNamespace,
    SourceProvider,
    TradeFee,
    TradeSide,
)
from advanced_trading_advisor.financial.fee_replay import (
    CalculationStatus,
    VerifiedOrder,
    VerifiedValuation,
    replay_fee_aware,
)
from advanced_trading_advisor.financial.ledger import (
    BalancePosting,
    Completeness,
    LedgerOperation,
    OpeningFacts,
    OperationType,
    PostingRole,
)
from advanced_trading_advisor.financial.replay import replay as replay_v4
from advanced_trading_advisor.financial.values import (
    USDT,
    AssetId,
    AssetQuantity,
    LedgerSequence,
    OperationId,
    USDTAmount,
    UTCTimestamp,
)

T0 = UTCTimestamp(datetime(2026, 1, 1, tzinfo=UTC))
T1 = UTCTimestamp(datetime(2026, 1, 2, tzinfo=UTC))


def d(value: str) -> Decimal:
    return Decimal(value)


def opening(
    number: int,
    asset: str,
    quantity: str,
    cost: str | None,
    *,
    incomplete: str = "0",
    completeness: Completeness | None = None,
) -> LedgerOperation:
    asset_id = AssetId(asset)
    facts = OpeningFacts(
        asset_id,
        AssetQuantity(d(quantity)),
        USDTAmount(d(cost)) if cost is not None else None,
        AssetQuantity(d(incomplete)),
        completeness or (Completeness.UNKNOWN if cost is None else Completeness.COMPLETE),
    )
    return LedgerOperation(
        OperationId(UUID(int=number)),
        LedgerSequence(number),
        T0,
        T0,
        OperationType.OPENING_BALANCE,
        facts,
        (BalancePosting(1, asset_id, d(quantity), PostingRole.OPENING),),
    )


def trade(
    number: int,
    side: TradeSide,
    fee_asset: str,
    fee_quantity: str,
    *,
    principal: str = "100",
    quantity: str = "1",
    committed: bool = True,
    namespace: SourceNamespace = SourceNamespace.SPOT_TRADE,
    when: UTCTimestamp = T1,
    path: tuple[int, ...] | None = None,
    evidence: HistoricalUSDTValue | None = None,
) -> FeeAwareTrade:
    fee_id = AssetId(fee_asset)
    category = (
        FeeCategory.BASE_ASSET
        if fee_id == AssetId("BTC")
        else FeeCategory.QUOTE_ASSET
        if fee_id == USDT
        else FeeCategory.THIRD_ASSET
    )
    source = SourceIdentity(
        UUID(int=1000),
        SourceProvider.BINANCE_SPOT,
        "account-A",
        AssetId("BTCUSDT"),
        namespace,
        str(number),
    )
    return FeeAwareTrade(
        OperationId(UUID(int=number)),
        GrossTradeExecution(
            side,
            AssetId("BTC"),
            USDT,
            AssetQuantity(d(quantity)),
            USDTAmount(d(principal)),
            when,
        ),
        TradeFee(fee_id, AssetQuantity(d(fee_quantity)), category, evidence),
        EventProvenance(
            source,
            str(number),
            f"fill-{number}",
            when,
            LedgerSequence(number) if committed else None,
        ),
        EffectiveOrder(path or (number,), OrderingBasis.PROVIDER_EXECUTION_ORDER, f"order-{number}")
        if committed
        else None,
    )


def direct_for(operation: FeeAwareTrade, value: str = "1.5") -> HistoricalUSDTValue:
    return HistoricalUSDTValue(
        AssetId("BNB"),
        operation.execution.executed_at,
        USDTAmount(d(value)),
        SourceProvider.BINANCE_SPOT,
        "archived-fee-values",
        f"value-{operation.operation_id.value}",
        "E1",
        1000,
        operation.fee.quantity,
        operation.provenance.source,
    )


@pytest.mark.parametrize(
    ("case", "side", "fee_asset", "fee_quantity", "initial", "expected"),
    [
        (
            "B1",
            TradeSide.BUY,
            "BTC",
            "0.01",
            (("USDT", "1000", "1000"),),
            (("BTC", "0.99", "100"), ("USDT", "900", "900"), None, None, None),
        ),
        (
            "B2",
            TradeSide.BUY,
            "USDT",
            "1",
            (("USDT", "1000", "1000"),),
            (("BTC", "1", "101"), ("USDT", "899", "899"), None, None, None),
        ),
        (
            "B3",
            TradeSide.BUY,
            "BNB",
            "0.1",
            (("USDT", "1000", "1000"), ("BNB", "2", "20")),
            (
                ("BTC", "1", "101.5"),
                ("USDT", "900", "900"),
                ("BNB", "1.9", "19"),
                None,
                "0.5",
                "0.5",
            ),
        ),
        (
            "S1",
            TradeSide.SELL,
            "BTC",
            "0.01",
            (("BTC", "2", "200"),),
            (("BTC", "0.99", "99"), ("USDT", "100", "100"), "-1", None, "-1"),
        ),
        (
            "S2",
            TradeSide.SELL,
            "USDT",
            "1",
            (("BTC", "2", "200"),),
            (("BTC", "1", "100"), ("USDT", "99", "99"), "-1", None, "-1"),
        ),
        (
            "S3",
            TradeSide.SELL,
            "BNB",
            "0.1",
            (("BTC", "2", "200"), ("BNB", "2", "20")),
            (
                ("BTC", "1", "100"),
                ("USDT", "100", "100"),
                ("BNB", "1.9", "19"),
                "-1.5",
                "0.5",
                "-1",
            ),
        ),
    ],
)
def test_six_approved_golden_cases(
    case: str,
    side: TradeSide,
    fee_asset: str,
    fee_quantity: str,
    initial: tuple[tuple[str, str, str], ...],
    expected: tuple,
) -> None:
    entries = tuple(
        opening(index, asset, qty, cost)
        for index, (asset, qty, cost) in enumerate(initial, start=1)
    )
    fill = trade(10, side, fee_asset, fee_quantity)
    attestations = ()
    if fee_asset == "BNB":
        fill = replace(fill, fee=replace(fill.fee, valuation=direct_for(fill)))
        attestations = (VerifiedValuation(fill.operation_id, fill.fee.valuation),)
    result = replay_fee_aware((*entries, fill), verified_valuations=attestations)
    expected_assets = expected[:3] if case in ("B3", "S3") else expected[:2]
    for asset, quantity, cost in expected_assets:
        state = result.asset(AssetId(asset))
        assert (state.quantity, state.known_cost_usdt) == (d(quantity), d(cost))
        assert state.unknown_basis_quantity == d("0")
        assert state.calculation_status is CalculationStatus.COMPLETE
    expected_pnl = expected[3:] if case in ("B3", "S3") else expected[2:]
    event = result.events[-1]
    assert (event.trade_pnl_usdt, event.fee_disposal_pnl_usdt, event.combined_pnl_usdt) == tuple(
        d(value) if value is not None else None for value in expected_pnl
    )
    assert result.calculation_status is CalculationStatus.COMPLETE
    expected_disposals = {
        "B1": ("0", "0"),
        "B2": ("0", "0"),
        "B3": ("0", "1"),
        "S1": ("101", "0"),
        "S2": ("100", "0"),
        "S3": ("100", "1"),
    }
    trade_cost, fee_cost = expected_disposals[case]
    assert (event.trade_known_cost_removed_usdt, event.fee_known_cost_removed_usdt) == (
        d(trade_cost),
        d(fee_cost),
    )
    assert event.calculation_status is CalculationStatus.COMPLETE


def test_unknown_basis_and_unverified_fee_never_become_complete() -> None:
    unknown = replay_fee_aware(
        (
            opening(1, "BTC", "1", None, incomplete="1"),
            trade(2, TradeSide.SELL, "BTC", "0"),
        )
    )
    assert unknown.events[0].combined_pnl_usdt is None
    assert unknown.calculation_status is CalculationStatus.UNAVAILABLE
    fill = trade(2, TradeSide.BUY, "BNB", "0.1")
    fill = replace(fill, fee=replace(fill.fee, valuation=direct_for(fill)))
    result = replay_fee_aware(
        (
            opening(1, "USDT", "1000", "1000"),
            opening(3, "BNB", "2", "20"),
            fill,
        )
    )
    assert result.asset(AssetId("BTC")).unknown_additive_cost
    assert result.asset(AssetId("BTC")).calculation_status is CalculationStatus.PARTIAL
    assert result.events[0].fee_disposal_pnl_usdt is None
    assert result.calculation_status is CalculationStatus.UNAVAILABLE


def test_mixed_unknown_basis_propagates_without_fabricating_pnl() -> None:
    result = replay_fee_aware(
        (
            opening(1, "BTC", "1", "100"),
            opening(2, "BTC", "1", None, incomplete="1"),
            trade(3, TradeSide.SELL, "BTC", "0"),
        )
    )
    assert result.asset(AssetId("BTC")).quantity == d("1")
    assert result.asset(AssetId("BTC")).unknown_basis_quantity == d("0.5")
    assert result.events[0].trade_known_cost_removed_usdt == d("50")
    assert result.events[0].combined_pnl_usdt is None


@pytest.mark.parametrize(
    ("cost", "incomplete", "completeness", "expected"),
    [
        ("0", "0", Completeness.COMPLETE, CalculationStatus.COMPLETE),
        (None, "2", Completeness.UNKNOWN, CalculationStatus.UNAVAILABLE),
        ("100", "1", Completeness.PARTIAL, CalculationStatus.PARTIAL),
        ("0", "1", Completeness.PARTIAL, CalculationStatus.PARTIAL),
    ],
)
def test_position_completeness_uses_known_quantity_not_positive_cost(
    cost: str | None,
    incomplete: str,
    completeness: Completeness,
    expected: CalculationStatus,
) -> None:
    result = replay_fee_aware(
        (opening(1, "BTC", "2", cost, incomplete=incomplete, completeness=completeness),)
    )
    assert result.asset(AssetId("BTC")).calculation_status is expected


def test_zero_cost_mixed_basis_remains_partial_after_sale() -> None:
    result = replay_fee_aware(
        (
            opening(1, "BTC", "1", "0"),
            opening(2, "BTC", "1", None, incomplete="1"),
            trade(3, TradeSide.SELL, "BTC", "0"),
        )
    )
    position = result.asset(AssetId("BTC"))
    assert (position.quantity, position.unknown_basis_quantity, position.known_cost_usdt) == (
        d("1"),
        d("0.5"),
        d("0"),
    )
    assert position.calculation_status is CalculationStatus.PARTIAL
    assert result.events[0].calculation_status is CalculationStatus.PARTIAL
    assert result.events[0].combined_pnl_usdt is None


def test_known_zero_realized_pnl_is_complete() -> None:
    result = replay_fee_aware((opening(1, "BTC", "1", "100"), trade(2, TradeSide.SELL, "BTC", "0")))
    assert result.events[0].combined_pnl_usdt == d("0")
    assert result.events[0].calculation_status is CalculationStatus.COMPLETE
    assert result.total_realized_pnl_usdt == d("0")


def test_rejects_negative_prefix_duplicate_identity_candidates_and_sor() -> None:
    fill = trade(2, TradeSide.BUY, "BTC", "0")
    with pytest.raises(DomainValidationError, match="Insufficient USDT"):
        replay_fee_aware((fill,))
    with pytest.raises(DomainValidationError, match="Duplicate operation"):
        replay_fee_aware((opening(1, "USDT", "1000", "1000"), fill, fill))
    with pytest.raises(DomainValidationError, match="Duplicate active source"):
        replay_fee_aware(
            (
                opening(1, "USDT", "1000", "1000"),
                fill,
                replace(
                    fill,
                    operation_id=OperationId(UUID(int=4)),
                    provenance=replace(fill.provenance, append_sequence=LedgerSequence(4)),
                ),
            )
        )
    with pytest.raises(DomainValidationError, match="Candidate"):
        replay_fee_aware((trade(2, TradeSide.BUY, "BTC", "0", committed=False),))
    with pytest.raises(DomainValidationError, match="SOR"):
        trade(2, TradeSide.BUY, "BTC", "0", namespace=SourceNamespace.SOR_ALLOCATION)


def test_equal_time_order_requires_attestation_and_matching_paths() -> None:
    first = trade(3, TradeSide.BUY, "BTC", "0", path=(1,))
    second = trade(4, TradeSide.SELL, "BTC", "0", path=(2,))
    history = (opening(1, "USDT", "200", "200"), opening(2, "BTC", "1", "100"))
    with pytest.raises(DomainValidationError, match="Unresolved"):
        replay_fee_aware((*history, first, second))
    with pytest.raises(DomainValidationError, match="disagrees"):
        replay_fee_aware(
            (*history, first, second),
            verified_orders=(VerifiedOrder(T1, (second.operation_id, first.operation_id)),),
        )
    resolved = replay_fee_aware(
        (*history, first, second),
        verified_orders=(VerifiedOrder(T1, (first.operation_id, second.operation_id)),),
    )
    assert resolved.ordered_operation_ids[-2:] == (first.operation_id, second.operation_id)


def test_duplicate_source_rejects_unprojected_correction_and_replay_is_deterministic() -> None:
    fill = trade(2, TradeSide.BUY, "BTC", "0")
    history = (opening(1, "USDT", "1000", "1000"), fill)
    assert replay_fee_aware(history) == replay_fee_aware(history)
    correction_like = replace(
        fill,
        operation_id=OperationId(UUID(int=3)),
        provenance=replace(fill.provenance, append_sequence=LedgerSequence(3)),
    )
    with pytest.raises(DomainValidationError, match="Duplicate active source"):
        replay_fee_aware((*history, correction_like))


def test_fee_aware_trades_from_different_portfolios_cannot_mix() -> None:
    first = trade(2, TradeSide.BUY, "BTC", "0")
    second = trade(3, TradeSide.BUY, "BTC", "0")
    second_source = replace(second.provenance.source, portfolio_id=UUID(int=2000))
    second = replace(second, provenance=replace(second.provenance, source=second_source))
    with pytest.raises(DomainValidationError, match="portfolio identities"):
        replay_fee_aware((opening(1, "USDT", "1000", "1000"), first, second))


def test_third_asset_sale_can_have_complete_combined_pnl_without_fee_value() -> None:
    result = replay_fee_aware(
        (
            opening(1, "BTC", "2", "200"),
            opening(2, "BNB", "2", "20"),
            trade(3, TradeSide.SELL, "BNB", "0.1"),
        )
    )
    sale = result.events[0]
    assert sale.trade_pnl_usdt is None
    assert sale.fee_disposal_pnl_usdt is None
    assert sale.combined_pnl_usdt == d("-1")
    assert result.calculation_status is CalculationStatus.COMPLETE


def test_known_zero_cost_trade_pnl_remains_partial_when_fee_basis_unknown() -> None:
    fill = trade(3, TradeSide.SELL, "BNB", "0.1")
    fill = replace(fill, fee=replace(fill.fee, valuation=direct_for(fill)))
    result = replay_fee_aware(
        (
            opening(1, "BTC", "2", "0"),
            opening(2, "BNB", "2", None, incomplete="2"),
            fill,
        ),
        verified_valuations=(VerifiedValuation(fill.operation_id, fill.fee.valuation),),
    )
    event = result.events[0]
    assert event.trade_pnl_usdt == d("98.5")
    assert event.fee_disposal_pnl_usdt is None
    assert event.combined_pnl_usdt is None
    assert event.calculation_status is CalculationStatus.PARTIAL
    assert result.asset(AssetId("BNB")).unknown_basis_quantity == d("1.9")
    assert result.calculation_status is CalculationStatus.UNAVAILABLE


def test_unknown_fee_asset_basis_blocks_fee_disposal_pnl() -> None:
    fill = trade(3, TradeSide.BUY, "BNB", "0.1")
    fill = replace(fill, fee=replace(fill.fee, valuation=direct_for(fill)))
    result = replay_fee_aware(
        (
            opening(1, "USDT", "1000", "1000"),
            opening(2, "BNB", "2", None, incomplete="2"),
            fill,
        ),
        verified_valuations=(VerifiedValuation(fill.operation_id, fill.fee.valuation),),
    )
    assert result.asset(AssetId("BTC")).known_cost_usdt == d("101.5")
    assert result.events[0].fee_disposal_pnl_usdt is None
    assert result.total_realized_pnl_usdt is None


def test_insufficient_base_third_asset_and_quote_fee_cash_reject() -> None:
    with pytest.raises(DomainValidationError, match="Insufficient asset"):
        replay_fee_aware((opening(1, "BTC", "1", "100"), trade(2, TradeSide.SELL, "BTC", "0.01")))
    with pytest.raises(DomainValidationError, match="Insufficient asset"):
        replay_fee_aware(
            (
                opening(1, "USDT", "1000", "1000"),
                trade(2, TradeSide.BUY, "BNB", "0.1"),
            )
        )
    with pytest.raises(DomainValidationError, match="Insufficient USDT"):
        replay_fee_aware(
            (
                opening(1, "USDT", "100", "100"),
                trade(2, TradeSide.BUY, "USDT", "1"),
            )
        )


def test_zero_net_quote_fee_sell_and_v4_parity() -> None:
    zero_net = trade(2, TradeSide.SELL, "USDT", "100")
    result = replay_fee_aware((opening(1, "BTC", "1", "80"), zero_net))
    assert result.asset(USDT).quantity == 0
    assert result.events[0].combined_pnl_usdt == d("-80")
    opening_cash = opening(1, "USDT", "200", "200")
    opening_btc = opening(2, "BTC", "1", "100")
    base = (opening_cash, opening_btc)
    legacy = replay_v4(base)
    extended = replay_fee_aware(base)
    for asset in (USDT, AssetId("BTC")):
        assert extended.asset(asset).quantity == legacy.asset(asset).quantity
        assert extended.asset(asset).known_cost_usdt == legacy.asset(asset).known_cost_usdt


def test_verified_evidence_must_match_exact_event_and_fee() -> None:
    fill = trade(2, TradeSide.BUY, "BNB", "0.1")
    evidence = direct_for(fill)
    fill = replace(fill, fee=replace(fill.fee, valuation=evidence))
    history = (opening(1, "USDT", "1000", "1000"), opening(3, "BNB", "2", "20"), fill)
    with pytest.raises(DomainValidationError, match="does not match"):
        replay_fee_aware(
            history,
            verified_valuations=(
                VerifiedValuation(
                    fill.operation_id, replace(evidence, value_usdt=USDTAmount(d("2")))
                ),
            ),
        )
    with pytest.raises(DomainValidationError, match="Duplicate valuation"):
        replay_fee_aware(
            history,
            verified_valuations=(
                VerifiedValuation(fill.operation_id, evidence),
                VerifiedValuation(fill.operation_id, evidence),
            ),
        )


def test_verified_price_uses_exact_decimal_product_and_rejects_excess_scale() -> None:
    fill = trade(3, TradeSide.BUY, "BNB", "0.1")
    price = HistoricalUSDTPrice(
        AssetId("BNB"),
        USDT,
        AssetId("BNBUSDT"),
        PriceRecordKind.SPOT_EXECUTION,
        T1,
        d("15"),
        SourceProvider.BINANCE_SPOT,
        "archive",
        "price-1",
        "E1",
        1000,
    )
    fill = replace(fill, fee=replace(fill.fee, valuation=price))
    history = (opening(1, "USDT", "1000", "1000"), opening(2, "BNB", "2", "20"), fill)
    result = replay_fee_aware(
        history, verified_valuations=(VerifiedValuation(fill.operation_id, price),)
    )
    assert result.asset(AssetId("BTC")).known_cost_usdt == d("101.5")
    too_precise = replace(price, price_usdt=d("0.000000000000000000000001"))
    with pytest.raises(DomainValidationError, match="scale"):
        replay_fee_aware(
            (*history[:-1], replace(fill, fee=replace(fill.fee, valuation=too_precise))),
            verified_valuations=(VerifiedValuation(fill.operation_id, too_precise),),
        )


def test_input_permutation_preserves_evidenced_order_and_accounting() -> None:
    cash = opening(1, "USDT", "200", "200")
    base = opening(2, "BTC", "1", "100")
    first = trade(3, TradeSide.BUY, "BTC", "0", path=(1,), principal="200")
    second = trade(4, TradeSide.SELL, "BTC", "0", path=(2,), principal="300")
    verified = (VerifiedOrder(T1, (first.operation_id, second.operation_id)),)
    original = replay_fee_aware((cash, base, first, second), verified_orders=verified)
    permuted = replay_fee_aware((second, base, cash, first), verified_orders=verified)
    assert permuted == original
    assert original.events[1].combined_pnl_usdt == d("150")
    assert original.asset(AssetId("BTC")).known_cost_usdt == d("150")
    with pytest.raises(DomainValidationError, match="does not match"):
        replay_fee_aware(
            (cash, base, first, second),
            verified_orders=(VerifiedOrder(T1, (first.operation_id, cash.id)),),
        )


def test_later_credit_cannot_repair_negative_historical_prefix() -> None:
    sale = trade(2, TradeSide.SELL, "BTC", "0")
    t2 = UTCTimestamp(datetime(2026, 1, 3, tzinfo=UTC))
    later = replace(opening(1, "BTC", "1", "100"), executed_at=t2, recorded_at=t2)
    with pytest.raises(DomainValidationError, match="Insufficient asset"):
        replay_fee_aware((sale, later))


def test_rejected_candidate_does_not_change_accepted_replay() -> None:
    history = (opening(1, "USDT", "1000", "1000"), trade(2, TradeSide.BUY, "BTC", "0"))
    accepted = replay_fee_aware(history)
    with pytest.raises(DomainValidationError, match="Candidate"):
        replay_fee_aware((*history, trade(3, TradeSide.BUY, "BTC", "0", committed=False)))
    assert replay_fee_aware(history) == accepted


def test_direct_fee_valuation_cannot_be_reused_for_another_fill() -> None:
    first = trade(2, TradeSide.BUY, "BNB", "0.1")
    second = trade(3, TradeSide.BUY, "BNB", "0.1")
    first_evidence = direct_for(first)
    with pytest.raises(DomainValidationError, match="does not match"):
        replace(second, fee=replace(second.fee, valuation=first_evidence))


def test_replay_exposes_calculation_without_claiming_source_coverage() -> None:
    result = replay_fee_aware((opening(1, "BTC", "1", "100"),))
    assert result.calculation_status is CalculationStatus.COMPLETE
    assert not hasattr(result, "source_history_coverage")
