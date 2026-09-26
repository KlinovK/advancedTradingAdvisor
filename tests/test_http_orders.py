"""Offline HTTP boundary tests for creating, cancelling and expiring spot limit orders."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.domain.persistence import CurrentPortfolio
from advanced_trading_advisor.http.app import create_app
from advanced_trading_advisor.portfolio.add_limit_order import AddLimitOrder
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
from advanced_trading_advisor.portfolio.update_limit_order_status import UpdateLimitOrderStatus

TOKEN = "synthetic-order-http-token"
NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
CONFIRMED_AT = NOW + timedelta(seconds=1)
INITIAL_ID = UUID("0d000000-0000-4000-8000-000000000001")
FIRST_ID = UUID("0d000000-0000-4000-8000-000000000002")
SECOND_ID = UUID("0d000000-0000-4000-8000-000000000003")
FIRST_ORDER_ID = UUID("0d000000-0000-4000-8000-000000000004")
TARGET_ORDER_ID = UUID("0d000000-0000-4000-8000-000000000005")
NEW_ORDER_ID = UUID("0d000000-0000-4000-8000-000000000006")
CREATE_SNAPSHOT_ID = UUID("0d000000-0000-4000-8000-000000000007")
TRANSITION_SNAPSHOT_ID = UUID("0d000000-0000-4000-8000-000000000008")
LATER_SNAPSHOT_ID = UUID("0d000000-0000-4000-8000-000000000009")
UNKNOWN_ID = UUID("0d000000-0000-4000-8000-000000000010")


def initial_current():
    accounts = tuple(
        Account(
            account_id,
            name,
            account_type,
            (AssetPosition("BTC", Decimal("1.00")), AssetPosition("USDC", Decimal("100.00"))),
        )
        for account_id, name, account_type in (
            (FIRST_ID, "Binance", AccountType.BINANCE),
            (SECOND_ID, "Wallet", AccountType.EXTERNAL_WALLET),
        )
    )
    orders = tuple(
        LimitOrder(
            order_id,
            "BTC",
            TradingSide.SELL,
            Decimal("65000.00"),
            Decimal("0.10"),
            LimitOrderStatus.OPEN,
            NOW - timedelta(days=1),
            NOW - timedelta(days=1),
            account_id=account_id,
        )
        for order_id, account_id in ((FIRST_ORDER_ID, FIRST_ID), (TARGET_ORDER_ID, SECOND_ID))
    )
    return CurrentPortfolio(
        ConfirmedPortfolioSnapshot(
            INITIAL_ID,
            NOW,
            Portfolio(accounts),
            FinancialSettings(Decimal("250.00"), Decimal("12")),
            orders,
            (CorePosition("BTC", Decimal("0.5"), Decimal("1.0")),),
        ),
        7,
    )


class FakeSnapshots:
    def __init__(self, current, *, maximum_reads=1, later_writer=False):
        self.current = current
        self.maximum_reads = maximum_reads
        self.later_writer = later_writer
        self.reads = 0
        self.writes = 0
        self.audit_updates = []
        self.mutation_result = None

    async def get_current(self):
        self.reads += 1
        assert self.reads <= self.maximum_reads, "Unexpected post-mutation read or retry."
        return self.current

    async def set_current(self, snapshot, *, expected_revision, update):
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


def app_with(repository, *, adder=None, updater=None, order_id=NEW_ORDER_ID):
    return create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN},
        snapshots=repository,
        add_limit_order=adder
        if adder is not None
        else AddLimitOrder(
            repository,
            now=lambda: CONFIRMED_AT,
            order_identity_factory=lambda: order_id,
            snapshot_identity_factory=lambda: CREATE_SNAPSHOT_ID,
        ),
        update_limit_order_status=updater
        if updater is not None
        else UpdateLimitOrderStatus(
            repository,
            now=lambda: CONFIRMED_AT,
            identity_factory=lambda: TRANSITION_SNAPSHOT_ID,
        ),
    )


def body(operation, **changes):
    values = {"expected_revision": 7}
    if operation == "create":
        values |= {
            "account_id": str(SECOND_ID),
            "asset": "BTC",
            "side": "buy",
            "target_price": "65000.123456789123456789",
            "quantity_asset": "0.123456789123456789",
        }
    return values | changes


def post(
    app, operation, *, payload=None, order_id=TARGET_ORDER_ID, authorization=f"Bearer {TOKEN}"
):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/v1/portfolio/orders"
            if operation != "create":
                path += f"/{order_id}/{operation}"
            return await client.post(
                path,
                json=body(operation) if payload is None else payload,
                headers={} if authorization is None else {"Authorization": authorization},
            )

    return asyncio.run(send())


def assert_unchanged(repository, before):
    assert repository.current == before
    assert repository.writes == 0
    assert repository.audit_updates == []


@pytest.mark.parametrize(
    "operation,side", [("create", "buy"), ("create", "sell"), ("cancel", None), ("expire", None)]
)
def test_exact_result_and_multi_account_ownership_even_after_later_writer(operation, side):
    before = initial_current()
    repository = FakeSnapshots(before, later_writer=True)
    payload = body(operation, side=side) if operation == "create" else body(operation)

    response = post(app_with(repository), operation, payload=payload)

    assert response.status_code == (201 if operation == "create" else 200)
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == repository.writes == 1
    assert len(repository.audit_updates) == 1
    snapshot_id = CREATE_SNAPSHOT_ID if operation == "create" else TRANSITION_SNAPSHOT_ID
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
    assert repository.current.revision == 9
    assert repository.current.snapshot.id == LATER_SNAPSHOT_ID
    saved = repository.mutation_result.snapshot
    assert saved.id == snapshot_id
    assert saved.portfolio == before.snapshot.portfolio
    assert saved.financial_settings == before.snapshot.financial_settings
    assert saved.core_positions == before.snapshot.core_positions
    assert [account["id"] for account in result["accounts"]] == [str(FIRST_ID), str(SECOND_ID)]
    assert result["aggregate_positions"] == [
        {"symbol": "BTC", "amount": "2.00"},
        {"symbol": "USDC", "amount": "200.00"},
    ]
    assert result["financial_settings"]["monthly_expenses_usd"] == "250.00"
    assert result["core_positions"][0]["hard_floor"] == "0.5"
    if operation == "create":
        assert saved.orders[:-1] == before.snapshot.orders
        order = saved.orders[-1]
        assert order.id == NEW_ORDER_ID
        assert order.account_id == SECOND_ID
        assert order.side is TradingSide(side)
        assert order.status is LimitOrderStatus.OPEN
        assert order.created_at == order.updated_at == CONFIRMED_AT
        assert order.resolved_at is None
        for field in ("target_price", "quantity_asset"):
            assert type(getattr(order, field)) is Decimal
            assert getattr(order, field).as_tuple() == Decimal(payload[field]).as_tuple()
            assert result["limit_orders"][-1][field] == payload[field]
        assert result["limit_orders"][-1]["id"] == str(NEW_ORDER_ID)
        assert result["limit_orders"][-1]["account_id"] == str(SECOND_ID)
        assert result["limit_orders"][-1]["status"] == "open"
    else:
        target = LimitOrderStatus.CANCELLED if operation == "cancel" else LimitOrderStatus.EXPIRED
        assert saved.orders[0] == before.snapshot.orders[0]
        assert saved.orders[1] == replace(
            before.snapshot.orders[1],
            status=target,
            updated_at=CONFIRMED_AT,
            resolved_at=CONFIRMED_AT,
        )
        assert result["limit_orders"][1]["id"] == str(TARGET_ORDER_ID)
        assert result["limit_orders"][1]["account_id"] == str(SECOND_ID)
        assert result["limit_orders"][1]["status"] == target.value
        assert result["limit_orders"][1]["resolved_at"] == "2026-09-26T12:00:01.000000Z"


@pytest.mark.parametrize("operation", ["create", "cancel", "expire"])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_authentication_precedes_use_case(operation, authorization):
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(app_with(repository), operation, authorization=authorization)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unchanged(repository, before)


@pytest.mark.parametrize("operation", ["create", "cancel", "expire"])
def test_unknown_account_or_order_is_404_without_mutation(operation):
    before = initial_current()
    repository = FakeSnapshots(before)
    payload = (
        body(operation, account_id=str(UNKNOWN_ID)) if operation == "create" else body(operation)
    )
    response = post(app_with(repository), operation, payload=payload, order_id=UNKNOWN_ID)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert_unchanged(repository, before)


@pytest.mark.parametrize("operation", ["create", "cancel", "expire"])
def test_missing_current_state_is_not_initialized(operation):
    repository = FakeSnapshots(None)
    response = post(app_with(repository), operation)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert repository.reads == 1
    assert_unchanged(repository, None)


@pytest.mark.parametrize("operation", ["create", "cancel", "expire"])
def test_stale_revision_fails_closed_without_retry_or_extra_read(operation):
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(app_with(repository), operation, payload=body(operation, expected_revision=6))
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "revision_conflict",
        "message": "Portfolio changed; reload before confirming.",
        "reload_required": True,
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert_unchanged(repository, before)


@pytest.mark.parametrize("operation", ["cancel", "expire"])
@pytest.mark.parametrize(
    "status", [LimitOrderStatus.CANCELLED, LimitOrderStatus.EXPIRED, LimitOrderStatus.FILLED]
)
def test_terminal_order_transition_is_state_conflict(operation, status):
    original = initial_current()
    terminal = replace(
        original.snapshot.orders[1], status=status, resolved_at=NOW - timedelta(days=1)
    )
    before = CurrentPortfolio(
        replace(original.snapshot, orders=(original.snapshot.orders[0], terminal)), 7
    )
    repository = FakeSnapshots(before)
    response = post(app_with(repository), operation)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert_unchanged(repository, before)


@pytest.mark.parametrize(
    "changes",
    [
        {"asset": "btc"},
        {"asset": "XRP"},
        {"asset": "bad symbol"},
        {"target_price": "0"},
        {"target_price": "-1"},
        {"target_price": "1e3"},
        {"quantity_asset": "0"},
        {"quantity_asset": "-1"},
        {"quantity_asset": "NaN"},
        {"account_id": str(UUID(int=0))},
    ],
)
def test_invalid_create_domain_input_is_sanitized_422(changes):
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(app_with(repository), "create", payload=body("create", **changes))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert response.headers["cache-control"] == "no-store"
    assert_unchanged(repository, before)


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": 0},
        {"expected_revision": "7"},
        {"account_id": "not-a-uuid"},
        {"side": "SHORT"},
        {"side": "BUY"},
        {"target_price": 65000},
        {"quantity_asset": 0.1},
        {"order_id": str(NEW_ORDER_ID)},
        {"status": "filled"},
        {"created_at": NOW.isoformat()},
        {"updated_at": NOW.isoformat()},
        {"resolved_at": NOW.isoformat()},
        {"settlement_asset": "USDC"},
        {"unknown": "field"},
    ],
)
def test_create_transport_rejects_invalid_types_and_server_owned_fields(changes):
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(app_with(repository), "create", payload=body("create", **changes))
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unchanged(repository, before)


@pytest.mark.parametrize("field", ["expected_revision", "account_id"])
def test_create_requires_revision_and_explicit_account(field):
    before = initial_current()
    repository = FakeSnapshots(before)
    payload = body("create")
    del payload[field]
    response = post(app_with(repository), "create", payload=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert repository.reads == 0
    assert_unchanged(repository, before)


@pytest.mark.parametrize("operation", ["cancel", "expire"])
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"expected_revision": 0},
        {"expected_revision": "7"},
        body("cancel", order_id=str(TARGET_ORDER_ID)),
        body("cancel", account_id=str(SECOND_ID)),
        body("cancel", target_status="filled"),
        body("cancel", status="open"),
        body("cancel", positions=[]),
        body("cancel", holdings=[]),
        body("cancel", settlement_asset="USDC"),
        body("cancel", execution_price="100"),
        body("cancel", fee="1"),
        body("cancel", updated_at=NOW.isoformat()),
        body("cancel", unexpected=True),
    ],
)
def test_lifecycle_body_allows_only_expected_revision(operation, payload):
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(app_with(repository), operation, payload=payload)
    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unchanged(repository, before)


@pytest.mark.parametrize("operation", ["cancel", "expire"])
def test_malformed_order_path_is_invalid_request(operation):
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(app_with(repository), operation, order_id="not-a-uuid")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    assert repository.reads == 0
    assert_unchanged(repository, before)


@pytest.mark.parametrize("operation", ["create", "cancel", "expire"])
def test_unexpected_error_is_sanitized_without_retry(operation):
    class FailingUseCase:
        calls = 0

        async def execute(self, command):
            self.calls += 1
            raise RuntimeError("private order and database credentials")

    before = initial_current()
    repository = FakeSnapshots(before)
    failing = FailingUseCase()
    app = app_with(
        repository,
        adder=failing if operation == "create" else None,
        updater=failing if operation != "create" else None,
    )
    response = post(app, operation)
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private order and database credentials" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert failing.calls == 1
    assert repository.reads == 0
    assert_unchanged(repository, before)


@pytest.mark.parametrize("operation", ["create", "cancel", "expire"])
def test_retry_with_original_revision_never_replays_command(operation):
    repository = FakeSnapshots(initial_current(), maximum_reads=2)
    app = app_with(repository)
    assert post(app, operation).status_code == (201 if operation == "create" else 200)
    retry = post(app, operation)
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "revision_conflict"
    assert repository.reads == 2
    assert repository.writes == 1
    assert len(repository.audit_updates) == 1


def test_identical_economic_terms_remain_allowed_with_distinct_order_identity():
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(
        app_with(repository),
        "create",
        payload=body("create", side="sell", target_price="65000.00", quantity_asset="0.10"),
    )
    assert response.status_code == 201
    assert repository.mutation_result.snapshot.orders[:2] == before.snapshot.orders
    assert repository.mutation_result.snapshot.orders[-1].id == NEW_ORDER_ID


def test_application_generated_duplicate_order_identity_retains_existing_rejection():
    before = initial_current()
    repository = FakeSnapshots(before)
    response = post(app_with(repository, order_id=TARGET_ORDER_ID), "create")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert_unchanged(repository, before)
