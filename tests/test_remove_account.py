import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.persistence import ConflictError, CurrentPortfolio, UpdateType
from advanced_trading_advisor.domain.validation import DomainValidationError
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
from advanced_trading_advisor.portfolio.remove_account import RemoveAccount, RemoveAccountCommand
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("6ee02640-19ed-4b0c-8658-744ed706e84e")
NEW_SNAPSHOT_ID = UUID("ecb4d825-7437-46a8-b492-a925eb13b0e7")
BINANCE_ID = UUID("bf38d2cd-512b-4956-b059-e67975992a7a")
WALLET_ID = UUID("6c0b904f-15bb-4924-9c64-a2760654143c")
OTHER_ID = UUID("c74828aa-6b0c-4178-a686-7bb5064064b5")
UNKNOWN_ID = UUID("a9b3378f-7f9f-4b38-8b14-88315698a807")
BINANCE_ORDER_ID = UUID("1efbd5c8-99f9-42ca-8f60-3817c2d04838")
OTHER_ORDER_ID = UUID("43542583-a3a1-44f7-9739-f4382b124e44")
WALLET_ORDER_ID = UUID("c483d6e8-96e1-428b-b31b-bf3b6689933b")


def order(order_id: UUID, account_id: UUID, status: LimitOrderStatus) -> LimitOrder:
    return LimitOrder(
        order_id,
        "SOL",
        TradingSide.BUY,
        Decimal("90"),
        Decimal("1"),
        status,
        NOW,
        NOW,
        None if status is LimitOrderStatus.OPEN else NOW,
        account_id=account_id,
    )


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
                    (AssetPosition("ETH", Decimal("2.0")),),
                ),
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (AssetPosition("ETH", Decimal("1.0")),),
                ),
                Account(
                    WALLET_ID,
                    "External Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (
                        AssetPosition("ETH", Decimal("0.5")),
                        AssetPosition("BTC", Decimal("0.05")),
                    ),
                ),
                Account(
                    OTHER_ID,
                    "Other Exchange",
                    AccountType.OTHER_EXCHANGE,
                    (AssetPosition("SOL", Decimal("2.0")),),
                ),
            )
        ),
        FinancialSettings(Decimal("250.00"), Decimal("12")),
        (
            order(BINANCE_ORDER_ID, BINANCE_ID, LimitOrderStatus.OPEN),
            order(OTHER_ORDER_ID, OTHER_ID, LimitOrderStatus.CANCELLED),
        ),
        (CorePosition("ETH", Decimal("3.0"), Decimal("3.25")),),
    )


def command(**changes) -> RemoveAccountCommand:
    values = {"expected_revision": 1, "account_id": WALLET_ID}
    return RemoveAccountCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "remove-account.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


def test_remove_one_account_preserves_remaining_state_and_history(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await RemoveAccount(
            snapshots,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        assert saved.snapshot.portfolio.accounts == (
            before.portfolio.accounts[0],
            before.portfolio.accounts[1],
            before.portfolio.accounts[3],
        )
        assert [account.id for account in saved.snapshot.portfolio.accounts] == [
            DEFAULT_MANUAL_ACCOUNT_ID,
            BINANCE_ID,
            OTHER_ID,
        ]
        assert {
            position.symbol: position.amount for position in saved.snapshot.portfolio.positions
        } == {
            "ETH": Decimal("3.0"),
            "SOL": Decimal("2.0"),
        }
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.core_positions == before.core_positions
        assert saved.snapshot.orders == before.orders
        assert await snapshots.get_current() == saved
        assert await snapshots.get(before.id) == before  # Historical account data is not deleted.
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.ACCOUNT_REMOVED
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [LimitOrderStatus.OPEN, LimitOrderStatus.CANCELLED])
def test_any_referenced_order_blocks_removal_without_changing_orders(tmp_path, status) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        original = initial_snapshot()
        original = replace(
            original,
            orders=original.orders + (order(WALLET_ORDER_ID, WALLET_ID, status),),
        )
        before = await ConfirmedStateService(snapshots).initialize(original)
        with pytest.raises(DomainValidationError, match="referenced orders"):
            await RemoveAccount(snapshots).execute(command())
        assert await snapshots.get_current() == before
        assert (await snapshots.get_current()).snapshot.orders == original.orders
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_cross_account_core_floor_blocks_removal(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        original = initial_snapshot()
        original = replace(
            original,
            orders=(original.orders[1],),  # No Binance order obscures the core-floor check.
        )
        before = await ConfirmedStateService(snapshots).initialize(original)
        with pytest.raises(DomainValidationError, match="Confirmed core floor exceeds"):
            await RemoveAccount(snapshots).execute(command(account_id=BINANCE_ID))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_last_account_cannot_be_removed(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        original = initial_snapshot()
        original = replace(
            original,
            portfolio=Portfolio((original.portfolio.accounts[0],)),
            orders=(),
            core_positions=(),
        )
        before = await ConfirmedStateService(snapshots).initialize(original)
        with pytest.raises(DomainValidationError, match="last account"):
            await RemoveAccount(snapshots).execute(command(account_id=DEFAULT_MANUAL_ACCOUNT_ID))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_unknown_account_cannot_be_removed(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(DomainValidationError, match="Account does not exist"):
            await RemoveAccount(snapshots).execute(command(account_id=UNKNOWN_ID))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_no_current_portfolio_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(DomainValidationError, match="No confirmed portfolio"):
            await RemoveAccount(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_stale_revision_does_not_create_snapshot_audit_or_pointer_move(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(ConflictError, match="reload before confirming"):
            await RemoveAccount(snapshots).execute(command(expected_revision=2))
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
            raise AssertionError("RemoveAccount must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("RemoveAccount must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await RemoveAccount(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())
        assert result.revision == 2
        assert repository.loads == 1
        assert len(repository.confirmations) == 1
        snapshot, expected_revision, update = repository.confirmations[0]
        assert expected_revision == 1
        assert update.update_type is UpdateType.ACCOUNT_REMOVED
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await RemoveAccount(repository, now=lambda: NOW + timedelta(seconds=1)).execute(
                command()
            )
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
