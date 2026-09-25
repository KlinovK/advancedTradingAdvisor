import asyncio
from dataclasses import replace
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
from advanced_trading_advisor.portfolio.confirm_limit_order_filled import (
    ConfirmLimitOrderFilled,
    ConfirmLimitOrderFilledCommand,
)
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
CONFIRMED_AT = NOW + timedelta(seconds=1)
INITIAL_ID = UUID("56e369e0-cd2e-4dcb-b3e1-545ed7137041")
NEW_ID = UUID("0b945690-f5e9-4b14-9f38-1f45ba0d7385")
BINANCE_ID = UUID("1631ad07-02f3-4361-9200-c5f0d127dd3b")
EXTERNAL_ID = UUID("fbc5f7c4-bb73-471a-b021-a065c39a79db")
BUY_ID = UUID("cd53dbf8-3d38-43ef-bb28-cc2f3bfa063a")
SELL_ID = UUID("891ce058-dd67-4330-aa4e-88a696b33f37")
OTHER_ID = UUID("a391728b-f967-4f60-a6db-e12267fc2bf8")
UNKNOWN_ID = UUID("e1723d30-f2aa-48ab-9909-6edc43a24b6c")


def position(symbol: str, amount: str) -> AssetPosition:
    return AssetPosition(symbol, Decimal(amount))


def snapshot() -> ConfirmedPortfolioSnapshot:
    return ConfirmedPortfolioSnapshot(
        INITIAL_ID,
        NOW,
        Portfolio(
            (
                Account(
                    DEFAULT_MANUAL_ACCOUNT_ID,
                    "Manual",
                    AccountType.MANUAL,
                    (position("ETH", "3"), position("USDC", "1000")),
                ),
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (
                        position("BTC", "1"),
                        position("USDT", "1000"),
                        position("USDC", "200"),
                        position("SOL", "2"),
                        position("BNB", "1"),
                    ),
                ),
                Account(
                    EXTERNAL_ID,
                    "Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (position("SOL", "4"),),
                ),
            )
        ),
        FinancialSettings(Decimal("250"), Decimal("12")),
        (
            LimitOrder(
                BUY_ID,
                "BTC",
                TradingSide.BUY,
                Decimal("100"),
                Decimal("0.2"),
                LimitOrderStatus.OPEN,
                NOW - timedelta(hours=1),
                NOW,
                account_id=BINANCE_ID,
            ),
            LimitOrder(
                SELL_ID,
                "ETH",
                TradingSide.SELL,
                Decimal("110"),
                Decimal("1"),
                LimitOrderStatus.OPEN,
                NOW - timedelta(hours=1),
                NOW,
                account_id=DEFAULT_MANUAL_ACCOUNT_ID,
            ),
            LimitOrder(
                OTHER_ID,
                "SOL",
                TradingSide.SELL,
                Decimal("100"),
                Decimal("1"),
                LimitOrderStatus.OPEN,
                NOW - timedelta(hours=1),
                NOW,
                account_id=BINANCE_ID,
            ),
        ),
        (CorePosition("ETH", Decimal("2"), Decimal("2.5")),),
    )


def command(**changes) -> ConfirmLimitOrderFilledCommand:
    values = {
        "expected_revision": 1,
        "order_id": BUY_ID,
        "settlement_asset": "USDT",
        "positions": (
            position("BTC", "1.2"),
            position("USDT", "975"),
            position("USDC", "200"),
            position("SOL", "2"),
            position("BNB", "0.9"),
        ),
    }
    return ConfirmLimitOrderFilledCommand(**(values | changes))


