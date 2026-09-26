"""Offline ASGI tests for atomic manual fill confirmation, never exchange execution."""

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import httpx
import pytest

from advanced_trading_advisor.domain.persistence import (
    CurrentPortfolio,
    RevisionConflictError,
    UpdateType,
)
from advanced_trading_advisor.http.app import create_app
from advanced_trading_advisor.http.dtos import CurrentPortfolioDTO
from advanced_trading_advisor.portfolio.confirm_limit_order_filled import ConfirmLimitOrderFilled
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

TOKEN = "synthetic-fill-http-token"
NOW = datetime(2020, 1, 1, 12, tzinfo=UTC)
FILLED_AT = NOW + timedelta(seconds=1)
INITIAL_ID, OWNER_ID, OTHER_ID, ORDER_ID, OTHER_ORDER_ID, FILL_ID, LATER_ID, UNKNOWN_ID = (
    UUID(f"0e000000-0000-4000-8000-{number:012d}") for number in range(1, 9)
)


def positions(**amounts):
    return tuple(AssetPosition(symbol, Decimal(amount)) for symbol, amount in amounts.items())


def initial_current(side=TradingSide.BUY, status=LimitOrderStatus.OPEN):
    orders = tuple(
        LimitOrder(
            order_id,
            "BTC",
            side,
            Decimal("100"),
            Decimal("0.1"),
            status if order_id == ORDER_ID else LimitOrderStatus.OPEN,
            NOW,
            NOW,
            resolved_at=NOW
            if order_id == ORDER_ID and status is not LimitOrderStatus.OPEN
            else None,
            account_id=account_id,
        )
        for order_id, account_id in ((OTHER_ORDER_ID, OTHER_ID), (ORDER_ID, OWNER_ID))
    )
    return CurrentPortfolio(
        ConfirmedPortfolioSnapshot(
            INITIAL_ID,
            NOW,
            Portfolio(
                (
                    Account(
                        OTHER_ID,
                        "Wallet",
                        AccountType.EXTERNAL_WALLET,
                        positions(BTC="1.5", USDC="200"),
                    ),
                    Account(
                        OWNER_ID,
                        "Exchange",
                        AccountType.BINANCE,
                        positions(BTC="1", USDC="100", USDT="100", SOL="2"),
                    ),
                )
            ),
            FinancialSettings(Decimal("250"), Decimal("12")),
            orders,
            (CorePosition("BTC", Decimal("2"), Decimal("2.5")),),
        ),
        7,
    )


class Snapshots:
    def __init__(self, current, *, later_writer=False, cas_conflict=False, max_reads=1):
        self.current = current
        self.later_writer = later_writer
        self.cas_conflict = cas_conflict
        self.max_reads = max_reads
        self.reads = 0
        self.attempts = 0
        self.saved = []
        self.updates = []

    async def get_current(self):
        self.reads += 1
        assert self.reads <= self.max_reads, "Unexpected second read or automatic retry."
        return self.current

    async def set_current(self, snapshot, *, expected_revision, update):
        self.attempts += 1
        if self.cas_conflict or expected_revision != self.current.revision:
            raise RevisionConflictError("synthetic private CAS details")
        assert update.previous_snapshot_id == self.current.snapshot.id
        assert update.new_snapshot_id == snapshot.id
        result = CurrentPortfolio(snapshot, expected_revision + 1)
        self.saved.append(result)
        self.updates.append(update)
        self.current = (
            CurrentPortfolio(replace(snapshot, id=LATER_ID), expected_revision + 2)
            if self.later_writer
            else result
        )
        return result


class CapturingConfirmer:
    def __init__(self, repository):
        self.commands = []
        self.delegate = ConfirmLimitOrderFilled(
            repository, now=lambda: FILLED_AT, identity_factory=lambda: FILL_ID
        )

    async def execute(self, command):
        self.commands.append(command)
        return await self.delegate.execute(command)


def app_with(repository, confirmer=None):
    return create_app(
        environment={"ADVISOR_API_TOKEN": TOKEN},
        snapshots=repository,
        confirm_limit_order_filled=confirmer or CapturingConfirmer(repository),
    )


def body(side=TradingSide.BUY, settlement="USDC", **changes):
    return {
        "expected_revision": 7,
        "settlement_asset": settlement,
        "positions": [
            {
                "symbol": "BTC",
                "amount": "1.123456789123456789" if side is TradingSide.BUY else "0.75",
            },
            {
                "symbol": settlement,
                "amount": "80.123456789123456789"
                if side is TradingSide.BUY
                else "123.456789123456789",
            },
        ],
    } | changes


def post(app, payload=None, *, order_id=ORDER_ID, authorization=f"Bearer {TOKEN}"):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post(
                f"/v1/portfolio/orders/{order_id}/confirm-filled",
                json=body() if payload is None else payload,
                headers={} if authorization is None else {"Authorization": authorization},
            )

    return asyncio.run(send())


