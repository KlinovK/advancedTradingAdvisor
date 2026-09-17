import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Context, Decimal, localcontext
from uuid import uuid4

import pytest

from advanced_trading_advisor.cli.main import main, manual_trade_input
from advanced_trading_advisor.domain.persistence import ConflictError, UpdateType
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.manual_trade import (
    ExecutedSpotTrade,
    RecordManualSpotTrade,
    SpotTradeCalculator,
)
from advanced_trading_advisor.portfolio.models import (
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    CorePosition,
    FinancialSettings,
    LimitOrder,
    LimitOrderStatus,
    Portfolio,
    TradingSide,
)
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.repositories import (
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)

NOW = datetime(2026, 9, 17, 8, tzinfo=UTC)
D = Decimal


def state(
    positions=(("SOL", "3.5"), ("USDT", "1374"), ("USDC", "1609")),
    *,
    orders=(),
    cores=(),
):
    return ConfirmedPortfolioSnapshot(
        uuid4(),
        NOW,
        Portfolio(tuple(AssetPosition(symbol, D(amount)) for symbol, amount in positions)),
        FinancialSettings(D("250"), D("12")),
        tuple(orders),
        tuple(cores),
    )


def amounts(portfolio):
    return {position.symbol: position.amount for position in portfolio.positions}


def buy(*, debit="150", fee="0", stable="USDC", asset="SOL", price="97"):
    return ExecutedSpotTrade.buy(asset, D(price), stable, D(debit), D(fee))


def sell(*, quantity="1.5", fee="0", stable="USDC", asset="SOL", price="110"):
    return ExecutedSpotTrade.sell(asset, D(price), stable, D(quantity), D(fee))


def open_order(side=TradingSide.BUY, asset="SOL", price="90", quantity="20"):
    return LimitOrder(
        uuid4(),
        asset,
        side,
        D(price),
        D(quantity),
        LimitOrderStatus.OPEN,
        NOW,
        NOW,
    )


def test_buy_150_divided_by_97_uses_fixed_decimal_context() -> None:
    with localcontext(Context(prec=5)):
        result = SpotTradeCalculator().calculate(state(), buy())

    values = amounts(result.portfolio)
    assert result.executed_asset_quantity == D("1.5463917525773195876288659793814432990")
    assert values["SOL"] == D("5.0463917525773195876288659793814432990")
    assert values["USDC"] == D("1459")
    assert values["USDT"] == D("1374")


def test_buy_and_sell_fee_semantics() -> None:
    bought = SpotTradeCalculator().calculate(state(), buy(fee="1"))
    assert bought.gross_quote_amount == D("149")
    assert bought.executed_asset_quantity == D("1.5360824742268041237113402061855670103")
    assert amounts(bought.portfolio)["USDC"] == D("1459")

    sold = SpotTradeCalculator().calculate(state(), sell(fee="1"))
    assert sold.gross_quote_amount == D("165")
    assert sold.stable_balance_change == D("164")
    assert amounts(sold.portfolio)["USDC"] == D("1773")
    assert amounts(sold.portfolio)["SOL"] == D("2.0")


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ExecutedSpotTrade.buy("DOGE", D("1"), "USDC", D("1")),
        lambda: ExecutedSpotTrade.buy("SOL", D("1"), "DAI", D("1")),
        lambda: ExecutedSpotTrade.buy("SOL", D("0"), "USDC", D("1")),
        lambda: ExecutedSpotTrade.buy("SOL", D("NaN"), "USDC", D("1")),
        lambda: ExecutedSpotTrade.buy("SOL", D("1"), "USDC", D("1"), D("1")),
        lambda: ExecutedSpotTrade.sell("SOL", D("1"), "USDC", D("1"), D("1")),
        lambda: ExecutedSpotTrade.buy("SOL", 1.0, "USDC", D("1")),
        lambda: ExecutedSpotTrade.sell("SOL", D("1"), "USDC", D("-1")),
    ],
)
def test_invalid_trade_values_are_rejected(factory) -> None:
    with pytest.raises((TypeError, ValueError)):
        factory()


def test_insufficient_balances_and_core_floor_are_rejected() -> None:
    calculator = SpotTradeCalculator()
    with pytest.raises(ValueError, match="Insufficient USDC"):
        calculator.calculate(state(), buy(debit="2000"))
    with pytest.raises(ValueError, match="Insufficient SOL"):
        calculator.calculate(state(), sell(quantity="4"))

    protected = state(cores=(CorePosition("SOL", D("3"), D("3.25")),))
    with pytest.raises(ValueError, match="core hard floor"):
        calculator.calculate(protected, sell(quantity="1"))


