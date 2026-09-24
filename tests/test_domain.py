from dataclasses import FrozenInstanceError, fields, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from advanced_trading_advisor.analysis.models import AnalysisRun, AnalysisRunStatus
from advanced_trading_advisor.analysis.recommendations import ActionType, Recommendation
from advanced_trading_advisor.domain.numeric import parse_decimal, parse_symbol
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.portfolio.models import (
    DEFAULT_MANUAL_ACCOUNT_ID,
    DEFAULT_MANUAL_ACCOUNT_NAME,
    Account,
    AccountType,
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    FinancialSettings,
    LimitOrder,
    LimitOrderStatus,
    Portfolio,
    StablecoinClassification,
    TradingSide,
)

D = Decimal
NOW = datetime(2026, 9, 9, tzinfo=UTC)


def order(**changes):
    args = dict(
        id=uuid4(),
        asset="BTC",
        side=TradingSide.BUY,
        target_price=D("60000"),
        quantity_asset=D("0.01"),
        status=LimitOrderStatus.OPEN,
        created_at=NOW,
        updated_at=NOW,
    )
    return LimitOrder(**(args | changes))


def snapshot(**changes):
    args = dict(
        id=uuid4(),
        confirmed_at=NOW,
        portfolio=Portfolio(
            (
                Account(
                    DEFAULT_MANUAL_ACCOUNT_ID,
                    DEFAULT_MANUAL_ACCOUNT_NAME,
                    AccountType.MANUAL,
                    (AssetPosition("BTC", D("0.01")),),
                ),
            )
        ),
        financial_settings=FinancialSettings(D("1000"), D("6")),
        orders=(),
    )
    return ConfirmedPortfolioSnapshot(**(args | changes))


def recommendation(action=ActionType.HOLD, **changes):
    args = dict(
        recommendation_id=uuid4(),
        asset="BTC",
        action_type=action,
        priority=1,
        reason="Review allocation.",
    )
    return Recommendation(**(args | changes))


@pytest.mark.parametrize("value", ["", "btc", " BTC", "BTC/USD", "_BTC", "B T", "🪙", "A" * 21, 1])
def test_bad_symbol(value):
    with pytest.raises(DomainValidationError):
        AssetPosition(value, D("1"))


@pytest.mark.parametrize(
    "value",
    [D("0"), D("-1"), D("NaN"), D("sNaN"), D("Infinity"), D("-Infinity"), 1.0, 1, True, "1", None],
)
def test_financial_boundaries(value):
    for create in (
        lambda: AssetPosition("BTC", value),
        lambda: FinancialSettings(value, D("6")),
        lambda: FinancialSettings(D("1000"), value),
        lambda: order(target_price=value),
        lambda: order(quantity_asset=value, side=TradingSide.SELL),
    ):
        with pytest.raises(DomainValidationError):
            create()


def test_parsing_is_exact_and_explicit():
    text = "0.12345678901234567890123456789012345678901234567890"
    assert str(parse_decimal(" " + text + " ")) == text
    assert parse_symbol(" btc ") == "BTC"
    assert parse_decimal("-1") == D("-1")
    with pytest.raises(DomainValidationError):
        parse_decimal("-1", positive=True)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "NaN",
        "Infinity",
        "1e3",
        "1,5",
        "1 000",
        "01",
        "+1",
        ".",
        "--1",
        "1.2.3",
        1.5,
        D("1"),
        None,
    ],
)
def test_invalid_numeric_text(value):
    with pytest.raises(DomainValidationError):
        parse_decimal(value)


def test_portfolio_duplicates_and_collection_types():
    p = AssetPosition("BTC", D("1"))
    with pytest.raises(DomainValidationError):
        Account(DEFAULT_MANUAL_ACCOUNT_ID, "Manual", AccountType.MANUAL, (p, p))
    with pytest.raises(DomainValidationError):
        Portfolio([p])
    with pytest.raises(DomainValidationError):
        Portfolio((p,))
    with pytest.raises(DomainValidationError):
        Portfolio(("BTC",))
    assert Portfolio(()).positions == ()


