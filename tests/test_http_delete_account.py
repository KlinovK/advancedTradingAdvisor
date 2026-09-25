"""Offline ASGI tests for removing one confirmed portfolio account."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.domain.persistence import CurrentPortfolio, UpdateType
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
from advanced_trading_advisor.portfolio.remove_account import RemoveAccount

TOKEN = "synthetic-account-removal-token"
NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
INITIAL_ID = UUID("0b000000-0000-4000-8000-000000000001")
MANUAL_ID = UUID("0b000000-0000-4000-8000-000000000002")
WALLET_ID = UUID("0b000000-0000-4000-8000-000000000003")
TARGET_ID = UUID("0b000000-0000-4000-8000-000000000004")
ORDER_ID = UUID("0b000000-0000-4000-8000-000000000005")
NEW_SNAPSHOT_ID = UUID("0b000000-0000-4000-8000-000000000006")
LATER_SNAPSHOT_ID = UUID("0b000000-0000-4000-8000-000000000007")
UNKNOWN_ID = UUID("0b000000-0000-4000-8000-000000000008")


def initial_current() -> CurrentPortfolio:
    snapshot = ConfirmedPortfolioSnapshot(
        INITIAL_ID,
        NOW,
        Portfolio(
            (
                Account(
                    MANUAL_ID,
                    "Manual",
                    AccountType.MANUAL,
                    (
                        AssetPosition("BTC", Decimal("1.00")),
                        AssetPosition("USDC", Decimal("100.00")),
                    ),
                ),
                Account(
                    WALLET_ID,
                    "Wallet",
                    AccountType.EXTERNAL_WALLET,
                    (AssetPosition("ETH", Decimal("2.0")),),
                ),
                Account(
                    TARGET_ID,
                    "Other exchange",
                    AccountType.OTHER_EXCHANGE,
                    (AssetPosition("SOL", Decimal("3.5")),),
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
        self.audit_updates = []
        self.mutation_result = None

    async def get_current(self) -> CurrentPortfolio | None:
        self.reads += 1
        assert self.reads <= self.maximum_reads, "Unexpected post-mutation read."
        return self.current

    async def set_current(self, snapshot, *, expected_revision, update) -> CurrentPortfolio:
        self.writes += 1
        assert self.current is not None
        assert expected_revision == self.current.revision
        assert update.previous_snapshot_id == self.current.snapshot.id
        assert update.new_snapshot_id == snapshot.id
        assert update.update_type is UpdateType.ACCOUNT_REMOVED
        self.audit_updates.append(update)
        self.mutation_result = CurrentPortfolio(snapshot, self.current.revision + 1)
        self.current = (
            CurrentPortfolio(replace(snapshot, id=LATER_SNAPSHOT_ID), self.current.revision + 2)
            if self.later_writer
            else self.mutation_result
        )
        return self.mutation_result


def app_with(repository: FakeSnapshots, *, remover=None):
    use_case = remover or RemoveAccount(
        repository,
        now=lambda: NOW + timedelta(seconds=1),
        identity_factory=lambda: NEW_SNAPSHOT_ID,
    )
    return create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN},
        snapshots=repository,
        remove_account=use_case,
    )


def delete(app, *, account_id=TARGET_ID, body=None, authorization=f"Bearer {TOKEN}"):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {} if authorization is None else {"Authorization": authorization}
            return await client.request(
                "DELETE",
                f"/v1/portfolio/accounts/{account_id}",
                json={"expected_revision": 7} if body is None else body,
                headers=headers,
            )

    return asyncio.run(send())


def assert_unchanged(repository, before):
    assert repository.current == before
    assert repository.writes == 0
    assert repository.audit_updates == []


def test_delete_returns_exact_n_plus_one_snapshot_without_post_success_read():
    before = initial_current()
    repository = FakeSnapshots(before, later_writer=True)

    response = delete(app_with(repository))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1  # A second load would fail.
    assert repository.writes == 1
    assert len(repository.audit_updates) == 1
    assert repository.mutation_result.revision == 8
    assert repository.mutation_result.snapshot.id == NEW_SNAPSHOT_ID
    assert repository.current.revision == 9  # Simulated later writer cannot change the response.
    assert repository.current.snapshot.id == LATER_SNAPSHOT_ID
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
    assert body["revision"] == 8
    assert body["snapshot_id"] == str(NEW_SNAPSHOT_ID)
    assert [account["id"] for account in body["accounts"]] == [str(MANUAL_ID), str(WALLET_ID)]
    assert (
        repository.mutation_result.snapshot.portfolio.accounts
        == (before.snapshot.portfolio.accounts[:2])
    )
    assert body["aggregate_positions"] == [
        {"symbol": "BTC", "amount": "1.00"},
        {"symbol": "ETH", "amount": "2.0"},
        {"symbol": "USDC", "amount": "100.00"},
    ]
    assert body["financial_settings"] == {
        "monthly_expenses_usd": "250.00",
        "target_expense_runway_months": "12",
    }
    assert body["core_positions"][0]["symbol"] == "ETH"
    assert body["limit_orders"][0]["id"] == str(ORDER_ID)
    assert repository.mutation_result.snapshot.orders == before.snapshot.orders
    assert repository.mutation_result.snapshot.core_positions == before.snapshot.core_positions
    assert (
        repository.mutation_result.snapshot.financial_settings == before.snapshot.financial_settings
    )


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_delete_requires_existing_bearer_authentication(authorization):
    before = initial_current()
    repository = FakeSnapshots(before)

    response = delete(app_with(repository), authorization=authorization)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unchanged(repository, before)


@pytest.mark.parametrize(
    "account_id,request_body",
    [
        ("not-a-uuid", {"expected_revision": 7}),
        (TARGET_ID, {}),
        (TARGET_ID, {"expected_revision": 0}),
        (TARGET_ID, {"expected_revision": "7"}),
        (TARGET_ID, {"expected_revision": 7, "account_id": str(TARGET_ID)}),
        (TARGET_ID, {"expected_revision": 7, "unexpected": True}),
        (TARGET_ID, ["not-an-object"]),
    ],
)
def test_malformed_path_or_body_is_sanitized_and_does_not_write(account_id, request_body):
    before = initial_current()
    repository = FakeSnapshots(before)

    response = delete(app_with(repository), account_id=account_id, body=request_body)

    assert response.status_code == 422
    assert response.json() == {
        "error": {"code": "invalid_request", "message": "Invalid request parameters."}
    }
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unchanged(repository, before)


def test_nil_uuid_remains_domain_validation_not_resource_not_found():
    before = initial_current()
    repository = FakeSnapshots(before)

    response = delete(app_with(repository), account_id=UUID(int=0))

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert repository.reads == 0
    assert_unchanged(repository, before)


def test_unknown_account_is_404_without_state_mutation():
    before = initial_current()
    repository = FakeSnapshots(before)

    response = delete(app_with(repository), account_id=UNKNOWN_ID)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert_unchanged(repository, before)


def test_stale_revision_is_reload_conflict_without_retry_or_state_mutation():
    before = initial_current()
    repository = FakeSnapshots(before)

    response = delete(app_with(repository), body={"expected_revision": 6})

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "revision_conflict",
        "message": "Portfolio changed; reload before confirming.",
        "reload_required": True,
    }
    assert response.headers["cache-control"] == "no-store"
    assert "current_revision" not in response.text
    assert repository.reads == 1
    assert_unchanged(repository, before)


def test_last_account_conflict_leaves_confirmed_state_unchanged():
    original = initial_current()
    snapshot = replace(
        original.snapshot,
        portfolio=Portfolio((original.snapshot.portfolio.accounts[0],)),
        orders=(),
        core_positions=(),
    )
    before = CurrentPortfolio(snapshot, original.revision)
    repository = FakeSnapshots(before)

    response = delete(app_with(repository), account_id=MANUAL_ID)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert_unchanged(repository, before)
    assert repository.current.snapshot.portfolio.accounts[0].id == MANUAL_ID


def test_referenced_account_conflict_retains_account_order_and_state():
    before = initial_current()
    repository = FakeSnapshots(before)

    response = delete(app_with(repository), account_id=WALLET_ID)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "state_conflict"
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 1
    assert_unchanged(repository, before)
    assert repository.current.snapshot.orders == before.snapshot.orders
    assert repository.current.snapshot.portfolio.accounts == before.snapshot.portfolio.accounts


def test_unexpected_use_case_failure_is_sanitized():
    class FailingRemoveAccount:
        async def execute(self, command):
            raise RuntimeError("private account state and database credentials")

    before = initial_current()
    repository = FakeSnapshots(before)

    response = delete(app_with(repository, remover=FailingRemoveAccount()))

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"
    assert "private account state and database credentials" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert repository.reads == 0
    assert_unchanged(repository, before)


def test_retry_of_committed_delete_at_original_revision_fails_closed():
    repository = FakeSnapshots(initial_current(), maximum_reads=2)
    app = app_with(repository)

    first = delete(app)
    retry = delete(app)

    assert first.status_code == 200
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "revision_conflict"
    assert repository.reads == 2
    assert repository.writes == 1
    assert len(repository.audit_updates) == 1