def test_position_creation_depletion_and_preservation() -> None:
    calculator = SpotTradeCalculator()
    without_sol = state((("BTC", "1"), ("USDC", "1609")))
    bought = calculator.calculate(without_sol, buy())
    assert amounts(bought.portfolio)["BTC"] == D("1")
    assert amounts(bought.portfolio)["SOL"] > 0

    without_usdt = state((("SOL", "1.5"), ("BTC", "1"), ("USDC", "100")))
    sold = calculator.calculate(without_usdt, sell(quantity="1.5", stable="USDT"))
    assert "SOL" not in amounts(sold.portfolio)
    assert amounts(sold.portfolio)["USDT"] == D("165")
    assert amounts(sold.portfolio)["BTC"] == D("1")


def test_confirmation_preserves_settings_cores_and_orders_and_warns(tmp_path) -> None:
    async def scenario():
        order = open_order()
        snapshot = state(
            orders=(order,),
            cores=(CorePosition("SOL", D("2"), D("3")),),
        )
        db = Database(tmp_path / "advisor.sqlite")
        await db.initialize()
        repository = SQLiteSnapshotRepository(db)
        current = await ConfirmedStateService(repository).initialize(snapshot)
        recorder = RecordManualSpotTrade(repository, now=lambda: NOW + timedelta(seconds=1))
        proposal = recorder.prepare(current, buy())
        saved = await recorder.confirm(proposal)

        assert saved.snapshot.financial_settings == snapshot.financial_settings
        assert saved.snapshot.core_positions == snapshot.core_positions
        assert saved.snapshot.orders == (order,)
        assert proposal.calculation.warnings
        assert "unchanged" in proposal.calculation.warnings[0]

    asyncio.run(scenario())


def run_cli(db, *arguments, answers=("yes",)):
    responses = iter(answers)
    output = []
    code = main(
        ["--db", str(db), *arguments],
        input_fn=lambda _prompt: next(responses),
        output=output.append,
        now=lambda: NOW,
    )
    return code, "\n".join(output)


def setup_cli(db, *, both_stables=True):
    arguments = [
        "setup",
        "--position",
        "SOL=3.5",
        "--position",
        "USDC=1609",
    ]
    if both_stables:
        arguments += ["--position", "USDT=1374"]
    arguments += ["--expenses", "250"]
    assert run_cli(db, *arguments)[0] == 0


def current(db):
    return asyncio.run(SQLiteSnapshotRepository(Database(db)).get_current())


def test_interactive_buy_decline_and_settlement_selection(tmp_path) -> None:
    db = tmp_path / "advisor.sqlite"
    setup_cli(db)
    before = current(db)
    answers = ("buy", "SOL", "97", "150", "", "0", "no")
    code, output = run_cli(db, "portfolio", "trade", answers=answers)
    assert code == 1
    assert "Settlement stablecoin is required" in output
    assert current(db) == before

    answers = ("buy", "SOL", "97", "150", "USDC", "0", "no")
    code, output = run_cli(db, "portfolio", "trade", answers=answers)
    assert code == 0
    assert "STABLES: 2983" in output and "STABLES: 2833" in output
    assert "No trade will be executed" in output
    assert current(db) == before


def test_interactive_sell_uses_only_existing_stable_as_default(tmp_path) -> None:
    db = tmp_path / "advisor.sqlite"
    setup_cli(db, both_stables=False)
    answers = ("sell", "SOL", "110", "1.5", "", "1", "yes")
    code, output = run_cli(db, "portfolio", "trade", answers=answers)
    assert code == 0
    assert "Net stable credit: 164.0 USDC" in output
    assert amounts(current(db).snapshot.portfolio)["USDC"] == D("1773")


def test_flag_buy_persists_one_revision_and_audit_without_changing_orders(tmp_path) -> None:
    db = tmp_path / "advisor.sqlite"
    setup_cli(db)
    before = current(db)
    code, output = run_cli(
        db,
        "portfolio",
        "trade",
        "--side",
        "buy",
        "--asset",
        "SOL",
        "--price",
        "97",
        "--stable-debit",
        "150",
        "--stable",
        "USDC",
        "--fee",
        "0",
    )
    assert code == 0 and "No trade was executed" in output
    after = current(db)
    assert after.revision == before.revision + 1
    assert after.snapshot.financial_settings == before.snapshot.financial_settings
    assert after.snapshot.core_positions == before.snapshot.core_positions
    assert after.snapshot.orders == before.snapshot.orders
    assert (
        asyncio.run(SQLiteSnapshotRepository(Database(db)).get(before.snapshot.id))
        == before.snapshot
    )
    audit = asyncio.run(SQLiteConfirmedUpdateRepository(Database(db)).list_recent())
    assert sum(item.update_type is UpdateType.SPOT_TRADE_RECORDED for item in audit) == 1