def test_confirmed_state_is_deeply_immutable():
    state = snapshot(orders=(order(),))
    with pytest.raises(FrozenInstanceError):
        state.portfolio.positions[0].amount = D("9")
    with pytest.raises(FrozenInstanceError):
        state.orders[0].status = LimitOrderStatus.FILLED
    with pytest.raises(FrozenInstanceError):
        state.confirmed_at = NOW
    recommendation()
    AnalysisRun.start(state.id, NOW)
    assert state.portfolio.positions[0].amount == D("0.01")


def test_duplicate_order_ids_and_confirmation_time():
    original = order()
    with pytest.raises(DomainValidationError):
        snapshot(orders=(original, original))
    with pytest.raises(DomainValidationError):
        snapshot(orders=(order(updated_at=NOW + timedelta(seconds=1)),))
    for field, value in [
        ("portfolio", {}),
        ("financial_settings", {}),
        ("orders", []),
        ("id", "abc"),
        ("id", UUID(int=0)),
        ("confirmed_at", NOW.replace(tzinfo=None)),
    ]:
        with pytest.raises(DomainValidationError):
            snapshot(**{field: value})


@pytest.mark.parametrize("status", list(LimitOrderStatus))
def test_order_lifecycle(status):
    closed = status is not LimitOrderStatus.OPEN
    result = order(status=status, resolved_at=NOW if closed else None)
    assert result.status is status
    with pytest.raises(DomainValidationError):
        order(status=status, resolved_at=None if closed else NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"updated_at": NOW - timedelta(seconds=1)},
        {"created_at": NOW.replace(tzinfo=None)},
        {"side": "buy"},
        {"status": "open"},
        {"id": "bad"},
        {"status": LimitOrderStatus.FILLED, "resolved_at": NOW - timedelta(seconds=1)},
        {"status": LimitOrderStatus.FILLED, "resolved_at": NOW + timedelta(seconds=1)},
    ],
)
def test_invalid_order(changes):
    with pytest.raises(DomainValidationError):
        order(**changes)


def test_stable_classification_is_explicit():
    policy = StablecoinClassification()
    assert policy.is_stable("USDC")
    assert not policy.is_stable("FAKEUSD")
    assert StablecoinClassification(frozenset({"DAI"})).is_stable("DAI")
    assert not StablecoinClassification(frozenset({"DAI"})).is_stable("USDC")
    with pytest.raises(DomainValidationError):
        StablecoinClassification({"USDC"})
    with pytest.raises(DomainValidationError):
        StablecoinClassification(frozenset({"usdc"}))


def test_independent_runs_for_same_state():
    state = snapshot()
    first = AnalysisRun.start(state.id, NOW)
    second = AnalysisRun.start(state.id, NOW + timedelta(hours=4))
    assert first.run_id != second.run_id
    assert first.snapshot_id == second.snapshot_id == state.id
    with pytest.raises(DomainValidationError):
        AnalysisRun(state.id, state.id, NOW)


@pytest.mark.parametrize("status", list(AnalysisRunStatus))
def test_run_lifecycle(status):
    run = AnalysisRun.start(uuid4(), NOW)
    terminal = status is not AnalysisRunStatus.RUNNING
    result = replace(run, status=status, completed_at=NOW if terminal else None)
    assert result.status is status
    with pytest.raises(DomainValidationError):
        replace(run, status=status, completed_at=None if terminal else NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"started_at": NOW.replace(tzinfo=None)},
        {"status": "running"},
        {"market_cutoff": NOW + timedelta(seconds=1)},
        {"status": AnalysisRunStatus.COMPLETED, "completed_at": NOW - timedelta(seconds=1)},
        {"run_id": "bad"},
        {"snapshot_id": UUID(int=0)},
    ],
)
def test_bad_run_metadata(changes):
    with pytest.raises(DomainValidationError):
        replace(AnalysisRun.start(uuid4(), NOW), **changes)


