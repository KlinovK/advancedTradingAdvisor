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
from advanced_trading_advisor.portfolio.rename_account import RenameAccount, RenameAccountCommand
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("8675ff16-42ec-4d14-bf40-3ad0d25d2ed1")
NEW_SNAPSHOT_ID = UUID("15546209-808d-4fba-b574-f665e6e1f64a")
BINANCE_ID = UUID("394e8daa-42e9-4b09-9e6c-f63931c7f85a")
UNKNOWN_ID = UUID("bf8315bd-56ef-40b5-9683-5fd325598918")
ORDER_ID = UUID("18bd218a-9a78-4107-8371-73046a451534")


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
                        AssetPosition("ETH", Decimal("3.00000000000000000001")),
                        AssetPosition("USDC", Decimal("1000.00")),
                    ),
                ),
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (AssetPosition("BTC", Decimal("0.250000000000000001")),),
                ),
            )
        ),
        FinancialSettings(Decimal("250.00"), Decimal("12")),
        (
            LimitOrder(
                ORDER_ID,
                "SOL",
                TradingSide.BUY,
                Decimal("90"),
                Decimal("1.5"),
                LimitOrderStatus.OPEN,
                NOW,
                NOW,
                account_id=BINANCE_ID,
            ),
        ),
        (CorePosition("ETH", Decimal("2"), Decimal("2.5")),),
    )


def command(**changes) -> RenameAccountCommand:
    values = {"expected_revision": 1, "account_id": BINANCE_ID, "name": "Long-Term Exchange"}
    return RenameAccountCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "rename-account.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


def test_rename_preserves_identity_holdings_ordering_and_unrelated_state(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await RenameAccount(
            snapshots,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        accounts = saved.snapshot.portfolio.accounts
        assert [account.id for account in accounts] == [DEFAULT_MANUAL_ACCOUNT_ID, BINANCE_ID]
        assert accounts[0] == before.portfolio.accounts[0]
        assert accounts[1].id == before.portfolio.accounts[1].id
        assert accounts[1].name == "Long-Term Exchange"
        assert accounts[1].type is AccountType.BINANCE
        assert accounts[1].positions == before.portfolio.accounts[1].positions
        assert accounts[1].positions[0].amount.as_tuple() == (
            before.portfolio.accounts[1].positions[0].amount.as_tuple()
        )
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.core_positions == before.core_positions
        assert saved.snapshot.orders == before.orders
        assert saved.snapshot.orders[0].account_id == BINANCE_ID
        assert saved.snapshot.portfolio.positions == before.portfolio.positions
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.ACCOUNT_RENAMED
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ["Manual Portfolio", " Binance "])
def test_duplicate_names_and_exact_label_text_are_allowed(tmp_path, name) -> None:
    async def scenario():
        _, snapshots = await disposable_storage(tmp_path)
        await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        saved = await RenameAccount(snapshots, now=lambda: NOW + timedelta(seconds=1)).execute(
            command(name=name)
        )
        assert saved.snapshot.portfolio.accounts[1].name == name
        assert saved.revision == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,error_type",
    [
        ({"name": ""}, DomainValidationError),
        ({"name": " \t "}, DomainValidationError),
        ({"account_id": UNKNOWN_ID}, ResourceNotFoundError),
        ({"name": "Binance"}, StateConflictError),
    ],
)
def test_invalid_or_noop_rename_creates_no_revision(tmp_path, changes, error_type) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(error_type):
            await RenameAccount(snapshots).execute(command(**changes))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_missing_current_state_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(ResourceNotFoundError):
            await RenameAccount(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_stale_revision_has_no_snapshot_audit_or_pointer_movement(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(RevisionConflictError):
            await RenameAccount(snapshots).execute(command(expected_revision=2))
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
            raise AssertionError("RenameAccount must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("RenameAccount must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await RenameAccount(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())
        assert result.revision == 2
        assert repository.loads == 1
        assert len(repository.confirmations) == 1
        snapshot, expected_revision, update = repository.confirmations[0]
        assert expected_revision == 1
        assert update.update_type is UpdateType.ACCOUNT_RENAMED
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await RenameAccount(repository, now=lambda: NOW + timedelta(seconds=1)).execute(
                command()
            )
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
