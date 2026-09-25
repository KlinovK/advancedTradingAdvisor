import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    CurrentPortfolio,
    RevisionConflictError,
    StateConflictError,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import DomainValidationError, ResourceNotFoundError
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import (
    DEFAULT_MANUAL_ACCOUNT_ID,
    Account,
    AccountType,
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    CorePosition,
    FinancialSettings,
    LimitOrder,
    LimitOrderStatus,
    Portfolio,
    TradingSide,
)
from advanced_trading_advisor.portfolio.update_account_holdings import (
    UpdateAccountHoldings,
    UpdateAccountHoldingsCommand,
)
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("bfb467f6-bd66-4b31-a731-165c783054c5")
NEW_SNAPSHOT_ID = UUID("0594c526-bd3c-4d66-9b17-a0394b61e905")
BINANCE_ID = UUID("041e9ee7-cd25-4c46-99c8-1b6f1da039d9")
WALLET_ID = UUID("0598caa9-cf0d-4283-a73d-ed48379c849d")
UNKNOWN_ID = UUID("40209f0e-75d7-469f-94a2-a979185c9685")
ORDER_ID = UUID("06f090d9-b13b-48b7-85d4-e0922b7288e4")


def initial_snapshot() -> ConfirmedPortfolioSnapshot:
    return ConfirmedPortfolioSnapshot(
        INITIAL_ID,
        NOW,
        Portfolio(
            (
                Account(
                    DEFAULT_MANUAL_ACCOUNT_ID,
                    "Manual Portfolio",
                    AccountType.MANUAL,
                    (
                        AssetPosition("ETH", Decimal("2.0")),
                        AssetPosition("USDC", Decimal("100.00")),
                    ),
                ),
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (
                        AssetPosition("BTC", Decimal("0.020000000000000001")),
                        AssetPosition("ETH", Decimal("1.5")),
                        AssetPosition("SOL", Decimal("3")),
                        AssetPosition("USDT", Decimal("1000.00")),
                    ),
                ),
                Account(
                    WALLET_ID,
                    "External Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (AssetPosition("BTC", Decimal("0.1")),),
                ),
            )
        ),
        FinancialSettings(Decimal("250.00"), Decimal("12")),
        (
            LimitOrder(
                ORDER_ID,
                "SOL",
                TradingSide.SELL,
                Decimal("100"),
                Decimal("1"),
                LimitOrderStatus.OPEN,
                NOW,
                NOW,
                account_id=BINANCE_ID,
            ),
        ),
        (CorePosition("ETH", Decimal("3"), Decimal("3.25")),),
    )


def replacement_positions() -> tuple[AssetPosition, ...]:
    return (
        AssetPosition("BTC", Decimal("0.0250000000000000000001")),
        AssetPosition("ETH", Decimal("1.0")),
        AssetPosition("USDT", Decimal("700.00")),
        AssetPosition("USDC", Decimal("20.200")),
    )


def command(**changes) -> UpdateAccountHoldingsCommand:
    values = {
        "expected_revision": 1,
        "account_id": BINANCE_ID,
        "positions": replacement_positions(),
    }
    return UpdateAccountHoldingsCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "update-account-holdings.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


