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
from advanced_trading_advisor.portfolio.create_account import CreateAccount, CreateAccountCommand
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
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("8cbe0654-9fe7-4d3a-9e56-42aa8f091ced")
NEW_SNAPSHOT_ID = UUID("d165491c-598e-4c7a-b22a-6bdd9f411798")
BINANCE_ID = UUID("3d6fdcb3-6f13-429d-af04-b5558b22942e")
NEW_ACCOUNT_ID = UUID("d046c969-7b7a-4e59-bd9c-65b540e81d67")
ORDER_ID = UUID("b257575c-b9b5-41db-846d-42ae2cd9eef9")


def initial_snapshot() -> ConfirmedPortfolioSnapshot:
    return ConfirmedPortfolioSnapshot(
        INITIAL_ID,
        NOW,
        Portfolio(
            (
                Account(
                    DEFAULT_MANUAL_ACCOUNT_ID,
                    "Wallet",
                    AccountType.MANUAL,
                    (
                        AssetPosition("ETH", Decimal("3.0")),
                        AssetPosition("USDC", Decimal("1000.00")),
                    ),
                ),
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (AssetPosition("BTC", Decimal("0.25")),),
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


def command(**changes) -> CreateAccountCommand:
    values = {
        "expected_revision": 1,
        "account_id": NEW_ACCOUNT_ID,
        "name": "Wallet",  # Names are labels; this intentionally duplicates an existing name.
        "account_type": AccountType.EXTERNAL_WALLET,
        "positions": (
            AssetPosition("ETH", Decimal("0.10000000000000000001")),
            AssetPosition("USDC", Decimal("10.200")),
        ),
    }
    return CreateAccountCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "create-account.sqlite")
    await database.initialize()
    snapshots = SQLiteSnapshotRepository(database)
    return database, snapshots


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


def test_create_account_confirms_one_revision_and_preserves_all_other_state(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await CreateAccount(
            snapshots,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        assert len(saved.snapshot.portfolio.accounts) == len(before.portfolio.accounts) + 1
        assert saved.snapshot.portfolio.accounts[:2] == before.portfolio.accounts
        created = saved.snapshot.portfolio.accounts[-1]
        assert created.id == NEW_ACCOUNT_ID
        assert created.name == "Wallet"
        assert created.type is AccountType.EXTERNAL_WALLET
        assert created.positions == command().positions
        assert created.positions[0].amount.as_tuple() == command().positions[0].amount.as_tuple()
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.core_positions == before.core_positions
        assert saved.snapshot.orders == before.orders
        assert saved.snapshot.orders[0].account_id == BINANCE_ID
        assert {p.symbol: p.amount for p in saved.snapshot.portfolio.positions} == {
            "BTC": Decimal("0.25"),
            "ETH": Decimal("3.10000000000000000001"),
            "USDC": Decimal("1010.200"),
        }
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.ACCOUNT_CREATED
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,error_type",
    [
        ({"account_id": BINANCE_ID}, StateConflictError),
        ({"name": " \t "}, DomainValidationError),
        (
            {
                "positions": (
                    AssetPosition("ETH", Decimal("1")),
                    AssetPosition("ETH", Decimal("2")),
                )
            },
            DomainValidationError,
        ),
        ({"account_type": "binance"}, DomainValidationError),
    ],
)
def test_domain_rejection_creates_no_confirmed_state(tmp_path, changes, error_type) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(error_type):
            await CreateAccount(snapshots).execute(command(**changes))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_asset_position_decimal_validation_is_reused() -> None:
    with pytest.raises(DomainValidationError):
        command(positions=(AssetPosition("BTC", Decimal("0")),))


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_command_requires_a_positive_integer_expected_revision(revision) -> None:
    with pytest.raises(DomainValidationError, match="Invalid expected revision"):
        command(expected_revision=revision)


def test_stale_revision_has_no_writes_or_automatic_retry(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(RevisionConflictError):
            await CreateAccount(snapshots).execute(command(expected_revision=2))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_no_current_state_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(ResourceNotFoundError):
            await CreateAccount(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_application_uses_only_existing_confirmed_state_boundary() -> None:
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
            raise AssertionError("CreateAccount must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("CreateAccount must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await CreateAccount(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())
        assert result.revision == 2
        assert repository.loads == 1
        assert len(repository.confirmations) == 1
        snapshot, expected_revision, update = repository.confirmations[0]
        assert expected_revision == 1
        assert update.update_type is UpdateType.ACCOUNT_CREATED
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.current = CurrentPortfolio(initial_snapshot(), 2)
        with pytest.raises(ConflictError):
            await CreateAccount(repository).execute(command())
        assert repository.loads == 1
        assert repository.confirmations == []

        repository = RepositorySpy()
        repository.conflict = True  # Another writer wins after this use case reads revision 1.
        with pytest.raises(ConflictError):
            await CreateAccount(repository, now=lambda: NOW + timedelta(seconds=1)).execute(
                command()
            )
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
