import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from test_portfolio_risk import NOW, asset, market, order, state

from advanced_trading_advisor.analysis.engine import RecommendationEngine, advice
from advanced_trading_advisor.analysis.models import AnalysisRunStatus
from advanced_trading_advisor.analysis.plan import PlanValidator
from advanced_trading_advisor.analysis.policy import RecommendationPolicy
from advanced_trading_advisor.analysis.recommendations import ActionType
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.analysis.use_case import AnalyzeCurrentPortfolio
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.cli.report import render_analysis
from advanced_trading_advisor.domain.persistence import FailureCategory, UpdateType
from advanced_trading_advisor.market.errors import MarketDataError
from advanced_trading_advisor.market.models import MarketAssetData
from advanced_trading_advisor.market.regime import MarketRegime
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import (
    DEFAULT_MANUAL_ACCOUNT_ID,
    DEFAULT_MANUAL_ACCOUNT_NAME,
    Account,
    AccountType,
    AssetPosition,
    LimitOrderStatus,
    Portfolio,
    TradingSide,
)
from advanced_trading_advisor.risk.assessment import PortfolioRiskPolicy
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)

D = Decimal


def fixture(orders=()):
    snapshot = state((("BTC", "10"), ("ETH", "10"), ("SOL", "10"), ("USDC", "10000")), orders)
    evidence = market(asset(), asset("ETH"), asset("SOL"), asset("USDC", "1"))
    risk = PortfolioRiskPolicy().assess(snapshot, evidence, now=NOW)
    return snapshot, evidence, risk


def proposed(
    snapshot,
    action=ActionType.PLACE_LIMIT_ORDER,
    symbol="BTC",
    side=TradingSide.BUY,
    price="83",
    quantity="1",
    order_id=None,
    reason="candidate advice",
):
    return advice(
        snapshot.id,
        symbol,
        action,
        reason,
        order_id=order_id,
        side=side,
        price=D(price) if price is not None else None,
        quantity=D(quantity) if quantity is not None else None,
    )


@pytest.mark.parametrize(
    "regime", [MarketRegime.NEUTRAL, MarketRegime.CONSTRUCTIVE, MarketRegime.RISK_ON]
)
def test_valid_buy_setups_and_determinism(regime):
    s, m, r = fixture()
    r = replace(r, market_regime=replace(r.market_regime, regime=regime))
    engine = RecommendationEngine()
    plan = engine.generate(s, m, r)
    assert plan == engine.generate(s, m, r)
    assert any(a.action_type is ActionType.PLACE_LIMIT_ORDER for a in plan)
    assert not PlanValidator().validate(plan, s, m, r).rejected
    buys = [a for a in plan if a.side is TradingSide.BUY]
    assert sum(
        a.quantity_asset * a.target_price for a in buys
    ) <= r.reserve.deployable_stables_usd * engine.policy.fraction(regime)


def test_elevated_smaller_deeper_and_risk_off_defensive():
    s, m, r = fixture()
    p = RecommendationPolicy()
    assert p.buy_target(m.assets[0], MarketRegime.ELEVATED_RISK) < p.buy_target(
        m.assets[0], MarketRegime.NEUTRAL
    )
    plans = {}
    for regime in (MarketRegime.NEUTRAL, MarketRegime.ELEVATED_RISK, MarketRegime.RISK_OFF):
        adjusted = replace(r, market_regime=replace(r.market_regime, regime=regime))
        plans[regime] = RecommendationEngine().generate(s, m, adjusted)

    def total(plan):
        return sum(a.target_price * a.quantity_asset for a in plan if a.side is TradingSide.BUY)

    assert total(plans[MarketRegime.ELEVATED_RISK]) < total(plans[MarketRegime.NEUTRAL])
    assert total(plans[MarketRegime.RISK_OFF]) == 0


