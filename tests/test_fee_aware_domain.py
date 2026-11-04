"""Offline construction contracts for immutable fee-aware trade facts."""

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.financial.fee_aware import (
    CorrectionProvenance,
    EffectiveOrder,
    EventProvenance,
    FeeAwareTrade,
    FeeCategory,
    GrossTradeExecution,
    HistoricalUSDTPrice,
    HistoricalUSDTValue,
    OrderingBasis,
    OwnerOrderResolution,
    PriceRecordKind,
    SourceIdentity,
    SourceNamespace,
    SourceProvider,
    TradeFee,
    TradeSide,
    classify_fee,
)
from advanced_trading_advisor.financial.values import (
    USDT,
    AssetId,
    AssetQuantity,
    LedgerSequence,
    OperationId,
    USDTAmount,
    UTCTimestamp,
)

T0 = UTCTimestamp(datetime(2026, 1, 1, 12, 0, 0, 123000, tzinfo=UTC))


def d(value: str) -> Decimal:
    return Decimal(value)


def source(
    namespace: SourceNamespace = SourceNamespace.SPOT_TRADE,
    external_execution_id: str = "42",
) -> SourceIdentity:
    return SourceIdentity(
        UUID(int=1),
        SourceProvider.BINANCE_SPOT,
        "account-1",
        AssetId("BTCUSDT"),
        namespace,
        external_execution_id,
    )


def price(
    *,
    asset: str = "BNB",
    value: str = "300",
    valued_at: UTCTimestamp = T0,
    resolution_us: int = 1000,
) -> HistoricalUSDTPrice:
    return HistoricalUSDTPrice(
        AssetId(asset),
        USDT,
        AssetId(asset + "USDT"),
        PriceRecordKind.SPOT_EXECUTION,
        valued_at,
        d(value),
        SourceProvider.BINANCE_SPOT,
        "historical-BNBUSDT",
        "price-record-7",
        "archive-record-7",
        resolution_us,
    )


def trade(
    *,
    side: TradeSide = TradeSide.BUY,
    fee_asset: str = "BTC",
    fee_quantity: str = "0.01",
    fee_category: FeeCategory = FeeCategory.BASE_ASSET,
    valuation: HistoricalUSDTPrice | HistoricalUSDTValue | None = None,
    source_identity: SourceIdentity | None = None,
    committed: bool = True,
) -> FeeAwareTrade:
    execution = GrossTradeExecution(
        side,
        AssetId("BTC"),
        USDT,
        AssetQuantity(d("1")),
        USDTAmount(d("1000")),
        T0,
    )
    provenance = EventProvenance(
        source_identity or source(),
        "order-3",
        "fill-archive-42",
        T0,
        LedgerSequence(5) if committed else None,
    )
    return FeeAwareTrade(
        OperationId(UUID(int=2)),
        execution,
        TradeFee(AssetId(fee_asset), AssetQuantity(d(fee_quantity)), fee_category, valuation),
        provenance,
        (
            EffectiveOrder((2, -1, 3), OrderingBasis.PROVIDER_EXECUTION_ORDER, "order-evidence-42")
            if committed
            else None
        ),
    )


@pytest.mark.parametrize(
    ("asset", "category"),
    [
        ("BTC", FeeCategory.BASE_ASSET),
        ("USDT", FeeCategory.QUOTE_ASSET),
        ("BNB", FeeCategory.THIRD_ASSET),
    ],
)
@pytest.mark.parametrize("side", [TradeSide.BUY, TradeSide.SELL])
def test_buy_sell_and_central_fee_classification(
    side: TradeSide, asset: str, category: FeeCategory
) -> None:
    valuation = price() if asset == "BNB" else None
    result = trade(side=side, fee_asset=asset, fee_category=category, valuation=valuation)
    assert classify_fee(AssetId("BTC"), USDT, AssetId(asset)) is category
    assert result.execution.side is side
    assert result.execution.gross_base_quantity.value == d("1")
    assert result.execution.gross_quote_quantity.value == d("1000")
    assert result.fee.quantity.value == d("0.01")
    assert result.fee.category is category
    assert result.operation_id.value != result.provenance.source.portfolio_id


def test_missing_third_asset_valuation_is_explicitly_incomplete() -> None:
    result = trade(fee_asset="BNB", fee_category=FeeCategory.THIRD_ASSET)
    assert result.fee.valuation is None
    assert result.fee.category is FeeCategory.THIRD_ASSET


