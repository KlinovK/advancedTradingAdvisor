"""Offline in-process tests for the first private HTTP read boundary."""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.domain.persistence import CurrentPortfolio
from advanced_trading_advisor.http.app import create_app
from advanced_trading_advisor.http.auth import APITokenSettings
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

TOKEN = "synthetic-test-token-not-a-real-secret"
SNAPSHOT_ID = UUID("08000000-0000-4000-8000-000000000001")
BINANCE_ID = UUID("08000000-0000-4000-8000-000000000002")
WALLET_ID = UUID("08000000-0000-4000-8000-000000000003")
ORDER_ID = UUID("08000000-0000-4000-8000-000000000004")
LOCAL_TIME = timezone(timedelta(hours=2))


class FakeSnapshots:
    def __init__(self, current: CurrentPortfolio | None):
        self.current = current
        self.reads = 0
        self.writes = 0

    async def get_current(self) -> CurrentPortfolio | None:
        self.reads += 1
        return self.current

    async def set_current(self, *_args, **_kwargs):
        self.writes += 1
        raise AssertionError("The HTTP read route must not write confirmed state.")


def current_portfolio() -> CurrentPortfolio:
    confirmed_at = datetime(2026, 9, 25, 11, 0, tzinfo=LOCAL_TIME)
    order_time = datetime(2026, 9, 24, 17, 30, tzinfo=LOCAL_TIME)
    snapshot = ConfirmedPortfolioSnapshot(
        SNAPSHOT_ID,
        confirmed_at,
        Portfolio(
            (
                Account(
                    BINANCE_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (
                        AssetPosition("BTC", Decimal("0.019800000000000000")),
                        AssetPosition("ETH", Decimal("3.450300000000000001")),
                    ),
                ),
                Account(
                    WALLET_ID,
                    "External Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (
                        AssetPosition("BTC", Decimal("0.000000000000000001")),
                        AssetPosition("USDC", Decimal("1400.230000000000000001")),
                    ),
                ),
            )
        ),
        FinancialSettings(Decimal("250.000000000000000001"), Decimal("12.0")),
        (
            LimitOrder(
                ORDER_ID,
                "ETH",
                TradingSide.SELL,
                Decimal("76500.000000000000000001"),
                Decimal("0.010000000000000001"),
                LimitOrderStatus.OPEN,
                order_time,
                order_time,
                account_id=BINANCE_ID,
            ),
        ),
        (CorePosition("ETH", Decimal("2.0"), Decimal("2.5")),),
    )
    return CurrentPortfolio(snapshot, revision=7)


def app_with(repository: FakeSnapshots):
    return create_app(environment={"ADVISOR_API_TOKEN": TOKEN}, snapshots=repository)


def get(app, authorization: str | None = f"Bearer {TOKEN}") -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {} if authorization is None else {"Authorization": authorization}
            return await client.get("/v1/portfolio", headers=headers)

    return asyncio.run(request())


@pytest.mark.parametrize(
    "authorization",
    [None, "", "Basic credential", "Bearer", "Bearer ", "Bearer wrong-token", "Bearer bad token"],
)
def test_missing_malformed_or_wrong_bearer_is_the_same_sanitized_401(
    authorization: str | None,
) -> None:
    repository = FakeSnapshots(current_portfolio())

    response = get(app_with(repository), authorization)

    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "unauthorized", "message": "Authentication required."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["www-authenticate"] == "Bearer"
    assert repository.reads == 0
    assert repository.writes == 0
    assert TOKEN not in response.text


def test_duplicate_authorization_headers_are_rejected_before_portfolio_read() -> None:
    repository = FakeSnapshots(current_portfolio())

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_with(repository)), base_url="http://test"
        ) as client:
            return await client.get(
                "/v1/portfolio",
                headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", "Bearer wrong")],
            )

    response = asyncio.run(request())

    assert response.status_code == 401
    assert repository.reads == 0


def test_resolved_order_timestamp_preserves_utc_instant() -> None:
    current = current_portfolio()
    order = current.snapshot.orders[0]
    resolved = replace(
        order,
        status=LimitOrderStatus.FILLED,
        updated_at=current.snapshot.confirmed_at,
        resolved_at=current.snapshot.confirmed_at,
    )
    snapshot = replace(current.snapshot, orders=(resolved,))

    response = get(app_with(FakeSnapshots(CurrentPortfolio(snapshot, current.revision))))

    assert response.status_code == 200
    assert response.json()["limit_orders"][0]["resolved_at"] == "2026-09-25T09:00:00.000000Z"