@pytest.mark.parametrize("kind", ["deficit", "no_capital", "overbought", "chase", "missing"])
def test_no_weak_or_unfunded_buys(kind):
    s, m, r = fixture()
    if kind == "deficit":
        r = replace(r, reserve=replace(r.reserve, stable_reserve_deficit_usd=D("1")))
    elif kind == "no_capital":
        r = replace(r, reserve=replace(r.reserve, deployable_stables_usd=D("0")))
    else:
        if kind == "missing":
            m = replace(
                m, assets=tuple(MarketAssetData(a.symbol, a.price_usd, NOW) for a in m.assets)
            )
        else:
            key = "rsi_4h" if kind == "overbought" else "change_24h_pct"
            m = replace(
                m,
                assets=tuple(
                    replace(a, technical_features=replace(a.technical_features, **{key: D("99")}))
                    for a in m.assets
                ),
            )
        r = PortfolioRiskPolicy().assess(s, m, now=NOW)
    candidates = RecommendationEngine().generate(s, m, r)
    final = PlanValidator().validate(candidates, s, m, r)
    assert all(a.side is not TradingSide.BUY for a in final.recommendations)


@pytest.mark.parametrize(
    "price,expected",
    [
        ("83", ActionType.KEEP_LIMIT_ORDER),
        ("90", ActionType.REPLACE_LIMIT_ORDER),
        ("110", ActionType.CANCEL_LIMIT_ORDER),
    ],
)
def test_existing_buy_order_review_exact_id(price, expected):
    o = order(symbol="BTC", price=price, quantity="1")
    s, m, r = fixture((o,))
    candidates = RecommendationEngine().generate(s, m, r)
    review = next(a for a in candidates if a.existing_order_id == o.id)
    assert review.action_type is expected
    assert not PlanValidator().validate(candidates, s, m, r).rejected
    if expected is ActionType.REPLACE_LIMIT_ORDER:
        assert review.target_price == D("83")
        assert review.quantity_asset is not None


def test_risk_off_cancels_buys_keeps_inventory_backed_sells():
    buy = order(symbol="BTC", price="83")
    sell = order(TradingSide.SELL, "BTC", "1", "130")
    s, m, r = fixture((buy, sell))
    r = replace(r, market_regime=replace(r.market_regime, regime=MarketRegime.RISK_OFF))
    plan = RecommendationEngine().generate(s, m, r)
    references = {a.existing_order_id: a for a in plan if a.existing_order_id}
    assert references[buy.id].action_type is ActionType.CANCEL_LIMIT_ORDER
    assert references[sell.id].action_type is ActionType.KEEP_LIMIT_ORDER


def test_multiple_same_asset_orders_each_get_an_exact_review():
    orders = (order(symbol="BTC", price="83"), order(symbol="BTC", price="82"))
    s, m, r = fixture(orders)
    refs = [
        a.existing_order_id for a in RecommendationEngine().generate(s, m, r) if a.existing_order_id
    ]
    assert set(refs) == {o.id for o in orders}
    assert len(refs) == 2


def test_combined_buys_and_duplicate_equivalent_proposals():
    s, m, r = fixture()
    budget = r.reserve.deployable_stables_usd * RecommendationPolicy().fraction(
        r.market_regime.regime
    )
    quantity = budget / D("83") * D("0.6")
    a = proposed(s, quantity=str(quantity))
    b = proposed(s, symbol="ETH", quantity=str(quantity))
    assert not PlanValidator().validate((a,), s, m, r).rejected
    assert PlanValidator().validate((a, b), s, m, r).rejected
    assert PlanValidator().validate((a, replace(a, recommendation_id=uuid4())), s, m, r).rejected


def test_sell_quantities_include_existing_reservations_and_replacements():
    existing = order(TradingSide.SELL, "BTC", "6", "120")
    s, m, r = fixture((existing,))
    a = proposed(s, action=ActionType.SELL, side=TradingSide.SELL, price=None, quantity="3")
    b = proposed(
        s, action=ActionType.PLACE_LIMIT_ORDER, side=TradingSide.SELL, price="130", quantity="2"
    )
    assert not PlanValidator().validate((a,), s, m, r).rejected
    assert PlanValidator().validate((a, b), s, m, r).rejected
    replacement = proposed(
        s,
        action=ActionType.REPLACE_LIMIT_ORDER,
        side=TradingSide.SELL,
        price="130",
        quantity="6",
        order_id=existing.id,
    )
    assert PlanValidator().validate((replacement,), s, m, r).rejected


