"""Offline ASGI tests for the first authenticated portfolio mutation."""

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.domain.persistence import (
    CurrentPortfolio,
    StateConflictError,
)
from advanced_trading_advisor.http import app as http_app
from advanced_trading_advisor.portfolio.create_account import CreateAccount
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

TOKEN = "synthetic-http-write-token"
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("09000000-0000-4000-8000-000000000001")
BINANCE_ID = UUID("09000000-0000-4000-8000-000000000002")
WALLET_ID = UUID("09000000-0000-4000-8000-000000000003")
ORDER_ID = UUID("09000000-0000-4000-8000-000000000004")
NEW_ACCOUNT_ID = UUID("09000000-0000-4000-8000-000000000005")
NEW_SNAPSHOT_ID = UUID("09000000-0000-4000-8000-000000000006")


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
    def __init__(self, current: CurrentPortfolio | None, *, maximum_reads: int = 1):
        self.current = current
        self.maximum_reads = maximum_reads
        self.reads = 0
        self.writes = 0
        self.confirmed_account_id = None

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
        self.confirmed_account_id = snapshot.portfolio.accounts[-1].id
        self.current = CurrentPortfolio(snapshot, self.current.revision + 1)
        return self.current


def app_with(repository: FakeSnapshots, *, use_case=None):
    creator = use_case or CreateAccount(
        repository,
        now=lambda: NOW + timedelta(seconds=1),
        identity_factory=lambda: NEW_SNAPSHOT_ID,
    )
    return http_app.create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN}, snapshots=repository, create_account=creator
    )


def body(**changes):
    data = {
        "expected_revision": 7,
        "name": "Second Binance",
        "account_type": "other_exchange",
        "positions": [
            {"symbol": "BTC", "amount": "0.123456789123456789"},
            {"symbol": "SOL", "amount": "2.50"},
        ],
    }
    return data | changes


def post(app, payload=None, *, authorization=f"Bearer {TOKEN}") -> httpx.Response:
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {} if authorization is None else {"Authorization": authorization}
            return await client.post(
                "/v1/portfolio/accounts",
                json=body() if payload is None else payload,
                headers=headers,
            )

    return asyncio.run(request())


def test_authenticated_creation_returns_exact_complete_current_portfolio(monkeypatch):
    monkeypatch.setattr(http_app, "uuid4", lambda: NEW_ACCOUNT_ID)
    repository = FakeSnapshots(initial_current())

    response = post(app_with(repository))

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1  # A second read would fail, even after the commit.
    assert repository.writes == 1
    assert repository.confirmed_account_id == NEW_ACCOUNT_ID
    assert repository.current.snapshot.id == NEW_SNAPSHOT_ID
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
    assert result["snapshot_id"] == str(NEW_SNAPSHOT_ID)
    assert result["accounts"][-1] == {
        "id": str(NEW_ACCOUNT_ID),
        "name": "Second Binance",
        "type": "other_exchange",
        "positions": [
            {"symbol": "BTC", "amount": "0.123456789123456789"},
            {"symbol": "SOL", "amount": "2.50"},
        ],
    }
    assert result["aggregate_positions"] == [
        {"symbol": "BTC", "amount": "1.123456789123456789"},
        {"symbol": "ETH", "amount": "2.0"},
        {"symbol": "SOL", "amount": "2.50"},
        {"symbol": "USDC", "amount": "100.00"},
    ]
    assert result["financial_settings"] == {
        "monthly_expenses_usd": "250.00",
        "target_expense_runway_months": "12",
    }
    assert result["core_positions"][0]["symbol"] == "ETH"
    assert result["limit_orders"][0]["id"] == str(ORDER_ID)
    amount = repository.current.snapshot.portfolio.accounts[-1].positions[0].amount
    assert type(amount) is Decimal
    assert amount.as_tuple() == Decimal("0.123456789123456789").as_tuple()


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_authentication_fails_before_create_account(authorization):
    repository = FakeSnapshots(initial_current())

    response = post(app_with(repository), authorization=authorization)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Wallet", "account_type": "manual", "positions": []},
        body(expected_revision=0),
        body(expected_revision="7"),
        body(account_type="unknown"),
        body(positions="not-an-array"),
        body(positions=[{"symbol": "BTC", "amount": 0.5}]),
        body(account_id=str(NEW_ACCOUNT_ID)),
        body(unexpected="not accepted"),
    ],
)
def test_malformed_request_uses_stable_validation_envelope_without_reads(payload):
    repository = FakeSnapshots(initial_current())

    response = post(app_with(repository), payload)

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 0


@pytest.mark.parametrize(
    "payload,expected_reads",
    [
        (body(name="  "), 1),
        (body(positions=[{"symbol": "BTC", "amount": "1e-4"}]), 0),
        (body(positions=[{"symbol": "btc", "amount": "0.1"}]), 0),
        (body(positions=[{"symbol": "BTC", "amount": "0"}]), 0),
    ],
)
def test_domain_validation_maps_to_sanitized_422(payload, expected_reads):
    repository = FakeSnapshots(initial_current())

    response = post(app_with(repository), payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == expected_reads
    assert repository.writes == 0


def test_missing_current_portfolio_maps_to_404_without_initializing():
    repository = FakeSnapshots(None)

    response = post(app_with(repository))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert repository.writes == 0


def test_stale_revision_maps_to_reload_conflict_without_retry_or_extra_read():
    repository = FakeSnapshots(initial_current())

    response = post(app_with(repository), body(expected_revision=6))

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


def test_lost_success_response_retry_fails_closed_at_original_revision():
    repository = FakeSnapshots(initial_current(), maximum_reads=2)
    app = app_with(repository)

    assert post(app).status_code == 201
    retry = post(app)

    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "revision_conflict"
    assert repository.reads == 2
    assert repository.writes == 1


@pytest.mark.parametrize(
    "failure,status,code",
    [
        (StateConflictError("private portfolio details"), 409, "state_conflict"),
        (RuntimeError("private database credentials"), 500, "internal_error"),
    ],
)
def test_typed_conflict_and_unexpected_error_are_sanitized(failure, status, code):
    class FailingUseCase:
        async def execute(self, command):
            raise failure

    repository = FakeSnapshots(initial_current())

    response = post(app_with(repository, use_case=FailingUseCase()))

    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert str(failure) not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 0