def test_fee_rules_reject_inconsistent_or_impossible_facts() -> None:
    base = trade()
    with pytest.raises(DomainValidationError, match="classification"):
        replace(
            base, fee=TradeFee(AssetId("BTC"), AssetQuantity(d("0.01")), FeeCategory.QUOTE_ASSET)
        )
    with pytest.raises(DomainValidationError, match="positive acquired"):
        replace(base, fee=TradeFee(AssetId("BTC"), AssetQuantity(d("1")), FeeCategory.BASE_ASSET))
    with pytest.raises(DomainValidationError, match="gross proceeds"):
        replace(
            trade(side=TradeSide.SELL, fee_asset="USDT", fee_category=FeeCategory.QUOTE_ASSET),
            fee=TradeFee(
                USDT, AssetQuantity(d("1000.000000000000000000000001")), FeeCategory.QUOTE_ASSET
            ),
        )
    zero_net = replace(
        trade(side=TradeSide.SELL, fee_asset="USDT", fee_category=FeeCategory.QUOTE_ASSET),
        fee=TradeFee(USDT, AssetQuantity(d("1000")), FeeCategory.QUOTE_ASSET),
    )
    assert zero_net.fee.quantity.value == zero_net.execution.gross_quote_quantity.value
    with pytest.raises(DomainValidationError, match="valuation"):
        replace(
            base,
            fee=TradeFee(
                AssetId("BTC"), AssetQuantity(d("0")), FeeCategory.BASE_ASSET, price(asset="BTC")
            ),
        )


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity", "1e-25", "1e60"])
def test_gross_quantities_reject_invalid_inputs(quantity: str) -> None:
    with pytest.raises(DomainValidationError):
        GrossTradeExecution(
            TradeSide.BUY, AssetId("BTC"), USDT, AssetQuantity(d(quantity)), USDTAmount(d("1")), T0
        )
    with pytest.raises(DomainValidationError):
        GrossTradeExecution(
            TradeSide.BUY, AssetId("BTC"), USDT, AssetQuantity(d("1")), USDTAmount(d(quantity)), T0
        )


@pytest.mark.parametrize("quantity", ["-1", "NaN", "Infinity", "1e-25", "1e60"])
def test_fee_quantities_preserve_decimal_policy(quantity: str) -> None:
    with pytest.raises(DomainValidationError):
        TradeFee(AssetId("BTC"), AssetQuantity(d(quantity)), FeeCategory.BASE_ASSET)
    assert trade(fee_quantity="0").fee.quantity.value == 0
    assert trade(fee_quantity="0.000000000000000000000001").fee.quantity.value == d("1e-24")


def test_non_usdt_pair_or_invalid_symbol_is_rejected() -> None:
    with pytest.raises(DomainValidationError, match="USDT quote"):
        GrossTradeExecution(
            TradeSide.BUY,
            AssetId("BTC"),
            AssetId("ETH"),
            AssetQuantity(d("1")),
            USDTAmount(d("1")),
            T0,
        )
    with pytest.raises(DomainValidationError):
        AssetId("btc")
    with pytest.raises(DomainValidationError, match="Source symbol"):
        replace(
            trade(),
            provenance=replace(
                trade().provenance, source=replace(source(), symbol=AssetId("ETHUSDT"))
            ),
        )


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity", "1e-25", "1e60"])
def test_invalid_historical_price_is_rejected(value: str) -> None:
    with pytest.raises(DomainValidationError):
        price(value=value)


def test_valuation_evidence_requires_exact_accounting_timestamp() -> None:
    exact = price()
    result = trade(fee_asset="BNB", fee_category=FeeCategory.THIRD_ASSET, valuation=exact)
    assert result.fee.valuation.matches(result.execution.executed_at)
    one_second = price(
        valued_at=UTCTimestamp(datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)),
        resolution_us=1_000_000,
    )
    assert not one_second.matches(T0)
    with pytest.raises(DomainValidationError, match="execution time"):
        trade(fee_asset="BNB", fee_category=FeeCategory.THIRD_ASSET, valuation=one_second)
    with pytest.raises(DomainValidationError, match="fee asset"):
        trade(fee_asset="BNB", fee_category=FeeCategory.THIRD_ASSET, valuation=price(asset="ETH"))
    with pytest.raises(DomainValidationError, match="execution time"):
        trade(
            fee_asset="BNB",
            fee_category=FeeCategory.THIRD_ASSET,
            valuation=price(valued_at=UTCTimestamp(T0.value + timedelta(milliseconds=1))),
        )
    with pytest.raises(DomainValidationError, match="aligned"):
        price(resolution_us=1_000_000)
    for resolution in (0, -1):
        with pytest.raises(DomainValidationError, match="resolution"):
            price(resolution_us=resolution)
    assert price(resolution_us=1).matches(T0)
    with pytest.raises(DomainValidationError, match="source pair"):
        replace(price(), quote_asset=AssetId("BTC"))
    with pytest.raises(DomainValidationError, match="source pair"):
        replace(price(), source_symbol=AssetId("BNBBTC"))
    with pytest.raises(DomainValidationError, match="Spot execution"):
        replace(price(), record_kind="ticker")