@pytest.mark.parametrize("kind", ["missing", "resolved", "wrong_asset", "wrong_side", "conflict"])
def test_invalid_order_references(kind):
    o = order(symbol="BTC", price="83")
    if kind == "resolved":
        o = replace(o, status=LimitOrderStatus.CANCELLED, resolved_at=NOW)
    s, m, r = fixture((o,))
    a = proposed(
        s,
        action=ActionType.KEEP_LIMIT_ORDER,
        price=None,
        quantity=None,
        order_id=uuid4() if kind == "missing" else o.id,
        symbol="ETH" if kind == "wrong_asset" else "BTC",
        side=TradingSide.SELL if kind == "wrong_side" else TradingSide.BUY,
    )
    plan = (a,)
    if kind == "conflict":
        plan += (replace(a, recommendation_id=uuid4(), action_type=ActionType.CANCEL_LIMIT_ORDER),)
    assert PlanValidator().validate(plan, s, m, r).rejected


def test_cancellation_and_future_sales_do_not_fund_buys():
    o = order(symbol="BTC", price="83", quantity="1")
    s, m, r = fixture((o,))
    r = replace(r, reserve=replace(r.reserve, deployable_stables_usd=D("0")))
    cancel = proposed(
        s, action=ActionType.CANCEL_LIMIT_ORDER, price=None, quantity=None, order_id=o.id
    )
    sell = proposed(
        s, action=ActionType.SELL, symbol="ETH", side=TradingSide.SELL, price=None, quantity="5"
    )
    buy = proposed(s, symbol="SOL")
    assert PlanValidator().validate((cancel, sell, buy), s, m, r).rejected
    replacement = proposed(s, action=ActionType.REPLACE_LIMIT_ORDER, order_id=o.id)
    assert PlanValidator().validate((replacement,), s, m, r).rejected


def test_allocation_bound_and_no_rejected_narrative():
    s, m, r = fixture()
    bad = proposed(s, quantity="999", reason="REJECTED ADVICE MUST NEVER SURVIVE")
    validated = PlanValidator().validate((bad,), s, m, r)
    assert validated.rejected
    assert "REJECTED ADVICE" not in str(validated.recommendations)
    assert validated.recommendations[0].action_type is ActionType.WAIT


def cli(db, *args, answer="yes", provider=None):
    output = []
    code = main(
        ["--db", str(db), *args],
        input_fn=lambda prompt: answer,
        output=output.append,
        provider_factory=provider,
        now=lambda: NOW,
    )
    return code, "\n".join(output)


def setup_args():
    return ("setup", "--position", "BTC=10", "--position", "USDC=10000", "--expenses", "250")


def current(db):
    return asyncio.run(SQLiteSnapshotRepository(Database(db)).get_current())


def test_cli_setup_decline_validation_and_reopen(tmp_path):
    db = tmp_path / "advisor.sqlite"
    assert cli(db, *setup_args(), answer="no")[0] == 0
    assert current(db) is None
    assert cli(db, "setup", "--position", "BTC=0,1", "--expenses", "250")[0] == 1
    assert current(db) is None
    assert cli(db, *setup_args())[0] == 0
    assert current(db).revision == 1
    manual_account = current(db).snapshot.portfolio.accounts[0]
    assert manual_account.id == DEFAULT_MANUAL_ACCOUNT_ID
    assert manual_account.name == DEFAULT_MANUAL_ACCOUNT_NAME
    assert manual_account.type is AccountType.MANUAL
    assert manual_account.positions == current(db).snapshot.portfolio.positions
    assert current(db).snapshot.financial_settings.target_expense_runway_months == 12
    assert cli(db, *setup_args())[0] == 1
    code, text = cli(db, "portfolio", "show")
    assert code == 0 and "BTC: 10" in text and "Revision: 1" in text