def test_authorized_response_preserves_full_state_and_exact_financial_strings() -> None:
    repository = FakeSnapshots(current_portfolio())

    response = get(app_with(repository))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {
        "revision",
        "snapshot_id",
        "confirmed_at",
        "accounts",
        "aggregate_positions",
        "financial_settings",
        "core_positions",
        "limit_orders",
    }
    assert body["revision"] == 7
    assert body["snapshot_id"] == str(SNAPSHOT_ID)
    assert body["confirmed_at"] == "2026-09-25T09:00:00.000000Z"
    assert body["accounts"] == [
        {
            "id": str(BINANCE_ID),
            "name": "Binance",
            "type": "binance",
            "positions": [
                {"symbol": "BTC", "amount": "0.019800000000000000"},
                {"symbol": "ETH", "amount": "3.450300000000000001"},
            ],
        },
        {
            "id": str(WALLET_ID),
            "name": "External Wallet",
            "type": "external_wallet",
            "positions": [
                {"symbol": "BTC", "amount": "0.000000000000000001"},
                {"symbol": "USDC", "amount": "1400.230000000000000001"},
            ],
        },
    ]
    assert body["aggregate_positions"] == [
        {"symbol": "BTC", "amount": "0.019800000000000001"},
        {"symbol": "ETH", "amount": "3.450300000000000001"},
        {"symbol": "USDC", "amount": "1400.230000000000000001"},
    ]
    assert body["financial_settings"] == {
        "monthly_expenses_usd": "250.000000000000000001",
        "target_expense_runway_months": "12.0",
    }
    assert body["core_positions"] == [
        {
            "symbol": "ETH",
            "hard_floor": "2.0",
            "preferred_quantity": "2.5",
            "policy_version": 1,
        }
    ]
    assert body["limit_orders"] == [
        {
            "id": str(ORDER_ID),
            "account_id": str(BINANCE_ID),
            "asset": "ETH",
            "side": "sell",
            "target_price": "76500.000000000000000001",
            "quantity_asset": "0.010000000000000001",
            "status": "open",
            "created_at": "2026-09-24T15:30:00.000000Z",
            "updated_at": "2026-09-24T15:30:00.000000Z",
            "resolved_at": None,
        }
    ]
    assert repository.reads == 1
    assert repository.writes == 0
    assert TOKEN not in response.text


def test_missing_current_is_sanitized_404_without_initialization() -> None:
    repository = FakeSnapshots(None)

    response = get(app_with(repository))

    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "portfolio_not_found", "message": "No confirmed portfolio exists."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert repository.writes == 0


def test_unexpected_repository_failure_is_sanitized_without_leaking_details() -> None:
    class FailingSnapshots(FakeSnapshots):
        async def get_current(self):
            raise RuntimeError("private URL, credentials, and portfolio payload")

    response = get(app_with(FailingSnapshots(None)))

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Portfolio could not be loaded."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text
    assert "credentials" not in response.text


@pytest.mark.parametrize("configured", [None, "", " ", "secret with spaces", "secret\n"])
def test_auth_configuration_fails_closed_without_rendering_token(configured: str | None) -> None:
    environment = {} if configured is None else {"ADVISOR_API_TOKEN": configured}
    with pytest.raises(ValueError, match="ADVISOR_API_TOKEN") as error:
        create_app(environment=environment, snapshots=FakeSnapshots(None))
    if configured and configured.strip():
        assert configured not in str(error.value)


def test_auth_settings_repr_never_contains_token() -> None:
    assert TOKEN not in repr(APITokenSettings.from_environment({"ADVISOR_API_TOKEN": TOKEN}))


def test_production_composition_requires_postgres_without_connecting(monkeypatch) -> None:
    calls = []

    def fake_create_storage(settings):
        calls.append(settings)

        class Bundle:
            snapshots = FakeSnapshots(None)
            analyses = object()

        return Bundle()

    monkeypatch.setattr("advanced_trading_advisor.http.app.create_storage", fake_create_storage)
    postgres = {
        "ADVISOR_API_TOKEN": TOKEN,
        "ADVISOR_STORAGE_BACKEND": "postgres",
        "ADVISOR_DATABASE_URL": "postgresql://synthetic:synthetic@localhost/synthetic",
    }
    app = create_app(environment=postgres)

    assert len(calls) == 1
    assert calls[0].backend.value == "postgres"
    assert get(app).status_code == 404
    for invalid in (
        {"ADVISOR_API_TOKEN": TOKEN},
        {**postgres, "ADVISOR_STORAGE_BACKEND": "sqlite"},
        {**postgres, "ADVISOR_DATABASE_URL": "not-a-url-with-private-credential"},
    ):
        with pytest.raises(RuntimeError, match="HTTP storage configuration invalid") as error:
            create_app(environment=invalid)
        assert "private-credential" not in str(error.value)
    assert len(calls) == 1


def test_only_portfolio_and_analysis_read_routes_are_added() -> None:
    app = app_with(FakeSnapshots(None))
    assert [(route.path, route.methods) for route in app.routes] == [
        ("/v1/portfolio", {"GET"}),
        ("/v1/analyses/latest", {"GET"}),
        ("/v1/analyses", {"GET"}),
        ("/v1/analyses/{run_id}", {"GET"}),
    ]
