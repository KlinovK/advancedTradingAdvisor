"""Offline ASGI tests for renaming an account and replacing its holdings."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.domain.persistence import CurrentPortfolio
from advanced_trading_advisor.http import app as http_app
from advanced_trading_advisor.portfolio.models import (
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
from advanced_trading_advisor.portfolio.rename_account import RenameAccount
from advanced_trading_advisor.portfolio.update_account_holdings import UpdateAccountHoldings

TOKEN = "synthetic-account-edit-token"
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("0a000000-0000-4000-8000-000000000001")
BINANCE_ID = UUID("0a000000-0000-4000-8000-000000000002")
WALLET_ID = UUID("0a000000-0000-4000-8000-000000000003")
ORDER_ID = UUID("0a000000-0000-4000-8000-000000000004")
RENAMED_SNAPSHOT_ID = UUID("0a000000-0000-4000-8000-000000000005")
HOLDINGS_SNAPSHOT_ID = UUID("0a000000-0000-4000-8000-000000000006")
LATER_SNAPSHOT_ID = UUID("0a000000-0000-4000-8000-000000000007")
UNKNOWN_ID = UUID("0a000000-0000-4000-8000-000000000008")


def initial_current() -> CurrentPortfolio:
    snapshot = ConfirmedPortfolioSnapshot(
        INITIAL_ID,
        NOW,
        Portfolio(
            (
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (
                        AssetPosition("BTC", Decimal("1.000000000000000000")),
                        AssetPosition("USDC", Decimal("100.00")),
                    ),
                ),
                Account(
                    WALLET_ID,
                    "Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (AssetPosition("ETH", Decimal("2.0")),),
                ),
            )
        ),
        FinancialSettings(Decimal("250.00"), Decimal("12")),
        (
            LimitOrder(
                ORDER_ID,
                "ETH",
                TradingSide.SELL,
                Decimal("5000.00"),
                Decimal("0.10"),
                LimitOrderStatus.OPEN,
                NOW - timedelta(days=1),
                NOW - timedelta(days=1),
                account_id=WALLET_ID,
            ),
        ),
        (CorePosition("ETH", Decimal("1.0"), Decimal("1.5")),),
    )
    return CurrentPortfolio(snapshot, 7)


class FakeSnapshots:
    def __init__(
        self,
        current: CurrentPortfolio | None,
        *,
        maximum_reads: int = 1,
        later_writer: bool = False,
    ):
        self.current = current
        self.maximum_reads = maximum_reads
        self.later_writer = later_writer
        self.reads = 0
        self.writes = 0
        self.mutation_result = None

    async def get_current(self) -> CurrentPortfolio | None:
        self.reads += 1
        assert self.reads <= self.maximum_reads, "Unexpected post-mutation state read."
        return self.current

    async def set_current(self, snapshot, *, expected_revision, update) -> CurrentPortfolio:
        self.writes += 1
        assert self.current is not None
        assert expected_revision == self.current.revision
        assert update.previous_snapshot_id == self.current.snapshot.id
        assert update.new_snapshot_id == snapshot.id
        self.mutation_result = CurrentPortfolio(snapshot, self.current.revision + 1)
        self.current = (
            CurrentPortfolio(replace(snapshot, id=LATER_SNAPSHOT_ID), self.current.revision + 2)
            if self.later_writer
            else self.mutation_result
        )
        return self.mutation_result


def app_with(repository, *, rename=None, holdings=None):
    return http_app.create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN},
        snapshots=repository,
        rename_account=rename
        if rename is not None
        else RenameAccount(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: RENAMED_SNAPSHOT_ID,
        ),
        update_account_holdings=holdings
        if holdings is not None
        else UpdateAccountHoldings(
            repository,
            now=lambda: NOW + timedelta(seconds=1),
            identity_factory=lambda: HOLDINGS_SNAPSHOT_ID,
        ),
    )


def path(kind, account_id=BINANCE_ID):
    base = f"/v1/portfolio/accounts/{account_id}"
    return base if kind == "rename" else f"{base}/holdings"


def body(kind, **changes):
    data = (
        {"expected_revision": 7, "name": "Renamed Binance"}
        if kind == "rename"
        else {
            "expected_revision": 7,
            "positions": [
                {"symbol": "BTC", "amount": "0.123456789123456789"},
                {"symbol": "SOL", "amount": "2.50"},
            ],
        }
    )
    return data | changes


def request(app, kind, *, account_id=BINANCE_ID, payload=None, authorization=f"Bearer {TOKEN}"):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {} if authorization is None else {"Authorization": authorization}
            return await client.request(
                "PATCH" if kind == "rename" else "PUT",
                path(kind, account_id),
                json=body(kind) if payload is None else payload,
                headers=headers,
            )

    return asyncio.run(send())


@pytest.mark.parametrize(
    "kind,snapshot_id", [("rename", RENAMED_SNAPSHOT_ID), ("holdings", HOLDINGS_SNAPSHOT_ID)]
)
def test_success_serializes_exact_mutation_even_if_another_writer_advances_state(
    monkeypatch, kind, snapshot_id
):
    def no_http_account_uuid():
        raise AssertionError("Editing must not generate an account UUID in HTTP.")

    monkeypatch.setattr(http_app, "uuid4", no_http_account_uuid)
    before = initial_current()
    repository = FakeSnapshots(before, later_writer=True)

    response = request(app_with(repository), kind)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 1
    assert repository.mutation_result.revision == 8
    assert repository.mutation_result.snapshot.id == snapshot_id
    assert repository.current.revision == 9
    assert repository.current.snapshot.id == LATER_SNAPSHOT_ID
    result = response.json()
    assert set(result) == {
        "revision",
        "snapshot_id",
        "confirmed_at",
        "accounts",
        "aggregate_positions",
        "financial_settings",
        "core_positions",
        "limit_orders",
    }
    assert result["revision"] == 8
    assert result["snapshot_id"] == str(snapshot_id)
    assert result["accounts"][0]["id"] == str(BINANCE_ID)
    assert result["accounts"][0]["type"] == "binance"
    assert result["accounts"][1]["id"] == str(WALLET_ID)
    assert result["accounts"][1]["name"] == "Wallet"
    assert result["financial_settings"] == {
        "monthly_expenses_usd": "250.00",
        "target_expense_runway_months": "12",
    }
    assert result["core_positions"][0]["symbol"] == "ETH"
    assert result["limit_orders"][0]["id"] == str(ORDER_ID)
    if kind == "rename":
        assert result["accounts"][0]["name"] == "Renamed Binance"
        assert result["accounts"][0]["positions"] == [
            {"symbol": "BTC", "amount": "1.000000000000000000"},
            {"symbol": "USDC", "amount": "100.00"},
        ]
        assert repository.mutation_result.snapshot.portfolio.accounts[0].positions == (
            before.snapshot.portfolio.accounts[0].positions
        )
        assert result["aggregate_positions"] == [
            {"symbol": "BTC", "amount": "1.000000000000000000"},
            {"symbol": "ETH", "amount": "2.0"},
            {"symbol": "USDC", "amount": "100.00"},
        ]
    else:
        assert result["accounts"][0]["name"] == "Binance"
        assert result["accounts"][0]["positions"] == [
            {"symbol": "BTC", "amount": "0.123456789123456789"},
            {"symbol": "SOL", "amount": "2.50"},
        ]
        assert result["accounts"][1]["positions"] == [{"symbol": "ETH", "amount": "2.0"}]
        assert result["aggregate_positions"] == [
            {"symbol": "BTC", "amount": "0.123456789123456789"},
            {"symbol": "ETH", "amount": "2.0"},
            {"symbol": "SOL", "amount": "2.50"},
        ]
        positions = repository.mutation_result.snapshot.portfolio.accounts[0].positions
        assert type(positions[0].amount) is Decimal
        assert positions[0].amount.as_tuple() == Decimal("0.123456789123456789").as_tuple()


@pytest.mark.parametrize("kind", ["rename", "holdings"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_authentication_rejects_before_application_use_case(kind, authorization):
    repository = FakeSnapshots(initial_current())

    response = request(app_with(repository), kind, authorization=authorization)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 0


@pytest.mark.parametrize("kind", ["rename", "holdings"])
@pytest.mark.parametrize("account_id", [UNKNOWN_ID, None])
def test_unknown_account_or_missing_current_maps_to_404(kind, account_id):
    repository = FakeSnapshots(initial_current() if account_id is not None else None)

    response = request(app_with(repository), kind, account_id=account_id or BINANCE_ID)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert repository.writes == 0


@pytest.mark.parametrize("kind", ["rename", "holdings"])
def test_stale_revision_maps_to_409_without_retry_or_extra_read(kind):
    repository = FakeSnapshots(initial_current())

    response = request(app_with(repository), kind, payload=body(kind, expected_revision=6))

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "revision_conflict",
        "message": "Portfolio changed; reload before confirming.",
        "reload_required": True,
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert repository.writes == 0
    assert "current_revision" not in response.text


@pytest.mark.parametrize("kind", ["rename", "holdings"])
def test_noop_maps_to_state_conflict_without_writes(kind):
    repository = FakeSnapshots(initial_current())
    payload = (
        body(kind, name="Binance")
        if kind == "rename"
        else body(
            kind,
            positions=[
                {"symbol": "BTC", "amount": "1.000000000000000000"},
                {"symbol": "USDC", "amount": "100.00"},
            ],
        )
    )

    response = request(app_with(repository), kind, payload=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert repository.writes == 0


def test_rename_allows_duplicate_account_names():
    repository = FakeSnapshots(initial_current())

    response = request(app_with(repository), "rename", payload=body("rename", name="Wallet"))

    assert response.status_code == 200
    assert [account["name"] for account in response.json()["accounts"]] == ["Wallet", "Wallet"]


@pytest.mark.parametrize(
    "kind,payload,account_id",
    [
        ("rename", body("rename", name=" \t "), BINANCE_ID),
        ("holdings", body("holdings", positions=[{"symbol": "BTC", "amount": "0"}]), BINANCE_ID),
        (
            "holdings",
            body(
                "holdings",
                positions=[
                    {"symbol": "ETH", "amount": "0.5"},
                ],
            ),
            WALLET_ID,
        ),
        (
            "holdings",
            body(
                "holdings",
                positions=[
                    {"symbol": "BTC", "amount": "1"},
                    {"symbol": "BTC", "amount": "2"},
                ],
            ),
            BINANCE_ID,
        ),
    ],
)
def test_domain_validation_is_sanitized_422(kind, payload, account_id):
    repository = FakeSnapshots(initial_current())

    response = request(app_with(repository), kind, account_id=account_id, payload=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.headers["cache-control"] == "no-store"
    assert repository.writes == 0


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("rename", {"name": "New name"}),
        ("rename", body("rename", expected_revision="7")),
        ("rename", body("rename", account_id=str(BINANCE_ID))),
        ("holdings", {"positions": []}),
        ("holdings", body("holdings", expected_revision=0)),
        ("holdings", body("holdings", positions="not-an-array")),
        ("holdings", body("holdings", positions=[{"symbol": "BTC", "amount": 0.1}])),
        ("holdings", body("holdings", account_id=str(BINANCE_ID))),
    ],
)
def test_malformed_body_uses_existing_invalid_request_envelope(kind, payload):
    repository = FakeSnapshots(initial_current())

    response = request(app_with(repository), kind, payload=payload)

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 0


@pytest.mark.parametrize("kind", ["rename", "holdings"])
def test_malformed_path_uuid_uses_invalid_request_envelope(kind):
    repository = FakeSnapshots(initial_current())

    response = request(app_with(repository), kind, account_id="not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert repository.reads == repository.writes == 0


def test_malformed_decimal_string_is_sanitized_422_before_any_read():
    repository = FakeSnapshots(initial_current())

    response = request(
        app_with(repository),
        "holdings",
        payload=body("holdings", positions=[{"symbol": "BTC", "amount": "1e-4"}]),
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert repository.reads == repository.writes == 0


@pytest.mark.parametrize("kind", ["rename", "holdings"])
def test_unexpected_use_case_failure_is_sanitized(kind):
    class FailingUseCase:
        async def execute(self, command):
            raise RuntimeError("private account and database details")

    repository = FakeSnapshots(initial_current())
    app = app_with(
        repository,
        rename=FailingUseCase() if kind == "rename" else None,
        holdings=FailingUseCase() if kind == "holdings" else None,
    )

    response = request(app, kind)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private account and database details" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 0


@pytest.mark.parametrize("kind", ["rename", "holdings"])
def test_retry_of_original_revision_fails_closed_without_replay(kind):
    repository = FakeSnapshots(initial_current(), maximum_reads=2)
    app = app_with(repository)

    first = request(app, kind)
    retry = request(app, kind)

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "revision_conflict"
    assert repository.reads == 2
    assert repository.writes == 1