def test_cli_order_add_requires_explicit_existing_account_id(tmp_path):
    db = tmp_path / "advisor.sqlite"
    assert cli(db, *setup_args())[0] == 0
    terms = ("--asset", "BTC", "--side", "buy", "--price", "83", "--quantity", "1")
    before = current(db)

    with pytest.raises(SystemExit) as missing:
        cli(db, "orders", "add", *terms)
    assert missing.value.code == 2

    unknown_id = UUID("22222222-2222-4222-8222-222222222222")
    code, output = cli(db, "orders", "add", "--account-id", str(unknown_id), *terms)
    assert code == 1
    assert "Order account must exist" in output
    assert current(db) == before

    code, _ = cli(db, "orders", "add", "--account-id", str(DEFAULT_MANUAL_ACCOUNT_ID), *terms)
    assert code == 0
    assert current(db).snapshot.orders[0].account_id == DEFAULT_MANUAL_ACCOUNT_ID


def test_cli_nonmanual_order_keeps_account_and_fill_fails_closed(tmp_path):
    db = tmp_path / "advisor.sqlite"
    assert cli(db, *setup_args())[0] == 0
    previous = current(db)
    wallet_id = UUID("22222222-2222-4222-8222-222222222222")
    wallet = Account(
        wallet_id,
        "Wallet",
        AccountType.EXTERNAL_WALLET,
        (AssetPosition("ETH", D("2")), AssetPosition("USDC", D("500"))),
    )
    multi_account = replace(
        previous.snapshot,
        id=uuid4(),
        portfolio=Portfolio(previous.snapshot.portfolio.accounts + (wallet,)),
    )
    repository = SQLiteSnapshotRepository(Database(db))
    asyncio.run(
        ConfirmedStateService(repository).confirm(
            multi_account,
            expected_revision=previous.revision,
            previous_snapshot_id=previous.snapshot.id,
            update_type=UpdateType.BALANCE_CORRECTION,
        )
    )

    assert (
        cli(
            db,
            "orders",
            "add",
            "--account-id",
            str(wallet_id),
            "--asset",
            "ETH",
            "--side",
            "sell",
            "--price",
            "110",
            "--quantity",
            "1",
        )[0]
        == 0
    )
    added = current(db)
    tracked_order = added.snapshot.orders[0]
    assert tracked_order.account_id == wallet_id

    fill_code, fill_output = cli(
        db,
        "orders",
        "update",
        str(tracked_order.id),
        "--status",
        "filled",
        "--position",
        "ETH=1",
    )
    assert fill_code == 1
    assert "account-aware replacement is not available" in fill_output
    assert current(db) == added

    assert cli(db, "orders", "update", str(tracked_order.id), "--status", "cancelled")[0] == 0
    cancelled = current(db).snapshot.orders[0]
    assert cancelled.account_id == wallet_id
    assert cancelled.status is LimitOrderStatus.CANCELLED
    assert current(db).snapshot.portfolio.accounts[1] == wallet


def test_cli_replace_orders_and_filled_balances(tmp_path):
    db = tmp_path / "advisor.sqlite"
    cli(db, *setup_args())
    initial = current(db)
    assert (
        cli(
            db,
            "orders",
            "add",
            "--account-id",
            str(DEFAULT_MANUAL_ACCOUNT_ID),
            "--asset",
            "BTC",
            "--side",
            "buy",
            "--price",
            "83",
            "--quantity",
            "1",
        )[0]
        == 0
    )
    added = current(db)
    o = added.snapshot.orders[0]
    assert added.revision == 2
    assert o.account_id == DEFAULT_MANUAL_ACCOUNT_ID
    assert cli(db, "orders", "update", str(o.id), "--status", "filled")[0] == 1
    assert current(db) == added
    assert (
        cli(
            db,
            "orders",
            "update",
            str(o.id),
            "--status",
            "filled",
            "--position",
            "BTC=11",
            "--position",
            "USDC=9917",
        )[0]
        == 0
    )
    filled = current(db)
    assert filled.snapshot.orders[0].status is LimitOrderStatus.FILLED
    assert filled.snapshot.orders[0].account_id == o.account_id
    assert filled.snapshot.portfolio.positions[1].amount == D("9917")
    assert cli(db, "orders", "update", str(o.id), "--status", "cancelled")[0] == 1
    assert (
        cli(
            db,
            "portfolio",
            "replace",
            "--position",
            "USDC=5000",
            "--expenses",
            "200",
            "--runway",
            "10",
        )[0]
        == 0
    )
    replaced = current(db)
    assert replaced.revision == 4 and replaced.snapshot.id != initial.snapshot.id
    assert (
        replaced.snapshot.portfolio.accounts[0].id
        == initial.snapshot.portfolio.accounts[0].id
        == DEFAULT_MANUAL_ACCOUNT_ID
    )
    assert replaced.snapshot.orders == filled.snapshot.orders
    assert (
        asyncio.run(SQLiteSnapshotRepository(Database(db)).get(initial.snapshot.id))
        == initial.snapshot
    )
    assert "filled" in cli(db, "orders", "show")[1]


