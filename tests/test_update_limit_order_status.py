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
from advanced_trading_advisor.portfolio.update_limit_order_status import (
    UpdateLimitOrderStatus,
    UpdateLimitOrderStatusCommand,
)
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("5767cfc2-bf97-4915-86d6-88d4c3ad12e8")
NEW_SNAPSHOT_ID = UUID("01f56373-4978-44b3-aa18-34d023e19119")
MANUAL_ORDER_ID = UUID("a24aa63f-b4f9-4e56-ac10-310e1b6ce0ea")
BINANCE_ORDER_ID = UUID("f205224a-5e55-42a2-8f04-99b49ac6c50f")
OTHER_ORDER_ID = UUID("1762b763-02e5-4b15-ae02-aa7d4b48943c")
UNKNOWN_ORDER_ID = UUID("ad8594d7-6ab3-4d75-aa3c-3cab136acbf1")
BINANCE_ID = UUID("3453c1e7-b14c-4693-a237-a89571a8e0d7")
OTHER_ID = UUID("65c132e5-73ec-44b4-a93b-39895f1d842f")


def order(order_id: UUID, account_id: UUID, status: LimitOrderStatus) -> LimitOrder:
    return LimitOrder(
        order_id,
        "SOL",
        TradingSide.BUY,
        Decimal("101.12345678901234567890"),
        Decimal("1.234567890123456789"),
        status,
        NOW - timedelta(hours=1),
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
            order(MANUAL_ORDER_ID, DEFAULT_MANUAL_ACCOUNT_ID, LimitOrderStatus.OPEN),
            order(BINANCE_ORDER_ID, BINANCE_ID, LimitOrderStatus.OPEN),
            order(OTHER_ORDER_ID, OTHER_ID, LimitOrderStatus.CANCELLED),
        ),
        (CorePosition("ETH", Decimal("2"), Decimal("2.5")),),
    )


def command(**changes) -> UpdateLimitOrderStatusCommand:
    values = {
        "expected_revision": 1,
        "order_id": BINANCE_ORDER_ID,
        "target_status": LimitOrderStatus.CANCELLED,
    }
    return UpdateLimitOrderStatusCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "update-limit-order-status.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


@pytest.mark.parametrize(
    "target_status,update_type",
    [
        (LimitOrderStatus.CANCELLED, UpdateType.ORDER_CANCELLED),
        (LimitOrderStatus.EXPIRED, UpdateType.ORDER_EXPIRED),
    ],
)
def test_open_binance_order_resolves_without_other_state_changes(
    tmp_path, target_status, update_type
) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        at = NOW + timedelta(seconds=1)
        saved = await UpdateLimitOrderStatus(
            snapshots,
            now=lambda: at,
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command(target_status=target_status))

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        assert saved.snapshot.confirmed_at == at
        assert len(saved.snapshot.orders) == len(before.orders)
        assert saved.snapshot.orders[0] == before.orders[0]
        assert saved.snapshot.orders[2] == before.orders[2]
        old, updated = before.orders[1], saved.snapshot.orders[1]
        assert updated.id == old.id
        assert updated.account_id == old.account_id == BINANCE_ID
        assert updated.asset == old.asset
        assert updated.side is old.side
        assert updated.target_price.as_tuple() == old.target_price.as_tuple()
        assert updated.quantity_asset.as_tuple() == old.quantity_asset.as_tuple()
        assert updated.created_at == old.created_at
        assert updated.status is target_status
        assert updated.updated_at == updated.resolved_at == at
        assert saved.snapshot.portfolio == before.portfolio
        assert saved.snapshot.portfolio.accounts == before.portfolio.accounts
        assert saved.snapshot.portfolio.positions == before.portfolio.positions
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.core_positions == before.core_positions
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is update_type
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


def test_other_exchange_order_can_expire_without_manual_account_assumption(tmp_path) -> None:
    async def scenario():
        _, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        other_open = replace(before.orders[2], status=LimitOrderStatus.OPEN, resolved_at=None)
        before = replace(before, orders=before.orders[:2] + (other_open,))
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await UpdateLimitOrderStatus(
            snapshots, now=lambda: NOW + timedelta(seconds=1)
        ).execute(command(order_id=OTHER_ORDER_ID, target_status=LimitOrderStatus.EXPIRED))
        assert saved.snapshot.orders[2].account_id == OTHER_ID
        assert saved.snapshot.orders[2].status is LimitOrderStatus.EXPIRED
        assert saved.snapshot.portfolio == before.portfolio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"order_id": UNKNOWN_ORDER_ID}, "Limit order does not exist"),
        ({"target_status": LimitOrderStatus.FILLED}, "Only CANCELLED or EXPIRED"),
        ({"target_status": LimitOrderStatus.OPEN}, "Only CANCELLED or EXPIRED"),
        ({"target_status": "cancelled"}, "Only CANCELLED or EXPIRED"),
    ],
)
def test_unknown_or_unsupported_target_fails_without_persistence(tmp_path, changes, match) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(DomainValidationError, match=match):
            await UpdateLimitOrderStatus(snapshots).execute(command(**changes))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "source_status,target_status",
    [
        (LimitOrderStatus.CANCELLED, LimitOrderStatus.CANCELLED),
        (LimitOrderStatus.EXPIRED, LimitOrderStatus.EXPIRED),
        (LimitOrderStatus.CANCELLED, LimitOrderStatus.EXPIRED),
        (LimitOrderStatus.EXPIRED, LimitOrderStatus.CANCELLED),
    ],
)
def test_terminal_order_cannot_be_repeated_or_retargeted(
    tmp_path, source_status, target_status
) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        terminal = replace(before.orders[1], status=source_status, resolved_at=NOW)
        before = replace(before, orders=(before.orders[0], terminal, before.orders[2]))
        current = await ConfirmedStateService(snapshots).initialize(before)
        with pytest.raises(DomainValidationError, match="Only an OPEN order"):
            await UpdateLimitOrderStatus(snapshots).execute(command(target_status=target_status))
        assert await snapshots.get_current() == current
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_missing_current_portfolio_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(DomainValidationError, match="No confirmed portfolio"):
            await UpdateLimitOrderStatus(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_stale_revision_has_no_writes_or_retry(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(ConflictError, match="reload before confirming"):
            await UpdateLimitOrderStatus(snapshots).execute(command(expected_revision=2))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_command_requires_positive_integer_expected_revision(revision) -> None:
    with pytest.raises(DomainValidationError, match="Invalid expected revision"):
        command(expected_revision=revision)


def test_command_rejects_invalid_order_identity() -> None:
    with pytest.raises(DomainValidationError, match="non-nil UUID"):
        command(order_id=UUID(int=0))


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
            raise AssertionError("UpdateLimitOrderStatus must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("UpdateLimitOrderStatus must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await UpdateLimitOrderStatus(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())
        assert result.revision == 2
        assert result.snapshot.portfolio is repository.current.snapshot.portfolio
        assert repository.loads == 1
        assert len(repository.confirmations) == 1
        snapshot, expected_revision, update = repository.confirmations[0]
        assert expected_revision == 1
        assert update.update_type is UpdateType.ORDER_CANCELLED
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await UpdateLimitOrderStatus(
                repository, now=lambda: NOW + timedelta(seconds=1)
            ).execute(command())
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
