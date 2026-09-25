from dataclasses import MISSING, FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.portfolio.models import (
    LimitOrder,
    LimitOrderStatus,
    TradingSide,
)

ORDER_ID = UUID("16583db1-1177-4931-8a87-fafcbbaef969")
ACCOUNT_ID = UUID("caee0d03-1958-4129-949c-b78ad1ac7cd3")
NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def order(**changes) -> LimitOrder:
    values = dict(
        id=ORDER_ID,
        account_id=ACCOUNT_ID,
        asset="ETH",
        side=TradingSide.BUY,
        target_price=Decimal("1234.000000000000000001"),
        quantity_asset=Decimal("0.01000000000000000001"),
        status=LimitOrderStatus.OPEN,
        created_at=NOW,
        updated_at=NOW,
    )
    return LimitOrder(**(values | changes))


def test_order_preserves_explicit_deterministic_account_uuid() -> None:
    result = order()

    assert result.account_id is ACCOUNT_ID
    assert result.id is ORDER_ID
    assert result.asset == "ETH"
    assert result.side is TradingSide.BUY


def test_account_identity_is_immutable() -> None:
    result = order()

    with pytest.raises(FrozenInstanceError):
        result.account_id = UUID("8607259b-c5f9-4262-b3b8-84cb87969425")


@pytest.mark.parametrize("account_id", [UUID(int=0), "not-a-uuid", None, 123])
def test_invalid_account_identity_uses_existing_uuid_validation(account_id: object) -> None:
    with pytest.raises(DomainValidationError, match="non-nil UUID"):
        order(account_id=account_id)


def test_account_id_has_no_default_or_silent_manual_assignment() -> None:
    account_field = next(value for value in fields(LimitOrder) if value.name == "account_id")

    assert account_field.default is MISSING
    assert account_field.default_factory is MISSING
    with pytest.raises(TypeError, match="account_id"):
        LimitOrder(
            ORDER_ID,
            "ETH",
            TradingSide.BUY,
            Decimal("100"),
            Decimal("1"),
            LimitOrderStatus.OPEN,
            NOW,
            NOW,
        )


def test_buy_order_invariants_and_exact_decimal_terms_are_unchanged() -> None:
    result = order()

    assert result.target_price.as_tuple() == Decimal("1234.000000000000000001").as_tuple()
    assert result.quantity_asset.as_tuple() == Decimal("0.01000000000000000001").as_tuple()
    with pytest.raises(DomainValidationError, match="Open order cannot be resolved"):
        order(resolved_at=NOW)
    with pytest.raises(DomainValidationError, match="positive"):
        order(target_price=Decimal("0"))


def test_sell_order_invariants_are_unchanged() -> None:
    resolved_at = NOW + timedelta(minutes=1)
    result = order(
        side=TradingSide.SELL,
        status=LimitOrderStatus.FILLED,
        updated_at=resolved_at,
        resolved_at=resolved_at,
    )

    assert result.side is TradingSide.SELL
    assert result.resolved_at == resolved_at
    assert result.account_id == ACCOUNT_ID
    with pytest.raises(DomainValidationError, match="Timestamp must be timezone-aware"):
        order(side=TradingSide.SELL, status=LimitOrderStatus.FILLED, resolved_at=None)
    with pytest.raises(DomainValidationError, match="positive"):
        order(side=TradingSide.SELL, quantity_asset=Decimal("-1"))