async def storage(tmp_path):
    database = Database(tmp_path / "confirm-limit-order-filled.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


@pytest.mark.parametrize("settlement_asset", ["USDT", "USDC"])
def test_buy_reconciles_only_order_account_and_writes_one_audit(tmp_path, settlement_asset) -> None:
    async def scenario():
        database, snapshots = await storage(tmp_path)
        before = snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        reconciled = (
            position("BTC", "1.2"),
            position("USDT", "975" if settlement_asset == "USDT" else "1000"),
            position("USDC", "175" if settlement_asset == "USDC" else "200"),
            position("SOL", "2"),
            position("BNB", "0.9"),
        )
        saved = await ConfirmLimitOrderFilled(
            snapshots, now=lambda: CONFIRMED_AT, identity_factory=lambda: NEW_ID
        ).execute(command(settlement_asset=settlement_asset, positions=reconciled))

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_ID
        assert saved.snapshot.confirmed_at == CONFIRMED_AT
        assert saved.snapshot.portfolio.accounts[1].positions == reconciled
        assert saved.snapshot.portfolio.accounts[1].id == BINANCE_ID
        assert saved.snapshot.portfolio.accounts[1].name == before.portfolio.accounts[1].name
        assert saved.snapshot.portfolio.accounts[1].type is AccountType.BINANCE
        assert saved.snapshot.portfolio.accounts[0] == before.portfolio.accounts[0]
        assert saved.snapshot.portfolio.accounts[2] == before.portfolio.accounts[2]
        assert tuple(a.id for a in saved.snapshot.portfolio.accounts) == tuple(
            a.id for a in before.portfolio.accounts
        )
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.core_positions == before.core_positions
        assert saved.snapshot.orders[1:] == before.orders[1:]
        filled = saved.snapshot.orders[0]
        old = before.orders[0]
        assert filled.id == old.id
        assert filled.account_id == old.account_id
        assert filled.asset == old.asset
        assert filled.side is old.side
        assert filled.target_price == old.target_price
        assert filled.quantity_asset == old.quantity_asset
        assert filled.created_at == old.created_at
        assert filled.status is LimitOrderStatus.FILLED
        assert filled.updated_at == filled.resolved_at == CONFIRMED_AT
        # The 25-stable debit differs from the 20-stable target-price commitment.
        assert Decimal("25") != old.target_price * old.quantity_asset
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.ORDER_FILLED
        assert updates[0].previous_snapshot_id == INITIAL_ID
        assert updates[0].new_snapshot_id == NEW_ID
        assert await snapshots.get(INITIAL_ID) == before

    asyncio.run(scenario())


def test_sell_reconciles_complete_manual_account_without_changing_other_accounts(tmp_path) -> None:
    async def scenario():
        _, snapshots = await storage(tmp_path)
        before = snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        reconciled = (position("ETH", "2.25"), position("USDC", "1090"))
        saved = await ConfirmLimitOrderFilled(snapshots, now=lambda: CONFIRMED_AT).execute(
            command(order_id=SELL_ID, settlement_asset="USDC", positions=reconciled)
        )
        assert saved.snapshot.portfolio.accounts[0].positions == reconciled
        assert saved.snapshot.portfolio.accounts[1:] == before.portfolio.accounts[1:]
        assert saved.snapshot.orders[1].status is LimitOrderStatus.FILLED
        assert saved.snapshot.orders[0] == before.orders[0]
        assert saved.snapshot.orders[2] == before.orders[2]
        assert saved.snapshot.core_positions == before.core_positions

    asyncio.run(scenario())


def test_omitted_symbol_is_absent_from_complete_reconciled_account(tmp_path) -> None:
    async def scenario():
        _, snapshots = await storage(tmp_path)
        before = snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        reconciled = command().positions[:-1]
        saved = await ConfirmLimitOrderFilled(snapshots, now=lambda: CONFIRMED_AT).execute(
            command(positions=reconciled)
        )
        assert saved.snapshot.portfolio.accounts[1].positions == reconciled
        assert all(
            position.symbol != "BNB" for position in saved.snapshot.portfolio.accounts[1].positions
        )
        assert saved.snapshot.portfolio.accounts[0] == before.portfolio.accounts[0]
        assert saved.snapshot.portfolio.accounts[2] == before.portfolio.accounts[2]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,match",
    [
        (
            {"positions": (position("BTC", "1"), position("USDT", "975"))},
            "asset balance to increase",
        ),
        (
            {"positions": (position("BTC", "1.2"), position("USDT", "1000"))},
            "settlement balance to decrease",
        ),
        (
            {
                "order_id": SELL_ID,
                "settlement_asset": "USDC",
                "positions": (position("ETH", "3"), position("USDC", "1090")),
            },
            "asset balance to decrease",
        ),
        (
            {
                "order_id": SELL_ID,
                "settlement_asset": "USDC",
                "positions": (position("ETH", "2.25"), position("USDC", "1000")),
            },
            "settlement balance to increase",
        ),
    ],
)
def test_directional_plausibility_rejects_without_writes(tmp_path, changes, match) -> None:
    async def scenario():
        database, snapshots = await storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(snapshot())
        with pytest.raises(DomainValidationError, match=match):
            await ConfirmLimitOrderFilled(snapshots).execute(command(**changes))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_global_core_floor_uses_all_accounts_and_rejects_breach(tmp_path) -> None:
    async def scenario():
        database, snapshots = await storage(tmp_path)
        before = snapshot()
        before = replace(
            before,
            core_positions=before.core_positions
            + (CorePosition("SOL", Decimal("5"), Decimal("5.5")),),
        )
        current = await ConfirmedStateService(snapshots).initialize(before)
        reconciled = (
            position("BTC", "1"),
            position("USDT", "1100"),
            position("USDC", "200"),
            position("SOL", "0.5"),
            position("BNB", "1"),
        )
        with pytest.raises(DomainValidationError, match="core floor"):
            await ConfirmLimitOrderFilled(snapshots, now=lambda: CONFIRMED_AT).execute(
                command(order_id=OTHER_ID, positions=reconciled)
            )
        assert await snapshots.get_current() == current
        assert await counts(database) == (1, 1, 1)

        valid = reconciled[:3] + (position("SOL", "1.5"), position("BNB", "1"))
        saved = await ConfirmLimitOrderFilled(snapshots, now=lambda: CONFIRMED_AT).execute(
            command(order_id=OTHER_ID, positions=valid)
        )
        assert saved.snapshot.portfolio.accounts[1].positions == valid
        assert saved.snapshot.portfolio.accounts[2] == before.portfolio.accounts[2]
        assert saved.snapshot.orders[2].status is LimitOrderStatus.FILLED

    asyncio.run(scenario())