@pytest.mark.parametrize(
    "side,price,quantity,error",
    [
        ("buy", "20000", "1", "Insufficient settlement balance"),
        ("sell", "110", "11", "Insufficient asset balance"),
    ],
)
def test_cli_filled_order_requires_resources_in_its_manual_account(
    tmp_path, side, price, quantity, error
):
    db = tmp_path / "advisor.sqlite"
    assert cli(db, *setup_args())[0] == 0
    assert (
        cli(
            db,
            "orders",
            "add",
            "--account-id",
            str(DEFAULT_MANUAL_ACCOUNT_ID),
            "--asset",
            "BTC",
            "--side",
            side,
            "--price",
            price,
            "--quantity",
            quantity,
        )[0]
        == 0
    )
    before = current(db)

    code, output = cli(
        db,
        "orders",
        "update",
        str(before.snapshot.orders[0].id),
        "--status",
        "filled",
        "--position",
        "BTC=10",
        "--position",
        "USDC=10000",
    )

    assert code == 1
    assert error in output
    assert current(db) == before


def test_cli_full_balance_entry_cannot_flatten_other_accounts(tmp_path):
    db = tmp_path / "advisor.sqlite"
    assert cli(db, *setup_args())[0] == 0
    assert (
        cli(
            db,
            "orders",
            "add",
            "--account-id",
            str(DEFAULT_MANUAL_ACCOUNT_ID),
            "--asset",
            "BTC",
            "--side",
            "buy",
            "--price",
            "83",
            "--quantity",
            "1",
        )[0]
        == 0
    )
    previous = current(db)
    wallet = Account(
        UUID("22222222-2222-4222-8222-222222222222"),
        "Wallet",
        AccountType.EXTERNAL_WALLET,
        (AssetPosition("BTC", D("0.5")),),
    )
    multi_account = replace(
        previous.snapshot,
        id=uuid4(),
        confirmed_at=NOW + timedelta(seconds=1),
        portfolio=Portfolio(previous.snapshot.portfolio.accounts + (wallet,)),
    )
    repository = SQLiteSnapshotRepository(Database(db))
    saved = asyncio.run(
        ConfirmedStateService(repository).confirm(
            multi_account,
            expected_revision=previous.revision,
            previous_snapshot_id=previous.snapshot.id,
            update_type=UpdateType.BALANCE_CORRECTION,
        )
    )

    replace_code, replace_output = cli(
        db, "portfolio", "replace", "--position", "BTC=10", "--expenses", "250"
    )
    fill_code, fill_output = cli(
        db,
        "orders",
        "update",
        str(saved.snapshot.orders[0].id),
        "--status",
        "filled",
        "--position",
        "BTC=10",
    )

    assert replace_code == fill_code == 1
    assert "account-aware replacement is not available" in replace_output
    assert "account-aware replacement is not available" in fill_output
    assert current(db) == saved


@pytest.mark.parametrize("status", ["cancelled", "expired"])
def test_cli_order_resolution_does_not_infer_balances(tmp_path, status):
    db = tmp_path / "advisor.sqlite"
    cli(db, *setup_args())
    cli(
        db,
        "orders",
        "add",
        "--account-id",
        str(DEFAULT_MANUAL_ACCOUNT_ID),
        "--asset",
        "BTC",
        "--side",
        "sell",
        "--price",
        "110",
        "--quantity",
        "1",
    )
    before = current(db)
    assert (
        cli(db, "orders", "update", str(before.snapshot.orders[0].id), "--status", status)[0] == 0
    )
    assert current(db).snapshot.portfolio == before.snapshot.portfolio
    assert current(db).snapshot.orders[0].account_id == before.snapshot.orders[0].account_id