def test_complete_replacement_preserves_other_state_and_writes_audit(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await UpdateAccountHoldings(
            snapshots,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        accounts = saved.snapshot.portfolio.accounts
        assert [account.id for account in accounts] == [
            DEFAULT_MANUAL_ACCOUNT_ID,
            BINANCE_ID,
            WALLET_ID,
        ]
        assert accounts[0] == before.portfolio.accounts[0]
        assert accounts[2] == before.portfolio.accounts[2]
        assert accounts[1].id == BINANCE_ID
        assert accounts[1].name == "Binance"
        assert accounts[1].type is AccountType.BINANCE
        assert accounts[1].positions == replacement_positions()
        assert accounts[1].positions[0].amount.as_tuple() == (
            replacement_positions()[0].amount.as_tuple()
        )
        assert "SOL" not in {position.symbol for position in accounts[1].positions}
        assert accounts[1].positions[-1].symbol == "USDC"
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.core_positions == before.core_positions
        assert saved.snapshot.orders == before.orders
        assert saved.snapshot.orders[0].account_id == BINANCE_ID
        assert {
            position.symbol: position.amount for position in saved.snapshot.portfolio.positions
        } == {
            "BTC": Decimal("0.1250000000000000000001"),
            "ETH": Decimal("3.0"),
            "USDC": Decimal("120.200"),
            "USDT": Decimal("700.00"),
        }
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.ACCOUNT_HOLDINGS_UPDATED
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


def test_empty_holdings_are_valid_for_an_account_without_core_inventory(tmp_path) -> None:
    async def scenario():
        _, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        saved = await UpdateAccountHoldings(
            snapshots, now=lambda: NOW + timedelta(seconds=1)
        ).execute(command(account_id=WALLET_ID, positions=()))
        assert saved.revision == before.revision + 1
        assert saved.snapshot.portfolio.accounts[2].positions == ()
        assert saved.snapshot.portfolio.accounts[:2] == before.snapshot.portfolio.accounts[:2]
        assert saved.snapshot.core_positions == before.snapshot.core_positions

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,error_type",
    [
        ({"account_id": UNKNOWN_ID}, ResourceNotFoundError),
        (
            {
                "positions": (
                    AssetPosition("BTC", Decimal("1")),
                    AssetPosition("BTC", Decimal("2")),
                )
            },
            DomainValidationError,
        ),
        ({"positions": initial_snapshot().portfolio.accounts[1].positions}, StateConflictError),
        (
            {"positions": (AssetPosition("ETH", Decimal("0.9")),)},
            DomainValidationError,
        ),
    ],
)
def test_rejected_update_creates_no_snapshot_audit_or_pointer_movement(
    tmp_path, changes, error_type
) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(error_type):
            await UpdateAccountHoldings(snapshots).execute(command(**changes))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_invalid_asset_position_uses_existing_decimal_rules() -> None:
    with pytest.raises(DomainValidationError):
        command(positions=(AssetPosition("BTC", Decimal("0")),))
    with pytest.raises(DomainValidationError):
        command(positions=(AssetPosition("BTC", 0.25),))


def test_missing_current_state_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(ResourceNotFoundError):
            await UpdateAccountHoldings(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_stale_revision_has_no_writes_or_retry(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(RevisionConflictError):
            await UpdateAccountHoldings(snapshots).execute(command(expected_revision=2))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_command_requires_a_positive_integer_expected_revision(revision) -> None:
    with pytest.raises(DomainValidationError, match="Invalid expected revision"):
        command(expected_revision=revision)


def test_command_rejects_invalid_target_identity() -> None:
    with pytest.raises(DomainValidationError, match="non-nil UUID"):
        command(account_id=UUID(int=0))


def test_only_confirmed_state_boundary_is_used_and_cas_conflict_is_not_retried() -> None:
    class RepositorySpy:
        def __init__(self):
            self.current = CurrentPortfolio(initial_snapshot(), 1)
            self.loads = 0
            self.confirmations = []
            self.conflict = False

        async def get_current(self):
            self.loads += 1
            return self.current

        async def set_current(self, snapshot, *, expected_revision, update):
            self.confirmations.append((snapshot, expected_revision, update))
            if self.conflict:
                raise ConflictError("Current state changed; reload before confirming.")
            return CurrentPortfolio(snapshot, expected_revision + 1)

        async def save(self, _snapshot):
            raise AssertionError("UpdateAccountHoldings must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("UpdateAccountHoldings must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await UpdateAccountHoldings(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())
        assert result.revision == 2
        assert repository.loads == 1
        assert len(repository.confirmations) == 1
        snapshot, expected_revision, update = repository.confirmations[0]
        assert expected_revision == 1
        assert update.update_type is UpdateType.ACCOUNT_HOLDINGS_UPDATED
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await UpdateAccountHoldings(repository, now=lambda: NOW + timedelta(seconds=1)).execute(
                command()
            )
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
