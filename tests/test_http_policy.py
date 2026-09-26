"""Offline ASGI tests for global financial settings and core-position policy."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.domain.persistence import CurrentPortfolio
from advanced_trading_advisor.http.app import create_app
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
from advanced_trading_advisor.portfolio.update_core_position import UpdateCorePosition
from advanced_trading_advisor.portfolio.update_financial_settings import UpdateFinancialSettings

TOKEN = "synthetic-policy-token"
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("0c000000-0000-4000-8000-000000000001")
FIRST_ID = UUID("0c000000-0000-4000-8000-000000000002")
SECOND_ID = UUID("0c000000-0000-4000-8000-000000000003")
ORDER_ID = UUID("0c000000-0000-4000-8000-000000000004")
SETTINGS_SNAPSHOT_ID = UUID("0c000000-0000-4000-8000-000000000005")
CORE_SNAPSHOT_ID = UUID("0c000000-0000-4000-8000-000000000006")
LATER_SNAPSHOT_ID = UUID("0c000000-0000-4000-8000-000000000007")


def initial_current() -> CurrentPortfolio:
    snapshot = ConfirmedPortfolioSnapshot(
        INITIAL_ID,
        NOW,
        Portfolio(
            (
                Account(
                    FIRST_ID,
                    "Binance",
                    AccountType.BINANCE,
                    (
                        AssetPosition("ETH", Decimal("1.25")),
                        AssetPosition("USDC", Decimal("100.00")),
                    ),
                ),
                Account(
                    SECOND_ID,
                    "Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (
                        AssetPosition("ETH", Decimal("1.25")),
                        AssetPosition("BTC", Decimal("0.50")),
                    ),
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
                account_id=SECOND_ID,
            ),
        ),
        (CorePosition("ETH", Decimal("2.0"), Decimal("2.25")),),
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
        self.audit_updates = []
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
        self.audit_updates.append(update)
        self.mutation_result = CurrentPortfolio(snapshot, self.current.revision + 1)
        self.current = (
            CurrentPortfolio(replace(snapshot, id=LATER_SNAPSHOT_ID), self.current.revision + 2)
            if self.later_writer
            else self.mutation_result
        )
        return self.mutation_result


def app_with(repository: FakeSnapshots, *, settings=None, core=None):
    settings_use_case = settings or UpdateFinancialSettings(
        repository,
        now=lambda: NOW + timedelta(seconds=1),
        identity_factory=lambda: SETTINGS_SNAPSHOT_ID,
    )
    core_use_case = core or UpdateCorePosition(
        repository,
        now=lambda: NOW + timedelta(seconds=1),
        identity_factory=lambda: CORE_SNAPSHOT_ID,
    )
    return create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN},
        snapshots=repository,
        update_financial_settings=settings_use_case,
        update_core_position=core_use_case,
    )


def body(kind, **changes):
    values = (
        {
            "expected_revision": 7,
            "monthly_expenses_usd": "200.123456789123456789",
            "target_expense_runway_months": "12.500000000000000001",
        }
        if kind == "settings"
        else {
            "expected_revision": 7,
            "hard_floor": "2.000000000000000001",
            "preferred_quantity": "2.500000000000000001",
        }
    )
    return values | changes


def put(app, kind, *, payload=None, symbol="ETH", authorization=f"Bearer {TOKEN}"):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {} if authorization is None else {"Authorization": authorization}
            path = (
                "/v1/portfolio/financial-settings"
                if kind == "settings"
                else f"/v1/portfolio/core-positions/{symbol}"
            )
            return await client.put(
                path, json=body(kind) if payload is None else payload, headers=headers
            )

    return asyncio.run(send())


def assert_unmodified(repository, before):
    assert repository.current == before
    assert repository.writes == 0
    assert repository.audit_updates == []


@pytest.mark.parametrize(
    "kind,snapshot_id", [("settings", SETTINGS_SNAPSHOT_ID), ("core", CORE_SNAPSHOT_ID)]
)
def test_success_returns_exact_n_plus_one_snapshot_even_after_later_writer(kind, snapshot_id):
    before = initial_current()
    repository = FakeSnapshots(before, later_writer=True)

    response = put(app_with(repository), kind)

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 1
    assert len(repository.audit_updates) == 1
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
    assert [account["id"] for account in result["accounts"]] == [str(FIRST_ID), str(SECOND_ID)]
    assert result["aggregate_positions"] == [
        {"symbol": "BTC", "amount": "0.50"},
        {"symbol": "ETH", "amount": "2.50"},
        {"symbol": "USDC", "amount": "100.00"},
    ]
    assert result["limit_orders"][0]["id"] == str(ORDER_ID)
    assert repository.mutation_result.snapshot.portfolio == before.snapshot.portfolio
    assert repository.mutation_result.snapshot.orders == before.snapshot.orders
    if kind == "settings":
        assert result["financial_settings"] == {
            "monthly_expenses_usd": "200.123456789123456789",
            "target_expense_runway_months": "12.500000000000000001",
        }
        assert repository.mutation_result.snapshot.core_positions == before.snapshot.core_positions
        settings = repository.mutation_result.snapshot.financial_settings
        assert type(settings.monthly_expenses_usd) is Decimal
        assert (
            settings.monthly_expenses_usd.as_tuple() == Decimal("200.123456789123456789").as_tuple()
        )
    else:
        assert result["core_positions"] == [
            {
                "symbol": "ETH",
                "hard_floor": "2.000000000000000001",
                "preferred_quantity": "2.500000000000000001",
                "policy_version": 1,
            }
        ]
        assert result["financial_settings"] == {
            "monthly_expenses_usd": "250.00",
            "target_expense_runway_months": "12",
        }
        assert (
            repository.mutation_result.snapshot.financial_settings
            == before.snapshot.financial_settings
        )
        core = repository.mutation_result.snapshot.core_positions[0]
        assert type(core.hard_floor) is Decimal
        assert core.hard_floor.as_tuple() == Decimal("2.000000000000000001").as_tuple()


def test_new_core_policy_is_upserted_without_changing_existing_core():
    before = initial_current()
    repository = FakeSnapshots(before)

    response = put(
        app_with(repository),
        "core",
        symbol="BTC",
        payload=body("core", hard_floor="0.25", preferred_quantity="0.50"),
    )

    assert response.status_code == 200
    assert response.json()["core_positions"] == [
        {"symbol": "ETH", "hard_floor": "2.0", "preferred_quantity": "2.25", "policy_version": 1},
        {"symbol": "BTC", "hard_floor": "0.25", "preferred_quantity": "0.50", "policy_version": 1},
    ]
    assert repository.mutation_result.snapshot.portfolio == before.snapshot.portfolio


def test_core_floor_uses_aggregate_holdings_across_accounts():
    before = initial_current()
    repository = FakeSnapshots(before)

    response = put(
        app_with(repository),
        "core",
        payload=body("core", hard_floor="2.50", preferred_quantity="2.50"),
    )

    assert response.status_code == 200
    assert response.json()["core_positions"][0]["hard_floor"] == "2.50"
    assert all(
        account.positions[0].amount == Decimal("1.25")
        for account in before.snapshot.portfolio.accounts
    )

    other = FakeSnapshots(before)
    invalid = put(
        app_with(other),
        "core",
        payload=body(
            "core", hard_floor="2.500000000000000001", preferred_quantity="2.500000000000000001"
        ),
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "validation_error"
    assert other.reads == 1
    assert_unmodified(other, before)


@pytest.mark.parametrize("kind", ["settings", "core"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_bearer_authentication_is_required(kind, authorization):
    before = initial_current()
    repository = FakeSnapshots(before)

    response = put(app_with(repository), kind, authorization=authorization)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unmodified(repository, before)


@pytest.mark.parametrize("kind", ["settings", "core"])
def test_missing_current_portfolio_is_404_without_mutation(kind):
    repository = FakeSnapshots(None)

    response = put(app_with(repository), kind)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert_unmodified(repository, None)


@pytest.mark.parametrize("kind", ["settings", "core"])
def test_stale_revision_is_reload_conflict_without_retry_or_mutation(kind):
    before = initial_current()
    repository = FakeSnapshots(before)

    response = put(app_with(repository), kind, payload=body(kind, expected_revision=6))

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "revision_conflict",
        "message": "Portfolio changed; reload before confirming.",
        "reload_required": True,
    }
    assert "current_revision" not in response.text
    assert repository.reads == 1
    assert_unmodified(repository, before)


@pytest.mark.parametrize("kind", ["settings", "core"])
def test_noop_is_state_conflict_without_mutation(kind):
    before = initial_current()
    repository = FakeSnapshots(before)
    payload = (
        body("settings", monthly_expenses_usd="250.0", target_expense_runway_months="12.00")
        if kind == "settings"
        else body("core", hard_floor="2.0", preferred_quantity="2.25")
    )

    response = put(app_with(repository), kind, payload=payload)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    assert repository.reads == 1
    assert_unmodified(repository, before)


@pytest.mark.parametrize(
    "kind,payload,symbol",
    [
        ("settings", body("settings", monthly_expenses_usd="0"), "ETH"),
        ("settings", body("settings", target_expense_runway_months="-1"), "ETH"),
        ("core", body("core", hard_floor="0"), "ETH"),
        ("core", body("core", preferred_quantity="0"), "ETH"),
        ("core", body("core", hard_floor="2.4", preferred_quantity="2.3"), "ETH"),
        ("core", body("core"), "XRP"),
        ("core", body("core"), "eth"),
        ("core", body("core"), "bad symbol"),
    ],
)
def test_domain_or_symbol_validation_is_sanitized_422_without_mutation(kind, payload, symbol):
    before = initial_current()
    repository = FakeSnapshots(before)

    response = put(app_with(repository), kind, payload=payload, symbol=symbol)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.headers["cache-control"] == "no-store"
    assert_unmodified(repository, before)


@pytest.mark.parametrize(
    "kind,payload",
    [
        ("settings", {"monthly_expenses_usd": "200", "target_expense_runway_months": "12"}),
        ("settings", body("settings", expected_revision=0)),
        ("settings", body("settings", expected_revision="7")),
        ("settings", body("settings", monthly_expenses_usd=200.5)),
        ("settings", body("settings", extra="not-accepted")),
        ("core", {"hard_floor": "2", "preferred_quantity": "2.5"}),
        ("core", body("core", expected_revision=-1)),
        ("core", body("core", hard_floor=2.0)),
        ("core", body("core", symbol="BTC")),
    ],
)
def test_malformed_body_uses_existing_invalid_request_envelope(kind, payload):
    before = initial_current()
    repository = FakeSnapshots(before)

    response = put(app_with(repository), kind, payload=payload)

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unmodified(repository, before)


@pytest.mark.parametrize("kind", ["settings", "core"])
def test_malformed_decimal_string_is_sanitized_before_any_read(kind):
    before = initial_current()
    repository = FakeSnapshots(before)
    payload = (
        body("settings", monthly_expenses_usd="1e2")
        if kind == "settings"
        else body("core", hard_floor="2,1")
    )

    response = put(app_with(repository), kind, payload=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert repository.reads == 0
    assert_unmodified(repository, before)


@pytest.mark.parametrize("kind", ["settings", "core"])
def test_unexpected_use_case_failure_is_sanitized(kind):
    class FailingUseCase:
        async def execute(self, command):
            raise RuntimeError("private portfolio data and database credentials")

    before = initial_current()
    repository = FakeSnapshots(before)
    app = app_with(
        repository,
        settings=FailingUseCase() if kind == "settings" else None,
        core=FailingUseCase() if kind == "core" else None,
    )

    response = put(app, kind)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private portfolio data and database credentials" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unmodified(repository, before)


@pytest.mark.parametrize("kind", ["settings", "core"])
def test_retry_at_original_revision_fails_closed_without_replay(kind):
    repository = FakeSnapshots(initial_current(), maximum_reads=2)
    app = app_with(repository)

    first = put(app, kind)
    retry = put(app, kind)

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "revision_conflict"
    assert repository.reads == 2
    assert repository.writes == 1
    assert len(repository.audit_updates) == 1