def test_cli_revision_conflict_after_confirmation_prompt(tmp_path):
    db = tmp_path / "advisor.sqlite"
    cli(db, *setup_args())
    output = []

    # Another connection commits while the user reviews the proposed state.
    def prompt(_):
        import sqlite3

        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE current_portfolio SET revision = revision + 1")
        return "yes"

    code = main(
        ["--db", str(db), "portfolio", "replace", "--empty", "--expenses", "250"],
        input_fn=prompt,
        output=output.append,
        now=lambda: NOW,
    )
    assert code == 1 and "reload" in " ".join(output)
    assert current(db).snapshot.portfolio.positions


class FakeProvider:
    async def get_market_snapshot(self, symbols):
        return market(*(asset(s, "1" if s == "USDC" else "100") for s in symbols))


def test_cli_analyze_history_status_no_mutation_and_roundtrip(tmp_path):
    db = tmp_path / "advisor.sqlite"
    cli(db, *setup_args())
    before = current(db)
    assert cli(db, "analyze", provider=FakeProvider)[0] == 0
    code, text = cli(db, "analyze", provider=FakeProvider)
    assert code == 0 and "ADVANCED TRADING ADVISOR" in text and "no trades were executed" in text
    assert current(db) == before
    runs = asyncio.run(SQLiteAnalysisRunRepository(Database(db)).list_all_recent())
    assert len(runs) == 2 and runs[0].run.run_id != runs[1].run.run_id
    result = AnalysisResultCodec.decode(runs[0].result_json)
    assert AnalysisResultCodec.decode(AnalysisResultCodec.encode(result)) == result
    assert result.snapshot_id == before.snapshot.id
    assert result.market_cutoff is not None
    assert runs[0].run.market_cutoff == result.market_cutoff
    assert "completed" in cli(db, "history")[1]
    assert "ADVANCED TRADING ADVISOR" in cli(db, "history", "latest")[1]
    assert "Last completed analysis:" in cli(db, "status")[1]
    assert "Revision: 1" in cli(db, "status")[1]
    altered = replace(
        result,
        financial=replace(
            result.financial, total_portfolio_usd=D("12345.12345678901234567890123456789")
        ),
    )
    assert (
        AnalysisResultCodec.decode(AnalysisResultCodec.encode(altered)).financial
        == altered.financial
    )
    rejected = PlanValidator().validate(
        (proposed(before.snapshot, quantity="999", reason="LEAK"),),
        before.snapshot,
        result.market,
        PortfolioRiskPolicy().assess(before.snapshot, result.market, now=NOW),
    )
    assert "LEAK" not in render_analysis(replace(result, recommendations=rejected.recommendations))
    payload = json.loads(runs[0].result_json)
    payload["schema_version"] = 6
    with pytest.raises(ValueError):
        AnalysisResultCodec.decode(json.dumps(payload))


@pytest.mark.parametrize(
    "error,category",
    [
        (MarketDataError("private upstream"), FailureCategory.MARKET_UNAVAILABLE),
        (RuntimeError("private internal"), FailureCategory.ANALYSIS_FAILED),
        (TimeoutError(), FailureCategory.TIMEOUT),
    ],
)
def test_failed_analysis_persisted_without_result(tmp_path, error, category):
    db = tmp_path / "advisor.sqlite"
    cli(db, *setup_args())
    before = current(db)

    class BrokenProvider:
        async def get_market_snapshot(self, symbols):
            raise error

    code, output = cli(db, "analyze", provider=BrokenProvider)
    assert code == 1 and "private" not in output
    runs = asyncio.run(SQLiteAnalysisRunRepository(Database(db)).list_all_recent())
    assert runs[0].run.status is AnalysisRunStatus.FAILED
    assert runs[0].failure_category is category and runs[0].result_json is None
    assert current(db) == before
    assert "No completed" in cli(db, "history", "latest")[1]