@pytest.mark.parametrize("action", list(ActionType))
def test_all_action_shapes(action):
    args = {}
    if action in {
        ActionType.KEEP_LIMIT_ORDER,
        ActionType.CANCEL_LIMIT_ORDER,
        ActionType.REPLACE_LIMIT_ORDER,
    }:
        args["existing_order_id"] = uuid4()
    if action in {
        ActionType.BUY,
        ActionType.SELL,
        ActionType.PLACE_LIMIT_ORDER,
        ActionType.REPLACE_LIMIT_ORDER,
    }:
        args.update(
            side=TradingSide.SELL if action is ActionType.SELL else TradingSide.BUY,
            quantity_asset=D("0.01"),
        )
    if action in {ActionType.PLACE_LIMIT_ORDER, ActionType.REPLACE_LIMIT_ORDER}:
        args["target_price"] = D("60000")
    assert recommendation(action, **args).action_type is action


@pytest.mark.parametrize(
    "action",
    [ActionType.KEEP_LIMIT_ORDER, ActionType.CANCEL_LIMIT_ORDER, ActionType.REPLACE_LIMIT_ORDER],
)
def test_reference_required(action):
    with pytest.raises(DomainValidationError):
        recommendation(action)


def test_place_forbids_reference_and_replace_requires_full_terms():
    terms = dict(side=TradingSide.BUY, target_price=D("50000"), quantity_asset=D("0.1"))
    with pytest.raises(DomainValidationError):
        recommendation(ActionType.PLACE_LIMIT_ORDER, existing_order_id=uuid4(), **terms)
    for field in terms:
        missing = terms | {field: None}
        with pytest.raises(DomainValidationError):
            recommendation(ActionType.REPLACE_LIMIT_ORDER, existing_order_id=uuid4(), **missing)
    for action in (ActionType.KEEP_LIMIT_ORDER, ActionType.CANCEL_LIMIT_ORDER):
        with pytest.raises(DomainValidationError):
            recommendation(action, existing_order_id=uuid4(), **terms)


@pytest.mark.parametrize(
    "changes",
    [
        {"priority": 0},
        {"priority": 6},
        {"priority": True},
        {"reason": " "},
        {"action_type": "hold"},
        {"existing_order_id": uuid4()},
        {"side": TradingSide.BUY},
        {"target_price": D("1")},
        {"quantity_asset": D("1")},
        {"invalidation_condition": " "},
        {"expires_at": NOW.replace(tzinfo=None)},
        {"review_at": NOW + timedelta(hours=1), "expires_at": NOW},
        {"recommendation_id": "bad"},
        {"asset": "btc"},
        {"target_price": float("inf")},
    ],
)
def test_recommendation_invalid_combinations(changes):
    with pytest.raises(DomainValidationError):
        recommendation(**changes)


def test_buy_sell_consistency_and_positive_quantity():
    for side in TradingSide:
        with pytest.raises(DomainValidationError):
            recommendation(
                ActionType.BUY if side is TradingSide.SELL else ActionType.SELL,
                side=side,
                quantity_asset=D("1"),
            )
    for value in (D("0"), D("-1"), D("NaN"), 1.0):
        with pytest.raises(DomainValidationError):
            recommendation(ActionType.SELL, side=TradingSide.SELL, quantity_asset=value)
    with pytest.raises(DomainValidationError):
        recommendation(ActionType.BUY, asset=None, side=TradingSide.BUY, quantity_asset=D("1"))


def test_spot_domain_has_no_leverage_fields():
    for model in (
        AssetPosition,
        Portfolio,
        LimitOrder,
        FinancialSettings,
        ConfirmedPortfolioSnapshot,
        AnalysisRun,
        Recommendation,
    ):
        names = {field.name for field in fields(model)}
        assert not any(
            word in name
            for name in names
            for word in ("leverage", "margin", "futures", "perpetual")
        )
