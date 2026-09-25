import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.persistence import ConflictError, CurrentPortfolio, UpdateType
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.portfolio.add_limit_order import AddLimitOrder, AddLimitOrderCommand
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
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("7bb7973b-2461-4e84-83d7-422a6e029c33")
NEW_SNAPSHOT_ID = UUID("fe777977-cc9e-4ab9-9788-f245aacaa4d0")
NEW_ORDER_ID = UUID("261cbb59-14c5-43fa-8d96-086711d13981")
EXISTING_ORDER_ID = UUID("76a399fc-e36e-46b1-bcc0-080a8be1e552")
BINANCE_ID = UUID("4d2c1602-9cd0-4355-a842-60e8a6afcdef")
OTHER_ID = UUID("bf12bb42-8a32-45d6-a642-41c129ff304c")
UNKNOWN_ID = UUID("e0522d21-810a-4937-a434-18a4d756c9c3")


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
                        AssetPosition("ETH", Decimal("3.0")),
                        AssetPosition("USDC", Decimal("1000.00")),
                    ),
                ),
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (AssetPosition("BTC", Decimal("0.025000000000000001")),),
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
            LimitOrder(
                EXISTING_ORDER_ID,
                "SOL",
                TradingSide.SELL,
                Decimal("120"),
                Decimal("0.5"),
                LimitOrderStatus.OPEN,
                NOW,
                NOW,
                account_id=OTHER_ID,
            ),
        ),
        (CorePosition("ETH", Decimal("2"), Decimal("2.5")),),
    )


def command(**changes) -> AddLimitOrderCommand:
    values = {
        "expected_revision": 1,
        "account_id": BINANCE_ID,
        "asset": "SOL",
        "side": TradingSide.BUY,
        "target_price": Decimal("101.12345678901234567890"),
        "quantity_asset": Decimal("1.234567890123456789"),
    }
    return AddLimitOrderCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "add-limit-order.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


def test_add_one_binance_order_preserves_state_and_writes_audit(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await AddLimitOrder(
            snapshots,
            now=lambda: NOW + timedelta(seconds=1),
            order_identity_factory=lambda: NEW_ORDER_ID,
            snapshot_identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        assert len(saved.snapshot.orders) == len(before.orders) + 1
        assert saved.snapshot.orders[:-1] == before.orders
        added = saved.snapshot.orders[-1]
        assert added.id == NEW_ORDER_ID
        assert added.account_id == BINANCE_ID
        assert added.asset == "SOL"
        assert added.side is TradingSide.BUY
        assert added.target_price.as_tuple() == command().target_price.as_tuple()
        assert added.quantity_asset.as_tuple() == command().quantity_asset.as_tuple()
        assert added.status is LimitOrderStatus.OPEN
        assert added.created_at == added.updated_at == saved.snapshot.confirmed_at
        assert added.resolved_at is None
        assert saved.snapshot.portfolio == before.portfolio
        assert saved.snapshot.portfolio.accounts == before.portfolio.accounts
        assert [account.id for account in saved.snapshot.portfolio.accounts] == [
            DEFAULT_MANUAL_ACCOUNT_ID,
            BINANCE_ID,
            OTHER_ID,
        ]
        assert saved.snapshot.portfolio.positions == before.portfolio.positions
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.core_positions == before.core_positions
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.ORDER_ADDED
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


def test_other_exchange_target_is_explicit_and_does_not_debit_inventory(tmp_path) -> None:
    async def scenario():
        _, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        saved = await AddLimitOrder(snapshots, now=lambda: NOW + timedelta(seconds=1)).execute(
            command(account_id=OTHER_ID, asset="BTC", side=TradingSide.SELL)
        )
        assert saved.snapshot.orders[-1].account_id == OTHER_ID
        assert saved.snapshot.orders[-1].side is TradingSide.SELL
        assert saved.snapshot.portfolio == before.snapshot.portfolio
        assert saved.snapshot.portfolio.accounts[2].positions == (
            AssetPosition("SOL", Decimal("2.0")),
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"account_id": UNKNOWN_ID}, "Order account must exist"),
        ({"asset": "XRP"}, "Unsupported asset"),
        ({"asset": "sol"}, "symbol"),
        ({"side": "buy"}, "Invalid trading side"),
        ({"target_price": Decimal("0")}, "positive"),
        ({"target_price": 1.5}, "Decimal"),
        ({"quantity_asset": Decimal("-1")}, "positive"),
        ({"quantity_asset": Decimal("NaN")}, "finite"),
    ],
)
def test_invalid_order_input_does_not_persist(tmp_path, changes, match) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(DomainValidationError, match=match):
            await AddLimitOrder(snapshots, now=lambda: NOW + timedelta(seconds=1)).execute(
                command(**changes)
            )
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_duplicate_order_identity_is_rejected_without_writes(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(DomainValidationError, match="Order IDs must be unique"):
            await AddLimitOrder(
                snapshots,
                now=lambda: NOW + timedelta(seconds=1),
                order_identity_factory=lambda: EXISTING_ORDER_ID,
            ).execute(command())
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_missing_current_portfolio_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(DomainValidationError, match="No confirmed portfolio"):
            await AddLimitOrder(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_stale_revision_has_no_writes_or_retry(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(ConflictError, match="reload before confirming"):
            await AddLimitOrder(snapshots).execute(command(expected_revision=2))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_command_requires_a_positive_integer_expected_revision(revision) -> None:
    with pytest.raises(DomainValidationError, match="Invalid expected revision"):
        command(expected_revision=revision)


def test_command_rejects_invalid_account_identity() -> None:
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
            raise AssertionError("AddLimitOrder must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("AddLimitOrder must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await AddLimitOrder(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            order_identity_factory=lambda: NEW_ORDER_ID,
            snapshot_identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())
        assert result.revision == 2
        assert result.snapshot.portfolio is repository.current.snapshot.portfolio
        assert repository.loads == 1
        assert len(repository.confirmations) == 1
        snapshot, expected_revision, update = repository.confirmations[0]
        assert expected_revision == 1
        assert update.update_type is UpdateType.ORDER_ADDED
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await AddLimitOrder(repository, now=lambda: NOW + timedelta(seconds=1)).execute(
                command()
            )
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