def assert_rejected(response, repository, before, status, code):
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert repository.current == before
    assert repository.saved == []
    assert repository.updates == []


@pytest.mark.parametrize("side", [TradingSide.BUY, TradingSide.SELL])
@pytest.mark.parametrize("settlement", ["USDC", "USDT"])
def test_atomic_fill_exact_positions_and_result_despite_later_writer(side, settlement):
    before = initial_current(side)
    repository = Snapshots(before, later_writer=True)
    confirmer = CapturingConfirmer(repository)
    payload = body(side, settlement)
    response = post(app_with(repository, confirmer), payload)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert len(confirmer.commands) == repository.reads == repository.attempts == 1
    command = confirmer.commands[0]
    assert command.order_id == ORDER_ID
    assert command.settlement_asset == settlement
    assert command.expected_revision == 7
    assert isinstance(command.positions, tuple)
    for position, supplied in zip(command.positions, payload["positions"], strict=True):
        assert position.symbol == supplied["symbol"]
        assert type(position.amount) is Decimal
        assert position.amount.as_tuple() == Decimal(supplied["amount"]).as_tuple()

    saved = repository.saved[0]
    snapshot = saved.snapshot
    assert saved.revision == 8
    assert snapshot.id == FILL_ID
    assert snapshot.confirmed_at == FILLED_AT
    assert repository.current.revision == 9
    assert repository.current.snapshot.id == LATER_ID
    assert response.json() == json.loads(json.dumps(asdict(CurrentPortfolioDTO.from_domain(saved))))
    assert snapshot.portfolio.accounts[0] == before.snapshot.portfolio.accounts[0]
    assert snapshot.portfolio.accounts[1] == replace(
        before.snapshot.portfolio.accounts[1], positions=command.positions
    )
    # Complete replacement removes SOL and the unselected stable; HTTP never merges them back.
    assert response.json()["accounts"][1]["positions"] == payload["positions"]
    assert snapshot.financial_settings == before.snapshot.financial_settings
    assert snapshot.core_positions == before.snapshot.core_positions
    aggregate = {p.symbol: p.amount for p in snapshot.portfolio.positions}
    assert aggregate["BTC"] == Decimal("1.5") + command.positions[0].amount
    assert aggregate["BTC"] >= Decimal("2")
    # SELL owner balance is below the core floor, but aggregate holdings satisfy it.
    assert snapshot.orders[0] == before.snapshot.orders[0]
    assert snapshot.orders[1] == replace(
        before.snapshot.orders[1],
        status=LimitOrderStatus.FILLED,
        updated_at=FILLED_AT,
        resolved_at=FILLED_AT,
    )
    assert len(repository.updates) == 1
    assert repository.updates[0].update_type is UpdateType.ORDER_FILLED
    assert repository.updates[0].previous_snapshot_id == INITIAL_ID
    assert repository.updates[0].new_snapshot_id == FILL_ID
    # Actual supplied balance movement differs from planned target_price * quantity_asset.
    order = before.snapshot.orders[1]
    assert (
        abs(Decimal("100") - command.positions[1].amount)
        != order.target_price * order.quantity_asset
    )


@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token"])
def test_auth_before_use_case(authorization):
    before = initial_current()
    repository = Snapshots(before)
    confirmer = CapturingConfirmer(repository)
    response = post(app_with(repository, confirmer), authorization=authorization)
    assert_rejected(response, repository, before, 401, "unauthorized")
    assert confirmer.commands == []
    assert repository.reads == 0


def test_missing_current_is_not_initialized():
    repository = Snapshots(None)
    assert_rejected(post(app_with(repository)), repository, None, 404, "resource_not_found")


def test_unknown_order_is_404_and_atomic():
    before = initial_current()
    repository = Snapshots(before)
    response = post(app_with(repository), order_id=UNKNOWN_ID)
    assert_rejected(response, repository, before, 404, "resource_not_found")


@pytest.mark.parametrize("at_commit", [False, True])
def test_revision_conflict_never_retries_or_mutates(at_commit):
    before = initial_current()
    repository = Snapshots(before, cas_conflict=at_commit)
    response = post(app_with(repository), body(expected_revision=7 if at_commit else 6))
    assert_rejected(response, repository, before, 409, "revision_conflict")
    assert response.json()["error"]["reload_required"] is True
    assert repository.reads == 1
    assert repository.attempts == int(at_commit)


@pytest.mark.parametrize(
    "status", [LimitOrderStatus.CANCELLED, LimitOrderStatus.EXPIRED, LimitOrderStatus.FILLED]
)
def test_terminal_order_cannot_be_rewritten(status):
    before = initial_current(status=status)
    repository = Snapshots(before)
    assert_rejected(post(app_with(repository)), repository, before, 409, "state_conflict")
    assert repository.attempts == 0