def test_enormous_source_resolution_cannot_make_stale_price_current() -> None:
    stale = price(
        valued_at=UTCTimestamp(datetime(1970, 1, 1, tzinfo=UTC)),
        resolution_us=10**20,
    )
    assert not stale.matches(T0)
    with pytest.raises(DomainValidationError, match="execution time"):
        trade(fee_asset="BNB", fee_category=FeeCategory.THIRD_ASSET, valuation=stale)


def test_direct_historical_usdt_value_is_distinct_from_price_evidence() -> None:
    direct = HistoricalUSDTValue(
        AssetId("BNB"),
        T0,
        USDTAmount(d("3")),
        SourceProvider.BINANCE_SPOT,
        "archived-direct-value",
        "record-9",
        "archive-record-9",
        1000,
        AssetQuantity(d("0.01")),
        source(),
    )
    result = trade(fee_asset="BNB", fee_category=FeeCategory.THIRD_ASSET, valuation=direct)
    assert result.fee.valuation.value_usdt.value == d("3")
    assert direct.matches(T0)
    with pytest.raises(DomainValidationError, match="positive"):
        replace(direct, value_usdt=USDTAmount(d("0")))
    with pytest.raises(DomainValidationError, match="execution time"):
        trade(
            fee_asset="BNB",
            fee_category=FeeCategory.THIRD_ASSET,
            valuation=replace(direct, valued_at=UTCTimestamp(T0.value + timedelta(milliseconds=1))),
        )
    with pytest.raises(DomainValidationError, match="fee or execution"):
        trade(
            fee_asset="BNB",
            fee_category=FeeCategory.THIRD_ASSET,
            valuation=replace(direct, fee_quantity=AssetQuantity(d("0.02"))),
        )
    with pytest.raises(DomainValidationError, match="fee or execution"):
        trade(
            fee_asset="BNB",
            fee_category=FeeCategory.THIRD_ASSET,
            valuation=replace(direct, execution_source=source(external_execution_id="43")),
        )
    for resolution in (0, -1):
        with pytest.raises(DomainValidationError, match="resolution"):
            replace(direct, source_resolution_us=resolution)


def test_enormous_source_resolution_cannot_make_stale_direct_value_current() -> None:
    direct = HistoricalUSDTValue(
        AssetId("BNB"),
        UTCTimestamp(datetime(1970, 1, 1, tzinfo=UTC)),
        USDTAmount(d("3")),
        SourceProvider.BINANCE_SPOT,
        "archived-direct-value",
        "record-9",
        "archive-record-9",
        10**20,
        AssetQuantity(d("0.01")),
        source(),
    )
    assert not direct.matches(T0)
    with pytest.raises(DomainValidationError, match="execution time"):
        trade(fee_asset="BNB", fee_category=FeeCategory.THIRD_ASSET, valuation=direct)


def test_trade_and_allocation_namespaces_do_not_collide() -> None:
    spot = source()
    sor = source(SourceNamespace.SOR_ALLOCATION)
    assert spot.external_execution_id == sor.external_execution_id
    assert spot != sor
    with pytest.raises(DomainValidationError, match="external execution"):
        replace(spot, external_execution_id="")
    with pytest.raises(DomainValidationError, match="exchange account"):
        replace(spot, exchange_account_key=" ")
    with pytest.raises(DomainValidationError, match="source namespace"):
        replace(spot, namespace="spot_trade")


def test_sor_identity_is_allowed_as_candidate_but_never_as_active_trade() -> None:
    sor = source(SourceNamespace.SOR_ALLOCATION)
    candidate = trade(source_identity=sor, committed=False)
    assert candidate.provenance.source.namespace is SourceNamespace.SOR_ALLOCATION
    assert candidate.provenance.append_sequence is None
    assert candidate.effective_order is None
    with pytest.raises(DomainValidationError, match="SOR allocations"):
        trade(source_identity=sor)
    assert trade().provenance.source.namespace is SourceNamespace.SPOT_TRADE


def test_candidate_and_committed_metadata_cannot_be_mixed() -> None:
    active = trade()
    candidate = trade(committed=False)
    assert active.provenance.append_sequence == LedgerSequence(5)
    assert active.effective_order is not None
    assert candidate.provenance.append_sequence is None
    assert candidate.effective_order is None
    with pytest.raises(DomainValidationError, match="both be present or absent"):
        replace(active, effective_order=None)
    with pytest.raises(DomainValidationError, match="both be present or absent"):
        replace(candidate, effective_order=active.effective_order)
    with pytest.raises(DomainValidationError, match="both be present or absent"):
        replace(active, provenance=replace(active.provenance, append_sequence=None))
    with pytest.raises(DomainValidationError, match="both be present or absent"):
        replace(
            candidate,
            provenance=replace(candidate.provenance, append_sequence=LedgerSequence(5)),
        )