def test_flag_sell_and_invalid_flag_combinations_do_not_partially_write(tmp_path) -> None:
    db = tmp_path / "advisor.sqlite"
    setup_cli(db)
    code, _ = run_cli(
        db,
        "portfolio",
        "trade",
        "--side",
        "sell",
        "--asset",
        "SOL",
        "--price",
        "110",
        "--quantity",
        "1.5",
        "--stable",
        "USDT",
        "--fee",
        "1",
    )
    assert code == 0
    saved = current(db)
    assert amounts(saved.snapshot.portfolio)["SOL"] == D("2.0")
    assert amounts(saved.snapshot.portfolio)["USDT"] == D("1538")

    for sizing in (("--quantity", "1"), ("--stable-debit", "10")):
        side = "buy" if sizing[0] == "--quantity" else "sell"
        code, _ = run_cli(
            db,
            "portfolio",
            "trade",
            "--side",
            side,
            "--asset",
            "SOL",
            "--price",
            "100",
            *sizing,
            "--stable",
            "USDC",
            "--fee",
            "0",
        )
        assert code == 1
        assert current(db) == saved


def test_atomic_rollback_and_revision_conflict_do_not_double_apply(tmp_path) -> None:
    async def scenario():
        db = Database(tmp_path / "advisor.sqlite")
        await db.initialize()
        repository = SQLiteSnapshotRepository(db)
        service = ConfirmedStateService(repository)
        first = await service.initialize(state())
        recorder = RecordManualSpotTrade(repository, now=lambda: NOW + timedelta(seconds=1))
        proposal = recorder.prepare(first, buy())

        async with db.transaction() as connection:
            await connection.execute(
                """CREATE TRIGGER fail_trade_pointer BEFORE UPDATE ON current_portfolio
                BEGIN SELECT RAISE(ABORT, 'injected failure'); END"""
            )
        with pytest.raises(sqlite3.IntegrityError):
            await recorder.confirm(proposal)
        assert await repository.get_current() == first
        assert len(await repository.list_recent()) == 1
        assert len(await SQLiteConfirmedUpdateRepository(db).list_recent()) == 1

        async with db.transaction() as connection:
            await connection.execute("DROP TRIGGER fail_trade_pointer")
        competing_snapshot = replace(
            first.snapshot, id=uuid4(), confirmed_at=NOW + timedelta(seconds=2)
        )
        competing = await service.confirm(
            competing_snapshot,
            expected_revision=1,
            previous_snapshot_id=first.snapshot.id,
            update_type=UpdateType.BALANCE_CORRECTION,
        )
        with pytest.raises(ConflictError, match="review the trade again"):
            await recorder.confirm(proposal)
        assert await repository.get_current() == competing
        assert len(await repository.list_recent()) == 2

    asyncio.run(scenario())


def test_scheduler_style_old_read_remains_valid_after_trade_confirmation(tmp_path) -> None:
    async def scenario():
        db = Database(tmp_path / "advisor.sqlite")
        await db.initialize()
        repository = SQLiteSnapshotRepository(db)
        first = await ConfirmedStateService(repository).initialize(state())
        scheduler_read = await repository.get_current()
        recorder = RecordManualSpotTrade(repository, now=lambda: NOW + timedelta(seconds=1))
        saved = await recorder.confirm(recorder.prepare(first, buy()))

        assert scheduler_read == first
        assert await repository.get(first.snapshot.id) == first.snapshot
        assert await repository.get_current() == saved
        assert saved.revision == 2

    asyncio.run(scenario())


def test_manual_trade_input_requires_complete_explicit_fields() -> None:
    class Args:
        side = "buy"
        asset = "SOL"
        price = "97"
        stable_debit = "150"
        quantity = None
        stable = None
        fee = "0"

    with pytest.raises(ValueError, match="Explicit trade mode requires"):
        manual_trade_input(Args(), state(), lambda _: pytest.fail("must not prompt"))
