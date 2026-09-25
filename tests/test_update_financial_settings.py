import asyncio
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
from advanced_trading_advisor.portfolio.update_financial_settings import (
    UpdateFinancialSettings,
    UpdateFinancialSettingsCommand,
)
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("840093ef-5d20-488d-8097-c02de7d71852")
NEW_SNAPSHOT_ID = UUID("16e1ea59-4f6f-41bd-bb93-d08a29d00de9")
BINANCE_ID = UUID("80b8fc6b-bf47-4430-a8dc-8ce5c58e4572")
WALLET_ID = UUID("b08db7c5-a5e7-4232-af2a-99f42c09eab7")
ORDER_ID = UUID("dcd9bd9f-6a57-48d9-b843-6b49c4c58d65")


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
                        AssetPosition("USDC", Decimal("100.00")),
                    ),
                ),
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (
                        AssetPosition("BTC", Decimal("0.025000000000000000001")),
                        AssetPosition("USDT", Decimal("700.00")),
                    ),
                ),
                Account(
                    WALLET_ID,
                    "External Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (AssetPosition("SOL", Decimal("2.5")),),
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


def command(**changes) -> UpdateFinancialSettingsCommand:
    values = {
        "expected_revision": 1,
        "monthly_expenses_usd": Decimal("275.12345678901234567890"),
        "target_expense_runway_months": Decimal("18.75"),
    }
    return UpdateFinancialSettingsCommand(**(values | changes))


async def disposable_storage(tmp_path):
    database = Database(tmp_path / "update-financial-settings.sqlite")
    await database.initialize()
    return database, SQLiteSnapshotRepository(database)


async def counts(database) -> tuple[int, int, int]:
    async with database.connection() as connection:
        values = []
        for table in ("portfolio_snapshots", "confirmed_updates", "current_portfolio"):
            row = await (await connection.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
            values.append(row[0])
        return tuple(values)


def test_settings_only_update_preserves_multi_account_state_and_writes_audit(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = initial_snapshot()
        await ConfirmedStateService(snapshots).initialize(before)
        saved = await UpdateFinancialSettings(
            snapshots,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: NEW_SNAPSHOT_ID,
        ).execute(command())

        assert saved.revision == 2
        assert saved.snapshot.id == NEW_SNAPSHOT_ID
        assert saved.snapshot.financial_settings == FinancialSettings(
            Decimal("275.12345678901234567890"), Decimal("18.75")
        )
        assert saved.snapshot.financial_settings.monthly_expenses_usd.as_tuple() == (
            command().monthly_expenses_usd.as_tuple()
        )
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
            assert actual.positions == original.positions
            assert [position.amount.as_tuple() for position in actual.positions] == [
                position.amount.as_tuple() for position in original.positions
            ]
        assert saved.snapshot.portfolio.positions == before.portfolio.positions
        assert saved.snapshot.core_positions == before.core_positions
        assert saved.snapshot.orders == before.orders
        assert await snapshots.get_current() == saved
        assert await counts(database) == (2, 2, 1)
        updates = await SQLiteConfirmedUpdateRepository(database).list_recent()
        assert updates[0].update_type is UpdateType.EXPENSE_SETTINGS_CHANGE
        assert updates[0].previous_snapshot_id == before.id
        assert updates[0].new_snapshot_id == NEW_SNAPSHOT_ID

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "changes",
    [
        {"monthly_expenses_usd": Decimal("0")},
        {"monthly_expenses_usd": Decimal("-1")},
        {"monthly_expenses_usd": Decimal("NaN")},
        {"monthly_expenses_usd": 250.0},
        {"target_expense_runway_months": Decimal("0")},
        {"target_expense_runway_months": Decimal("-1")},
        {"target_expense_runway_months": Decimal("Infinity")},
        {"target_expense_runway_months": 12.0},
    ],
)
def test_invalid_settings_use_existing_domain_validation_without_writes(tmp_path, changes) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(DomainValidationError):
            await UpdateFinancialSettings(snapshots).execute(command(**changes))
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_equal_settings_are_noop_without_revision_or_audit(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(DomainValidationError, match="Financial settings are unchanged"):
            await UpdateFinancialSettings(snapshots).execute(
                command(
                    monthly_expenses_usd=Decimal("250.0"),
                    target_expense_runway_months=Decimal("12.00"),
                )
            )
        assert await snapshots.get_current() == before
        assert await counts(database) == (1, 1, 1)

    asyncio.run(scenario())


def test_no_current_portfolio_fails_without_initializing(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        with pytest.raises(DomainValidationError, match="No confirmed portfolio"):
            await UpdateFinancialSettings(snapshots).execute(command())
        assert await counts(database) == (0, 0, 0)

    asyncio.run(scenario())


def test_stale_revision_has_no_writes_or_retry(tmp_path) -> None:
    async def scenario():
        database, snapshots = await disposable_storage(tmp_path)
        before = await ConfirmedStateService(snapshots).initialize(initial_snapshot())
        with pytest.raises(ConflictError, match="reload before confirming"):
            await UpdateFinancialSettings(snapshots).execute(command(expected_revision=2))
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
            raise AssertionError("UpdateFinancialSettings must not save snapshots directly")

        async def append(self, _update):
            raise AssertionError("UpdateFinancialSettings must not append audits directly")

    async def scenario():
        repository = RepositorySpy()
        result = await UpdateFinancialSettings(
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
        assert update.update_type is UpdateType.EXPENSE_SETTINGS_CHANGE
        assert update.previous_snapshot_id == INITIAL_ID
        assert update.new_snapshot_id == snapshot.id == NEW_SNAPSHOT_ID

        repository = RepositorySpy()
        repository.conflict = True
        with pytest.raises(ConflictError):
            await UpdateFinancialSettings(
                repository, now=lambda: NOW + timedelta(seconds=1)
            ).execute(command())
        assert repository.loads == 1
        assert len(repository.confirmations) == 1

    asyncio.run(scenario())