@pytest.mark.parametrize("settlement_asset", ["BTC", "SOL", "EUR", "", "usdt"])
def test_command_rejects_non_stable_or_noncanonical_settlement(settlement_asset) -> None:
    with pytest.raises(DomainValidationError):
        command(settlement_asset=settlement_asset)


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_command_requires_positive_integer_expected_revision(revision) -> None:
    with pytest.raises(DomainValidationError, match="Invalid expected revision"):
        command(expected_revision=revision)


def test_command_requires_order_id_and_explicit_settlement_asset() -> None:
    with pytest.raises(DomainValidationError, match="non-nil UUID"):
        command(order_id=UUID(int=0))
    with pytest.raises(TypeError):
        ConfirmLimitOrderFilledCommand(expected_revision=1, order_id=BUY_ID, positions=())


def test_invalid_account_positions_rejected_without_writes(tmp_path) -> None:
    async def scenario():
        database, snapshots = await storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(snapshot())
        with pytest.raises(DomainValidationError, match="unique within an account"):
            await ConfirmLimitOrderFilled(snapshots).execute(
                command(positions=(position("BTC", "1.2"), position("BTC", "1.3")))
            )
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_missing_current_portfolio_rejected(tmp_path) -> None:
    async def scenario():
        database, snapshots = await storage(tmp_path)
        with pytest.raises(ResourceNotFoundError):
            await ConfirmLimitOrderFilled(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_unknown_or_terminal_order_rejected_without_writes(tmp_path) -> None:
    async def scenario():
        database, snapshots = await storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(snapshot())
        with pytest.raises(ResourceNotFoundError):
            await ConfirmLimitOrderFilled(snapshots).execute(command(order_id=UNKNOWN_ID))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

        filled = await ConfirmLimitOrderFilled(snapshots, now=lambda: CONFIRMED_AT).execute(
            command()
        )
        with pytest.raises(StateConflictError):
            await ConfirmLimitOrderFilled(snapshots).execute(command(expected_revision=2))
        assert await snapshots.get_current() == filled
        assert await counts(database) == (2, 2, 1)

    asyncio.run(scenario())


def test_stale_revision_rejected_without_writes(tmp_path) -> None:
    async def scenario():
        database, snapshots = await storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(snapshot())
        with pytest.raises(RevisionConflictError):
            await ConfirmLimitOrderFilled(snapshots).execute(command(expected_revision=2))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_only_confirmed_state_boundary_is_used_and_cas_conflict_is_not_retried() -> None:
    class RepositorySpy:
        def __init__(self):
            self.current = CurrentPortfolio(snapshot(), 1)
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
            raise AssertionError("Must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("Must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await ConfirmLimitOrderFilled(
            repository, now=lambda: CONFIRMED_AT, identity_factory=lambda: NEW_ID
        ).execute(command())
        assert result.revision == 2
        assert repository.loads == 1
        assert len(repository.confirmations) == 1
        proposed, revision, update = repository.confirmations[0]
        assert revision == 1
        assert proposed.id == update.new_snapshot_id == NEW_ID
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.update_type is UpdateType.ORDER_FILLED

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await ConfirmLimitOrderFilled(repository, now=lambda: CONFIRMED_AT).execute(command())
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
