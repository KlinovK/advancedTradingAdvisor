import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from advanced_trading_advisor.domain.persistence import PayloadError
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import (
    Account,
    AccountType,
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    FinancialSettings,
    Portfolio,
)
from advanced_trading_advisor.storage.codecs import (
    PAYLOAD_VERSION,
    SNAPSHOT_PAYLOAD_VERSION,
    SnapshotCodec,
    utc_text,
)
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.postgres_repositories import pg_save_snapshot
from advanced_trading_advisor.storage.repositories import SQLiteSnapshotRepository, snapshot_row

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
SNAPSHOT_ID = UUID("a82fe49d-c43f-4c0d-91da-d5f64a31931b")
BINANCE_ID = UUID("cb3824be-f078-48a5-8fdf-3e49ba25b6f4")
WALLET_ID = UUID("e548c922-272f-469f-bb89-35c8e7cc6de5")


def snapshot(accounts: tuple[Account, ...]) -> ConfirmedPortfolioSnapshot:
    return ConfirmedPortfolioSnapshot(
        SNAPSHOT_ID,
        NOW,
        Portfolio(accounts),
        FinancialSettings(Decimal("250.00"), Decimal("12.0")),
    )


def account(
    account_id: UUID = BINANCE_ID,
    name: str = "Binance",
    account_type: AccountType = AccountType.BINANCE,
    positions: tuple[AssetPosition, ...] = (AssetPosition("ETH", Decimal("0.8")),),
) -> Account:
    return Account(account_id, name, account_type, positions)


def test_one_account_round_trip_preserves_identity_name_type_and_decimal() -> None:
    original = snapshot(
        (
            account(
                positions=(
                    AssetPosition("USDC", Decimal("123456789012345678901234567890.00100")),
                    AssetPosition("BTC", Decimal("0.000000000000000000000000000000123456789")),
                )
            ),
        )
    )

    encoded = SnapshotCodec.encode(original)
    decoded = SnapshotCodec.decode(encoded)
    payload = json.loads(encoded)

    assert decoded == original
    assert decoded.portfolio.accounts[0].id == BINANCE_ID
    assert decoded.portfolio.accounts[0].name == "Binance"
    assert decoded.portfolio.accounts[0].type is AccountType.BINANCE
    assert decoded.portfolio.accounts[0].positions[0].amount.as_tuple() == (
        original.portfolio.accounts[0].positions[0].amount.as_tuple()
    )
    assert payload["data"]["portfolio"]["accounts"][0]["positions"][0]["amount"] == (
        "123456789012345678901234567890.00100"
    )
    assert SnapshotCodec.encode(decoded) == encoded


def test_multiple_accounts_keep_same_symbol_separate_and_derive_aggregate() -> None:
    original = snapshot(
        (
            account(positions=(AssetPosition("ETH", Decimal("0.8")),)),
            account(
                WALLET_ID,
                "External Wallet",
                AccountType.EXTERNAL_WALLET,
                (AssetPosition("ETH", Decimal("2.7")),),
            ),
        )
    )

    encoded = SnapshotCodec.encode(original)
    decoded = SnapshotCodec.decode(encoded)
    data = json.loads(encoded)["data"]

    assert decoded == original
    assert [entry.id for entry in decoded.portfolio.accounts] == [BINANCE_ID, WALLET_ID]
    assert [entry.positions[0].amount for entry in decoded.portfolio.accounts] == [
        Decimal("0.8"),
        Decimal("2.7"),
    ]
    assert decoded.portfolio.positions == (AssetPosition("ETH", Decimal("3.5")),)
    assert "positions" not in data
    assert "positions" not in data["portfolio"]
    assert list(data["portfolio"]) == ["accounts"]


@pytest.mark.parametrize("account_type", list(AccountType))
def test_every_account_type_round_trips(account_type: AccountType) -> None:
    original = snapshot((account(account_type=account_type),))

    encoded = SnapshotCodec.encode(original)

    assert SnapshotCodec.decode(encoded).portfolio.accounts[0].type is account_type
    assert json.loads(encoded)["data"]["portfolio"]["accounts"][0]["type"] == (account_type.value)


def test_empty_portfolio_round_trips() -> None:
    original = snapshot(())

    encoded = SnapshotCodec.encode(original)

    assert SnapshotCodec.decode(encoded) == original
    assert json.loads(encoded)["data"]["portfolio"] == {"accounts": []}


