from dataclasses import FrozenInstanceError
from decimal import Context, Decimal, localcontext
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.portfolio.models import (
    Account,
    AccountType,
    AssetPosition,
    Portfolio,
)

BINANCE_ID = UUID("e2c87e22-b58d-4e0c-a5f6-94797af39dd9")
WALLET_ID = UUID("938d3971-9cfb-4942-a329-22d48c86f97a")


def account(
    account_id: UUID,
    name: str,
    account_type: AccountType,
    *positions: tuple[str, str],
) -> Account:
    return Account(
        account_id,
        name,
        account_type,
        tuple(AssetPosition(symbol, Decimal(amount)) for symbol, amount in positions),
    )


def test_one_account_one_position() -> None:
    binance = account(BINANCE_ID, "Binance", AccountType.BINANCE, ("ETH", "0.8"))
    portfolio = Portfolio((binance,))

    assert portfolio.accounts == (binance,)
    assert portfolio.positions == (AssetPosition("ETH", Decimal("0.8")),)


def test_one_account_multiple_positions_are_sorted() -> None:
    portfolio = Portfolio(
        (account(BINANCE_ID, "Binance", AccountType.BINANCE, ("SOL", "4"), ("ETH", "0.8")),)
    )

    assert tuple(position.symbol for position in portfolio.positions) == ("ETH", "SOL")


def test_multiple_accounts_with_different_symbols_are_aggregated() -> None:
    portfolio = Portfolio(
        (
            account(BINANCE_ID, "Binance", AccountType.BINANCE, ("SOL", "4")),
            account(WALLET_ID, "Wallet", AccountType.EXTERNAL_WALLET, ("BTC", "0.02")),
        )
    )

    assert portfolio.positions == (
        AssetPosition("BTC", Decimal("0.02")),
        AssetPosition("SOL", Decimal("4")),
    )


def test_same_symbol_across_accounts_sums_exactly_in_symbol_order() -> None:
    portfolio = Portfolio(
        (
            account(BINANCE_ID, "Binance", AccountType.BINANCE, ("ETH", "0.8"), ("SOL", "4")),
            account(
                WALLET_ID,
                "Wallet",
                AccountType.EXTERNAL_WALLET,
                ("ETH", "2.7"),
                ("BTC", "0.02"),
            ),
        )
    )

    assert portfolio.positions == (
        AssetPosition("BTC", Decimal("0.02")),
        AssetPosition("ETH", Decimal("3.5")),
        AssetPosition("SOL", Decimal("4")),
    )


def test_decimal_aggregation_does_not_round_under_low_caller_precision() -> None:
    portfolio = Portfolio(
        (
            account(
                BINANCE_ID,
                "Binance",
                AccountType.BINANCE,
                ("ETH", "1.000000000000000000000000000001"),
            ),
            account(
                WALLET_ID,
                "Wallet",
                AccountType.EXTERNAL_WALLET,
                ("ETH", "0.000000000000000000000000000009"),
            ),
        )
    )

    with localcontext(Context(prec=4)):
        result = portfolio.positions

    assert result == (AssetPosition("ETH", Decimal("1.000000000000000000000000000010")),)


def test_duplicate_account_ids_are_rejected_even_with_distinct_names() -> None:
    with pytest.raises(DomainValidationError, match="Account IDs must be unique"):
        Portfolio(
            (
                account(BINANCE_ID, "Binance", AccountType.BINANCE),
                account(BINANCE_ID, "Second", AccountType.MANUAL),
            )
        )


def test_duplicate_account_names_and_cross_account_symbols_are_allowed() -> None:
    portfolio = Portfolio(
        (
            account(BINANCE_ID, "Wallet", AccountType.EXTERNAL_WALLET, ("ETH", "1")),
            account(WALLET_ID, "Wallet", AccountType.EXTERNAL_WALLET, ("ETH", "2")),
        )
    )

    assert portfolio.accounts[0].name == portfolio.accounts[1].name
    assert portfolio.positions == (AssetPosition("ETH", Decimal("3")),)


def test_projection_does_not_mutate_account_source_data() -> None:
    binance = account(BINANCE_ID, "Binance", AccountType.BINANCE, ("ETH", "0.8"))
    wallet = account(WALLET_ID, "Wallet", AccountType.EXTERNAL_WALLET, ("ETH", "2.7"))
    original_positions = (binance.positions, wallet.positions)
    portfolio = Portfolio((binance, wallet))

    first_projection = portfolio.positions
    second_projection = portfolio.positions

    assert (binance.positions, wallet.positions) == original_positions
    assert portfolio.accounts == (binance, wallet)
    assert first_projection == second_projection
    assert first_projection[0] is not binance.positions[0]
    with pytest.raises(FrozenInstanceError):
        first_projection[0].amount = Decimal("99")
    assert binance.positions[0].amount == Decimal("0.8")


def test_empty_portfolio_remains_valid() -> None:
    portfolio = Portfolio(())

    assert portfolio.accounts == ()
    assert portfolio.positions == ()


def test_accounts_must_be_an_immutable_tuple_of_accounts() -> None:
    binance = account(BINANCE_ID, "Binance", AccountType.BINANCE)
    with pytest.raises(DomainValidationError, match="Collection must be a tuple"):
        Portfolio([binance])
    with pytest.raises(DomainValidationError, match="Collection must be a tuple"):
        Portfolio(("Binance",))
    with pytest.raises(FrozenInstanceError):
        Portfolio((binance,)).accounts = ()


def test_temporary_aggregate_constructor_bridge_uses_one_manual_account() -> None:
    existing = Portfolio((AssetPosition("BTC", Decimal("0.02")),))

    assert len(existing.accounts) == 1
    assert existing.accounts[0].type is AccountType.MANUAL
    assert existing.accounts[0].positions == existing.positions
    assert existing == Portfolio((AssetPosition("BTC", Decimal("0.02")),))
