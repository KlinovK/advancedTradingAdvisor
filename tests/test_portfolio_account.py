from dataclasses import FrozenInstanceError
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.portfolio.models import Account, AccountType, AssetPosition

ACCOUNT_ID = UUID("e2c87e22-b58d-4e0c-a5f6-94797af39dd9")
OTHER_ACCOUNT_ID = UUID("938d3971-9cfb-4942-a329-22d48c86f97a")


@pytest.mark.parametrize("account_type", list(AccountType))
def test_account_types_create_with_supplied_identity(account_type: AccountType) -> None:
    account = Account(ACCOUNT_ID, "External Wallet", account_type, ())

    assert account.id is ACCOUNT_ID
    assert account.type is account_type
    assert account.positions == ()


def test_account_preserves_multiple_positions_without_mutating_them() -> None:
    btc = AssetPosition("BTC", Decimal("0.0198"))
    usdc = AssetPosition("USDC", Decimal("1200.50"))
    positions = (btc, usdc)

    account = Account(ACCOUNT_ID, "Binance", AccountType.BINANCE, positions)

    assert account.positions is positions
    assert account.positions == positions
    assert account.positions[0] is btc
    assert account.positions[1] is usdc


def test_symbols_cannot_repeat_within_one_account() -> None:
    with pytest.raises(DomainValidationError, match="unique within an account"):
        Account(
            ACCOUNT_ID,
            "Binance",
            AccountType.BINANCE,
            (AssetPosition("ETH", Decimal("1")), AssetPosition("ETH", Decimal("2"))),
        )


def test_same_name_is_allowed_for_distinct_accounts() -> None:
    first = Account(ACCOUNT_ID, "External Wallet", AccountType.EXTERNAL_WALLET, ())
    second = Account(OTHER_ACCOUNT_ID, "External Wallet", AccountType.EXTERNAL_WALLET, ())

    assert first.name == second.name
    assert first.id != second.id


@pytest.mark.parametrize("name", ["", "  \t\n  "])
def test_blank_name_is_rejected(name: str) -> None:
    with pytest.raises(DomainValidationError, match="Account name is required"):
        Account(ACCOUNT_ID, name, AccountType.MANUAL, ())


@pytest.mark.parametrize("account_id", [UUID(int=0), "not-a-uuid"])
def test_account_uses_existing_uuid_validation(account_id: object) -> None:
    with pytest.raises(DomainValidationError, match="non-nil UUID"):
        Account(account_id, "Manual", AccountType.MANUAL, ())


def test_positions_must_be_an_immutable_tuple() -> None:
    with pytest.raises(DomainValidationError, match="Collection must be a tuple"):
        Account(ACCOUNT_ID, "Manual", AccountType.MANUAL, [])

    account = Account(ACCOUNT_ID, "Manual", AccountType.MANUAL, ())
    with pytest.raises(FrozenInstanceError):
        account.positions = (AssetPosition("BTC", Decimal("1")),)


@pytest.mark.parametrize(
    "symbol,amount",
    [
        ("btc", Decimal("1")),
        ("BTC", Decimal("0")),
        ("BTC", Decimal("-1")),
        ("BTC", Decimal("NaN")),
        ("BTC", 1.0),
    ],
)
def test_existing_asset_position_validation_remains_in_force(symbol: str, amount: object) -> None:
    with pytest.raises(DomainValidationError):
        Account(
            ACCOUNT_ID,
            "Manual",
            AccountType.MANUAL,
            (AssetPosition(symbol, amount),),
        )


def test_account_rejects_non_account_type() -> None:
    with pytest.raises(DomainValidationError, match="Invalid account type"):
        Account(ACCOUNT_ID, "Manual", "manual", ())