def test_account_and_position_tuple_order_is_preserved() -> None:
    original = snapshot(
        (
            account(
                WALLET_ID,
                "Wallet",
                AccountType.EXTERNAL_WALLET,
                (AssetPosition("SOL", Decimal("2.00")), AssetPosition("BTC", Decimal("0.1"))),
            ),
            account(BINANCE_ID, "Exchange", AccountType.BINANCE),
        )
    )

    encoded = SnapshotCodec.encode(original)
    decoded = SnapshotCodec.decode(encoded)

    assert decoded == original
    assert [entry.id for entry in decoded.portfolio.accounts] == [WALLET_ID, BINANCE_ID]
    assert [p.symbol for p in decoded.portfolio.accounts[0].positions] == ["SOL", "BTC"]
    assert SnapshotCodec.encode(decoded) == encoded


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["portfolio"].update(accounts=None),
        lambda data: data["portfolio"].update(extra=True),
        lambda data: data["portfolio"]["accounts"][0].update(id="not-a-uuid"),
        lambda data: data["portfolio"]["accounts"][0].update(name=" \t "),
        lambda data: data["portfolio"]["accounts"][0].update(type="unknown"),
        lambda data: data["portfolio"]["accounts"][0].update(positions=None),
        lambda data: data["portfolio"]["accounts"][0].update(extra=True),
        lambda data: data["portfolio"]["accounts"][0]["positions"][0].update(amount=0.8),
        lambda data: data["portfolio"]["accounts"].append(data["portfolio"]["accounts"][0].copy()),
        lambda data: data["portfolio"]["accounts"][0]["positions"].append(
            data["portfolio"]["accounts"][0]["positions"][0].copy()
        ),
    ],
)
def test_malformed_account_payload_is_rejected(mutate) -> None:
    payload = json.loads(SnapshotCodec.encode(snapshot((account(),))))
    mutate(payload["data"])

    with pytest.raises(PayloadError, match="Malformed or unsupported versioned payload"):
        SnapshotCodec.decode(json.dumps(payload))


@pytest.mark.parametrize("old_version", [1, 2])
def test_old_aggregate_only_snapshot_is_rejected(old_version: int) -> None:
    payload = json.loads(SnapshotCodec.encode(snapshot((account(),))))
    payload["schema_version"] = old_version
    payload["data"]["positions"] = [{"symbol": "ETH", "amount": "0.8"}]
    del payload["data"]["portfolio"]
    if old_version == 1:
        del payload["data"]["core_positions"]

    with pytest.raises(PayloadError, match="Malformed or unsupported versioned payload"):
        SnapshotCodec.decode(json.dumps(payload))


def test_snapshot_row_version_remains_independent_of_payload_version() -> None:
    original = snapshot((account(),))
    payload = SnapshotCodec.encode(original)

    assert json.loads(payload)["schema_version"] == SNAPSHOT_PAYLOAD_VERSION
    assert PAYLOAD_VERSION == 1
    assert (
        snapshot_row(
            {
                "schema_version": PAYLOAD_VERSION,
                "payload_json": payload,
                "snapshot_id": str(original.id),
                "confirmed_at": utc_text(original.confirmed_at),
            }
        )
        == original
    )


def test_sqlite_repository_round_trips_account_provenance_in_disposable_database(tmp_path) -> None:
    original = snapshot(
        (
            account(),
            account(
                WALLET_ID,
                "External Wallet",
                AccountType.EXTERNAL_WALLET,
                (AssetPosition("ETH", Decimal("2.7")),),
            ),
        )
    )

    async def scenario() -> None:
        database = Database(tmp_path / "advisor.sqlite")
        await database.initialize()
        repository = SQLiteSnapshotRepository(database)
        await ConfirmedStateService(repository).initialize(original)

        assert await repository.get(original.id) == original
        assert (await repository.get_current()).snapshot == original

    asyncio.run(scenario())


def test_postgres_save_uses_shared_codec_without_connecting_to_postgres() -> None:
    original = snapshot((account(),))

    class FakeConnection:
        payload: str | None = None

        async def execute(self, _query: str, *arguments) -> None:
            self.payload = arguments[2]
            assert arguments[3] == PAYLOAD_VERSION

        async def fetchrow(self, _query: str, _snapshot_id: UUID) -> dict:
            return {
                "schema_version": PAYLOAD_VERSION,
                "payload_json": self.payload,
                "snapshot_id": str(original.id),
                "confirmed_at": original.confirmed_at,
            }

    connection = FakeConnection()
    asyncio.run(pg_save_snapshot(connection, original))

    assert connection.payload == SnapshotCodec.encode(original)
    assert SnapshotCodec.decode(connection.payload) == original