def test_correction_provenance_requires_complete_valid_audit_metadata() -> None:
    original = OperationId(UUID(int=2))
    first_version = OperationId(UUID(int=3))
    correction = CorrectionProvenance(
        original, original, first_version, 1, 2, "corrected fee", UUID(int=7), T0
    )
    assert correction.version == 2
    assert replace(correction, reason="Исправлена комиссия").reason == "Исправлена комиссия"
    second = CorrectionProvenance(
        original,
        first_version,
        OperationId(UUID(int=4)),
        2,
        3,
        "second correction",
        UUID(int=7),
        T0,
    )
    assert second.original_event_id == correction.original_event_id
    assert second.target_version_id == correction.new_version_id
    for change in (
        {"version": 0},
        {"version": True},
        {"version": 3},
        {"target_version": 0},
        {"target_version_id": first_version},
        {"new_version_id": original},
        {"new_version_id": None},
        {"reason": ""},
        {"authorized_actor_id": UUID(int=0)},
        {"original_event_id": None},
        {"recorded_at": None},
    ):
        with pytest.raises(DomainValidationError):
            replace(correction, **change)
    with pytest.raises(DomainValidationError, match="target"):
        replace(second, target_version_id=original)
    with pytest.raises(DomainValidationError, match="refer to itself"):
        replace(second, new_version_id=second.target_version_id)


def test_effective_order_is_independent_of_append_sequence_and_recording_time() -> None:
    first = trade()
    later = replace(
        first,
        provenance=replace(
            first.provenance,
            append_sequence=LedgerSequence(99),
            recorded_at=UTCTimestamp(T0.value + timedelta(days=1)),
        ),
        effective_order=EffectiveOrder(
            (2, -2), OrderingBasis.PROVIDER_EXECUTION_ORDER, "provider-order-1"
        ),
    )
    assert later.provenance.append_sequence.value == 99
    assert later.effective_order.path < first.effective_order.path
    assert later.execution.executed_at == first.execution.executed_at
    assert later.provenance.recorded_at != first.provenance.recorded_at
    for path in ((), (1, True), [1, 2]):
        with pytest.raises(DomainValidationError):
            EffectiveOrder(path, OrderingBasis.PROVIDER_EXECUTION_ORDER, "evidence")
    with pytest.raises(DomainValidationError, match="ordering evidence"):
        EffectiveOrder((1,), OrderingBasis.PROVIDER_EXECUTION_ORDER, "")


def test_owner_resolution_requires_complete_structured_evidence() -> None:
    resolution = OwnerOrderResolution(
        UUID(int=7),
        "owner reviewed execution order",
        (OperationId(UUID(int=2)), OperationId(UUID(int=5))),
        OperationId(UUID(int=3)),
        OperationId(UUID(int=4)),
    )
    order = EffectiveOrder((2, -2), OrderingBasis.OWNER_RESOLUTION, "resolution-1", resolution)
    assert replace(trade(), effective_order=order).effective_order.owner_resolution == resolution
    with pytest.raises(DomainValidationError, match="structured resolution"):
        EffectiveOrder((2, -2), OrderingBasis.OWNER_RESOLUTION, "resolution-1")
    with pytest.raises(DomainValidationError, match="structured resolution"):
        EffectiveOrder((2, -2), OrderingBasis.PROVIDER_EXECUTION_ORDER, "resolution-1", resolution)
    for change in (
        {"authorized_actor_id": UUID(int=0)},
        {"reason": ""},
        {"compared_event_ids": (OperationId(UUID(int=2)),)},
        {"compared_event_ids": (OperationId(UUID(int=2)), OperationId(UUID(int=2)))},
        {"predecessor_id": None, "successor_id": None},
        {"predecessor_id": OperationId(UUID(int=4))},
    ):
        with pytest.raises(DomainValidationError):
            replace(resolution, **change)
    with pytest.raises(DomainValidationError, match="trade and its neighbors"):
        replace(
            trade(),
            effective_order=replace(
                order,
                owner_resolution=replace(
                    resolution,
                    compared_event_ids=(OperationId(UUID(int=5)), OperationId(UUID(int=6))),
                ),
            ),
        )
    with pytest.raises(DomainValidationError, match="trade and its neighbors"):
        replace(
            trade(),
            effective_order=replace(
                order, owner_resolution=replace(resolution, predecessor_id=OperationId(UUID(int=2)))
            ),
        )


def test_facts_are_immutable_and_do_not_mutate_v4_replay_types() -> None:
    result = trade()
    with pytest.raises(FrozenInstanceError):
        result.fee = None
    with pytest.raises(FrozenInstanceError):
        result.execution.side = TradeSide.SELL
    with pytest.raises(FrozenInstanceError):
        result.provenance.source.external_execution_id = "other"
    with pytest.raises(FrozenInstanceError):
        result.effective_order.path = (9,)