@pytest.mark.parametrize(
    "side,btc,cash",
    [
        (TradingSide.BUY, "1", "80"),
        (TradingSide.BUY, "1.2", "100"),
        (TradingSide.SELL, "1", "120"),
        (TradingSide.SELL, "0.75", "100"),
        (TradingSide.SELL, "0.49", "120"),  # Global BTC 1.99 breaches hard floor 2.
    ],
)
def test_reconciliation_and_global_core_rejections_are_atomic(side, btc, cash):
    before = initial_current(side)
    repository = Snapshots(before)
    payload = body(
        side, positions=[{"symbol": "BTC", "amount": btc}, {"symbol": "USDC", "amount": cash}]
    )
    response = post(app_with(repository), payload)
    assert_rejected(response, repository, before, 422, "validation_error")
    assert repository.attempts == 0


@pytest.mark.parametrize("symbol", ["usdc", " USDC", "BTC", "DAI", "", "USDC "])
def test_settlement_must_be_explicit_canonical_supported_stable(symbol):
    before = initial_current()
    repository = Snapshots(before)
    response = post(app_with(repository), body(settlement_asset=symbol))
    assert_rejected(response, repository, before, 422, "validation_error")
    assert repository.reads == 0


@pytest.mark.parametrize("amount", ["0", "-1", "NaN", "1e3", "1,5", ""])
def test_invalid_decimal_is_sanitized_before_execution(amount):
    before = initial_current()
    repository = Snapshots(before)
    confirmer = CapturingConfirmer(repository)
    payload = body()
    payload["positions"][0]["amount"] = amount
    response = post(app_with(repository, confirmer), payload)
    assert_rejected(response, repository, before, 422, "validation_error")
    assert confirmer.commands == []


@pytest.mark.parametrize(
    "values",
    [
        [],
        [{"symbol": "btc", "amount": "1.2"}],
        [{"symbol": "BTC", "amount": "1.2"}, {"symbol": "BTC", "amount": "2"}],
    ],
)
def test_invalid_complete_positions_are_atomic(values):
    before = initial_current()
    repository = Snapshots(before)
    response = post(app_with(repository), body(positions=values))
    assert_rejected(response, repository, before, 422, "validation_error")


@pytest.mark.parametrize("field", ["expected_revision", "settlement_asset", "positions"])
def test_required_fields(field):
    payload = body()
    del payload[field]
    assert_transport_rejected(payload)


def assert_transport_rejected(payload, *, order_id=ORDER_ID):
    before = initial_current()
    repository = Snapshots(before)
    confirmer = CapturingConfirmer(repository)
    response = post(app_with(repository, confirmer), payload, order_id=order_id)
    assert_rejected(response, repository, before, 422, "invalid_request")
    assert confirmer.commands == []
    assert repository.reads == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_revision": 0},
        {"expected_revision": -1},
        {"expected_revision": True},
        {"expected_revision": "7"},
        {"expected_revision": 7.0},
        {"settlement_asset": None},
        {"positions": None},
        {"positions": {}},
        {"positions": [{"symbol": "BTC", "amount": 1.2}]},
        {"positions": [{"symbol": "BTC", "amount": "1.2", "fee": "1"}]},
        {"positions": [{"symbol": "BTC"}]},
        {"order_id": str(ORDER_ID)},
        {"account_id": str(OTHER_ID)},
        {"status": "filled"},
        {"target_status": "filled"},
        {"execution_price": "100"},
        {"fill_price": "100"},
        {"fee": "1"},
        {"proceeds": "10"},
        {"created_at": NOW.isoformat()},
        {"updated_at": NOW.isoformat()},
        {"resolved_at": NOW.isoformat()},
        {"unexpected": True},
    ],
)
def test_strict_transport_rejects_fields_and_types(changes):
    assert_transport_rejected(body(**changes))


def test_malformed_path_does_not_execute():
    assert_transport_rejected(body(), order_id="not-a-uuid")


def test_unexpected_error_is_sanitized_and_not_retried():
    class FailingConfirmer:
        calls = 0

        async def execute(self, command):
            self.calls += 1
            raise RuntimeError("private holdings and credentials")

    before = initial_current()
    repository = Snapshots(before)
    confirmer = FailingConfirmer()
    response = post(app_with(repository, confirmer))
    assert_rejected(response, repository, before, 500, "internal_error")
    assert "private holdings and credentials" not in response.text
    assert confirmer.calls == 1
    assert repository.reads == 0


def test_resubmitting_original_revision_does_not_replay_fill():
    repository = Snapshots(initial_current(), max_reads=2)
    app = app_with(repository)
    assert post(app).status_code == 200
    confirmed = repository.current
    response = post(app)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "revision_conflict"
    assert repository.current == confirmed
    assert repository.reads == 2
    assert repository.attempts == len(repository.saved) == len(repository.updates) == 1


def test_default_composition_uses_existing_fill_use_case():
    repository = Snapshots(initial_current())
    app = create_app(environment={"ADVISOR_API_TOKEN": TOKEN}, snapshots=repository)
    response = post(app)
    assert response.status_code == 200
    assert repository.reads == repository.attempts == 1
    assert repository.updates[0].update_type is UpdateType.ORDER_FILLED
