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
from advanced_trading_advisor.portfolio.update_core_position import (
    UpdateCorePosition,
    UpdateCorePositionCommand,
)
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("d3795005-5745-4386-bae0-a54e5e7811ee")
NEW_SNAPSHOT_ID = UUID("32d86e19-6e43-4ae9-9f60-9f37b58f2702")
BINANCE_ID = UUID("0267503c-071b-4c66-92b1-e2ab4b52cf82")
WALLET_ID = UUID("5fb43c0d-0427-4166-a18b-d17f7c512cb7")
ORDER_ID = UUID("c6f13ec1-c3dd-4e72-9ecb-72c4057c1058")


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
                    (
                        AssetPosition("ETH", Decimal("1.5")),
                        AssetPosition("USDT", Decimal("700.00")),
                    ),
                ),
                Account(
                    WALLET_ID,
                    "External Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (
                        AssetPosition("BTC", Decimal("0.2")),
                        AssetPosition("SOL", Decimal("2.0")),
                    ),
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
        (
            CorePosition("ETH", Decimal("3.0"), Decimal("3.25")),
            CorePosition("BTC", Decimal("0.1"), Decimal("0.15")),
        ),
    )


def command(**changes) -> UpdateCorePositionCommand:
    values = {
        "expected_revision": 1,
        "symbol": "ETH",
        "hard_floor": Decimal("3.25000000000000000001"),
        "preferred_quantity": Decimal("3.4"),
    }
    return UpdateCorePositionCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "update-core-position.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


def test_upsert_replaces_one_global_core_and_preserves_everything_else(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await UpdateCorePosition(
            snapshots,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        assert saved.snapshot.core_positions == (
            before.core_positions[1],
            CorePosition("ETH", Decimal("3.25000000000000000001"), Decimal("3.4")),
        )
        assert saved.snapshot.core_positions[1].hard_floor.as_tuple() == (
            command().hard_floor.as_tuple()
        )
        assert saved.snapshot.core_positions[1].policy_version == 1
        assert saved.snapshot.portfolio == before.portfolio
        assert saved.snapshot.portfolio.accounts == before.portfolio.accounts
        assert [account.id for account in saved.snapshot.portfolio.accounts] == [
            DEFAULT_MANUAL_ACCOUNT_ID,
            BINANCE_ID,
            WALLET_ID,
        ]
        for actual, original in zip(
            saved.snapshot.portfolio.accounts, before.portfolio.accounts, strict=True
        ):
            assert [position.amount.as_tuple() for position in actual.positions] == [
                position.amount.as_tuple() for position in original.positions
            ]
        assert saved.snapshot.portfolio.positions == before.portfolio.positions
        assert saved.snapshot.financial_settings == before.financial_settings
        assert saved.snapshot.orders == before.orders
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.CORE_CONFIGURATION_CHANGE
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


def test_new_core_asset_is_appended_like_cli_upsert(tmp_path) -> None:
    async def scenario():
        _, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        saved = await UpdateCorePosition(snapshots, now=lambda: NOW + timedelta(seconds=1)).execute(
            command(symbol="SOL", hard_floor=Decimal("1"), preferred_quantity=Decimal("1.5"))
        )
        assert saved.snapshot.core_positions == before.snapshot.core_positions + (
            CorePosition("SOL", Decimal("1"), Decimal("1.5")),
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes,match",
    [
        ({"symbol": "USDT"}, "Stablecoins cannot have a core floor"),
        ({"symbol": "XRP"}, "supported crypto asset"),
        ({"hard_floor": Decimal("0")}, "positive"),
        ({"preferred_quantity": Decimal("1")}, "Preferred core is below floor"),
        ({"hard_floor": Decimal("3.6"), "preferred_quantity": Decimal("4")}, "core floor exceeds"),
    ],
)
def test_invalid_or_unfunded_core_is_rejected_without_writes(tmp_path, changes, match) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(DomainValidationError, match=match):
            await UpdateCorePosition(snapshots).execute(command(**changes))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_identical_resulting_core_collection_is_noop(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(StateConflictError):
            await UpdateCorePosition(snapshots).execute(
                command(
                    symbol="BTC", hard_floor=Decimal("0.10"), preferred_quantity=Decimal("0.15")
                )
            )
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_upsert_preserves_cli_ordering_even_for_same_earlier_entry(tmp_path) -> None:
    async def scenario():
        _, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        saved = await UpdateCorePosition(snapshots, now=lambda: NOW + timedelta(seconds=1)).execute(
            command(symbol="ETH", hard_floor=Decimal("3.0"), preferred_quantity=Decimal("3.25"))
        )
        assert saved.revision == before.revision + 1
        assert saved.snapshot.core_positions == (
            before.snapshot.core_positions[1],
            before.snapshot.core_positions[0],
        )

    asyncio.run(scenario())


def test_no_current_portfolio_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(ResourceNotFoundError):
            await UpdateCorePosition(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_stale_revision_has_no_writes_or_retry(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(RevisionConflictError):
            await UpdateCorePosition(snapshots).execute(command(expected_revision=2))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


@pytest.mark.parametrize("revision", [0, -1, True, "1"])
def test_command_requires_a_positive_integer_expected_revision(revision) -> None:
    with pytest.raises(DomainValidationError, match="Invalid expected revision"):
        command(expected_revision=revision)


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
            raise AssertionError("UpdateCorePosition must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("UpdateCorePosition must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await UpdateCorePosition(
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
        assert update.update_type is UpdateType.CORE_CONFIGURATION_CHANGE
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await UpdateCorePosition(repository, now=lambda: NOW + timedelta(seconds=1)).execute(
                command()
            )
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