def test_analyze_without_setup_and_static_cli(tmp_path):
    db = tmp_path / "advisor.sqlite"
    assert cli(db, "analyze", provider=FakeProvider)[0] == 1
    assert "Revision: 0" in cli(db, "status")[1]
    cli(db, *setup_args())
    code, output = cli(db, "analyze", "--source", "static")
    assert code == 0 and "STATIC / DEVELOPMENT ONLY" in output
    assert "PLACE LIMIT ORDER" not in output


def test_cancelled_analysis_marks_failure(tmp_path):
    async def scenario():
        db = Database(tmp_path / "advisor.sqlite")
        await db.initialize()
        snapshots, runs = SQLiteSnapshotRepository(db), SQLiteAnalysisRunRepository(db)
        await ConfirmedStateService(snapshots).initialize(fixture()[0])

        class Cancelled:
            async def get_market_snapshot(self, symbols):
                raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await AnalyzeCurrentPortfolio(snapshots, runs, Cancelled, now=lambda: NOW).execute()
        assert (await runs.list_all_recent())[0].run.status is AnalysisRunStatus.FAILED

    asyncio.run(scenario())


def test_allocation_bound_even_when_cash_budget_fits():
    s, m, r = fixture()
    # Lower the allocation cap but keep sufficient buy cash. Projected exposure blocks the leg.
    p = RecommendationPolicy(max_asset_allocation=D("0.08"))
    a = proposed(s, quantity="1")
    assert PlanValidator(p).validate((a,), s, m, r).rejected


def test_market_buy_and_stable_sell_fail_closed():
    s, m, r = fixture()
    for a in (
        proposed(s, action=ActionType.BUY, price=None),
        proposed(s, action=ActionType.SELL, symbol="USDC", side=TradingSide.SELL, price=None),
    ):
        assert PlanValidator().validate((a,), s, m, r).rejected


def test_marketable_sell_review_is_not_a_fill():
    o = order(TradingSide.SELL, "BTC", "1", "90")
    s, m, r = fixture((o,))
    plan = RecommendationEngine().generate(s, m, r)
    assert (
        next(a for a in plan if a.existing_order_id == o.id).action_type
        is ActionType.CANCEL_LIMIT_ORDER
    )
    assert s.orders[0].status is LimitOrderStatus.OPEN


def test_deficit_candidates_contain_no_new_buy_before_validation():
    s, m, r = fixture()
    r = replace(r, reserve=replace(r.reserve, stable_reserve_deficit_usd=D("10")))
    assert all(a.side is not TradingSide.BUY for a in RecommendationEngine().generate(s, m, r))


def test_latest_history_spans_previous_snapshots_and_failures(tmp_path):
    db = tmp_path / "advisor.sqlite"
    cli(db, *setup_args())
    cli(db, "analyze", provider=FakeProvider)
    previous_id = str(current(db).snapshot.id)
    cli(db, "portfolio", "replace", "--empty", "--expenses", "250")
    assert previous_id in cli(db, "history", "latest")[1]
    assert previous_id in cli(db, "history")[1]


def test_completion_cutoff_can_cross_run_start_but_not_completion(tmp_path):
    from datetime import timedelta

    from advanced_trading_advisor.analysis.models import AnalysisRun
    from advanced_trading_advisor.domain.validation import DomainValidationError

    async def scenario():
        db = Database(tmp_path / "advisor.sqlite")
        await db.initialize()
        snapshots, runs = SQLiteSnapshotRepository(db), SQLiteAnalysisRunRepository(db)
        s = fixture()[0]
        await snapshots.save(s)
        run = AnalysisRun.start(s.id, NOW)
        await runs.create(run)
        with pytest.raises(DomainValidationError):
            await runs.mark_completed(
                run.run_id, completed_at=NOW, market_cutoff=NOW + timedelta(minutes=1)
            )
        completed = await runs.mark_completed(
            run.run_id,
            completed_at=NOW + timedelta(minutes=2),
            market_cutoff=NOW + timedelta(minutes=1),
        )
        assert (await runs.get(run.run_id)) == completed

    asyncio.run(scenario())
