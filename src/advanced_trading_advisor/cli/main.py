import argparse
import asyncio
import os
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from advanced_trading_advisor.ai.service import OpenAIReasoner
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.analysis.use_case import AnalysisFailed
from advanced_trading_advisor.configuration import local_environment
from advanced_trading_advisor.delivery.models import DeliveryStatus
from advanced_trading_advisor.domain.numeric import parse_decimal, parse_symbol
from advanced_trading_advisor.domain.persistence import PersistenceError, UpdateType
from advanced_trading_advisor.domain.validation import require
from advanced_trading_advisor.market.live import SYMBOL_IDS
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.manual_trade import (
    SETTLEMENT_ASSETS,
    ExecutedSpotTrade,
    RecordManualSpotTrade,
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
from advanced_trading_advisor.scheduler.models import ScheduledStatus
from advanced_trading_advisor.scheduler.policy import SchedulerPolicy
from advanced_trading_advisor.scheduler.service import Scheduler
from advanced_trading_advisor.scheduler.settings import SchedulerSettings
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.delivery import SQLiteDeliveryRepository
from advanced_trading_advisor.storage.maintenance import (
    backup_database,
    check_schema,
    inspect_database,
    readonly,
)
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)
from advanced_trading_advisor.storage.scheduler import SQLiteSchedulerRepository
from advanced_trading_advisor.telegram.settings import TelegramSettings
from advanced_trading_advisor.telegram.transport import TelegramSendError

from .composition import compose_analysis, compose_delivery
from .report import render_analysis, render_manual_spot_trade, render_setups, render_snapshot


def add_balances(parser, required=True):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        "--position",
        action="append",
        metavar="SYMBOL=AMOUNT",
        help="Repeat for FULL balances, including reserved units.",
    )
    group.add_argument("--empty", action="store_true", help="Explicitly confirm zero holdings.")


def parser():
    root = argparse.ArgumentParser(
        prog="advisor", description="Local spot-only decision support. No trades are executed."
    )
    root.add_argument(
        "--db",
        default=os.environ.get("ADVISOR_DB_PATH", "~/.advanced_trading_advisor/advisor.sqlite"),
    )
    commands = root.add_subparsers(dest="command")
    setup = commands.add_parser("setup")
    portfolio = commands.add_parser("portfolio").add_subparsers(dest="operation", required=True)
    portfolio.add_parser("show")
    replacement = portfolio.add_parser("replace")
    trade = portfolio.add_parser("trade")
    trade.add_argument("--side", choices=[side.value for side in TradingSide])
    trade.add_argument("--asset")
    trade.add_argument("--price")
    trade.add_argument("--stable-debit")
    trade.add_argument("--quantity")
    trade.add_argument("--stable")
    trade.add_argument("--fee")
    core = portfolio.add_parser("core")
    core.add_argument("--asset", required=True, help="Explicit core symbol, e.g. ETH.")
    core.add_argument("--floor", required=True, help="Hard protected asset quantity.")
    core.add_argument("--preferred", required=True, help="Preferred long-term quantity.")
    for p in (setup, replacement):
        add_balances(p)
        p.add_argument("--expenses", required=True, help="Monthly expenses in USD.")
        p.add_argument("--runway", default="12", help="Target months, default 12.")
    orders = commands.add_parser("orders").add_subparsers(dest="operation", required=True)
    orders.add_parser("show")
    add = orders.add_parser("add")
    add.add_argument("--asset", required=True)
    add.add_argument("--side", choices=[s.value for s in TradingSide], required=True)
    add.add_argument("--price", required=True, help="USD per asset.")
    add.add_argument("--quantity", required=True)
    update = orders.add_parser("update")
    update.add_argument("id", type=UUID)
    update.add_argument("--status", choices=["filled", "cancelled", "expired"], required=True)
    add_balances(update, required=False)
    analyze = commands.add_parser("analyze")
    ai = analyze.add_mutually_exclusive_group()
    ai.add_argument("--ai", dest="ai", action="store_true", default=None)
    ai.add_argument("--no-ai", dest="ai", action="store_false")
    analyze.add_argument(
        "--source",
        choices=["live", "static"],
        default="live",
        help="Static is an explicitly labelled offline development fixture.",
    )
    commands.add_parser("status")
    commands.add_parser("doctor")
    scheduler = commands.add_parser("scheduler")
    scheduler.add_argument("operation", choices=["run", "once", "status"])
    telegram = commands.add_parser("telegram")
    telegram.add_argument("operation", choices=["status", "test", "send-latest"])
    backup = commands.add_parser("backup")
    backup.add_argument("operation", choices=["inspect"], nargs="?")
    backup.add_argument("path", nargs="?")
    backup.add_argument("--output", help="New backup file path; existing destinations are refused.")
    history = commands.add_parser("history")
    history.add_argument("operation", choices=["latest"], nargs="?")
    history.add_argument("--limit", type=int, default=20)
    setups = commands.add_parser("setups")
    setups.add_argument("id", type=UUID, nargs="?", help="Optional exact persisted setup UUID.")
    return root


def balances(args):
    values = []
    for raw in args.position or ():
        symbol, sep, amount = raw.partition("=")
        require(bool(sep), "Use --position SYMBOL=AMOUNT.")
        symbol = parse_symbol(symbol)
        require(symbol in SYMBOL_IDS, "Supported assets: BTC ETH SOL USDT USDC.")
        values.append(AssetPosition(symbol, parse_decimal(amount, positive=True)))
    return Portfolio(tuple(values))


def _settlement_stable(snapshot, input_fn) -> str:
    existing = tuple(
        position.symbol
        for position in snapshot.portfolio.positions
        if position.symbol in SETTLEMENT_ASSETS
    )
    default = existing[0] if len(existing) == 1 else None
    prompt = f"Settlement stable [{default}]: " if default else "Settlement stable: "
    entered = input_fn(prompt).strip()
    require(bool(entered) or default is not None, "Settlement stablecoin is required.")
    return parse_symbol(entered or default)


def manual_trade_input(args, snapshot, input_fn) -> ExecutedSpotTrade:
    values = (
        args.side,
        args.asset,
        args.price,
        args.stable_debit,
        args.quantity,
        args.stable,
        args.fee,
    )
    explicit = any(value is not None for value in values)
    if explicit:
        require(
            all(
                value is not None
                for value in (args.side, args.asset, args.price, args.stable, args.fee)
            ),
            "Explicit trade mode requires --side, --asset, --price, --stable, and --fee.",
        )
        side = TradingSide(args.side)
        asset = parse_symbol(args.asset)
        price = parse_decimal(args.price, positive=True)
        stable = parse_symbol(args.stable)
        fee = parse_decimal(args.fee)
        if side is TradingSide.BUY:
            require(
                args.stable_debit is not None and args.quantity is None,
                "BUY requires --stable-debit and does not accept --quantity.",
            )
            return ExecutedSpotTrade.buy(
                asset,
                price,
                stable,
                parse_decimal(args.stable_debit, positive=True),
                fee,
            )
        require(
            args.quantity is not None and args.stable_debit is None,
            "SELL requires --quantity and does not accept --stable-debit.",
        )
        return ExecutedSpotTrade.sell(
            asset, price, stable, parse_decimal(args.quantity, positive=True), fee
        )

    side = TradingSide(input_fn("Side [BUY/SELL]: ").strip().lower())
    asset = parse_symbol(input_fn("Asset: "))
    price = parse_decimal(input_fn("Execution price: "), positive=True)
    if side is TradingSide.BUY:
        sizing = parse_decimal(input_fn("Total stable debited, including fee: "), positive=True)
    else:
        sizing = parse_decimal(input_fn("Actual asset quantity sold: "), positive=True)
    stable = _settlement_stable(snapshot, input_fn)
    fee_text = input_fn(f"Fee in {stable} [0]: ").strip()
    fee = parse_decimal(fee_text or "0")
    return (
        ExecutedSpotTrade.buy(asset, price, stable, sizing, fee)
        if side is TradingSide.BUY
        else ExecutedSpotTrade.sell(asset, price, stable, sizing, fee)
    )


class CLI:
    def __init__(
        self,
        *,
        input_fn=input,
        output=print,
        now=lambda: datetime.now(UTC),
        provider_factory=None,
        reasoner_factory=OpenAIReasoner,
        sleep=asyncio.sleep,
        telegram_sender_factory=None,
    ):
        self.input, self.output, self.now, self.provider_factory = (
            input_fn,
            output,
            now,
            provider_factory,
        )
        self.reasoner_factory = reasoner_factory
        self.sleep = sleep
        self.telegram_sender_factory = telegram_sender_factory

    async def run(self, args):
        db = Database(Path(args.db))
        if args.command == "telegram":
            environment = local_environment()
            if args.operation == "status":
                enabled = environment.get("TELEGRAM_ENABLED", "false").lower() == "true"
                token_configured = bool(environment.get("TELEGRAM_BOT_TOKEN"))
                chat_configured = bool(environment.get("TELEGRAM_CHAT_ID"))
                latest = None
                if db.path.exists():
                    async with readonly(db.path) as connection:
                        version = await check_schema(connection)
                    if version >= 3:
                        latest = await SQLiteDeliveryRepository(db).latest()
                self.output(
                    f"Enabled: {'yes' if enabled else 'no'}\n"
                    f"Token configured: {'yes' if token_configured else 'no'}\n"
                    f"Chat configured: {'yes' if chat_configured else 'no'}\n"
                    f"Latest delivery: {latest.status.value if latest else 'none'}\n"
                    f"Latest analysis: {latest.analysis_run_id if latest else 'none'}"
                )
                TelegramSettings.from_environment(environment)
                return
            settings = TelegramSettings.from_environment(environment)
            require(settings.enabled, "Telegram disabled; set TELEGRAM_ENABLED=true.")
            if args.operation == "test":
                service = compose_delivery(
                    db,
                    settings,
                    now=self.now,
                    sleep=self.sleep,
                    sender_factory=self.telegram_sender_factory,
                )
                await service.test_connection()
                self.output("Telegram accepted the harmless connection test.")
                return
            require(db.path.is_file(), "No local database; complete an analysis first.")
            await db.initialize()
            runs = SQLiteAnalysisRunRepository(db)
            latest = await runs.latest_completed()
            require(latest is not None, "No completed analysis to deliver.")
            prior = await SQLiteDeliveryRepository(db).get(
                latest.run.run_id, settings.destination_key
            )
            service = compose_delivery(
                db,
                settings,
                now=self.now,
                sleep=self.sleep,
                sender_factory=self.telegram_sender_factory,
            )
            delivery = await service.deliver(latest.run.run_id, manual_retry=True)
            if delivery.status is DeliveryStatus.DELIVERED:
                self.output(
                    "Telegram already delivered this analysis."
                    if prior and prior.status is DeliveryStatus.DELIVERED
                    else f"Telegram delivery: DELIVERED; analysis {delivery.analysis_run_id}."
                )
            elif delivery.status is DeliveryStatus.FAILED:
                raise AnalysisFailed(
                    f"Telegram delivery failed ({delivery.failure_category.value}); "
                    "analysis remains completed."
                )
            else:
                self.output(
                    f"Telegram delivery: {delivery.status.value}; no duplicate message sent."
                )
            return
        if args.command == "scheduler":
            settings = SchedulerSettings.from_environment()
            policy = SchedulerPolicy(settings)
            if args.operation == "status":
                previous = None
                if db.path.exists():
                    async with readonly(db.path) as c:
                        version = await check_schema(c)
                    if version >= 2:
                        previous = await SQLiteSchedulerRepository(db).latest()
                at = self.now()
                self.output(
                    f"Enabled: {'yes' if settings.enabled else 'no'}\nCadence: 4h\n"
                    f"Candle delay: {settings.candle_delay_minutes}m\n"
                    f"Current UTC: {at.isoformat()}\n"
                    f"Previous slot: {previous.scheduled_for if previous else 'none'}\n"
                    f"Previous status: {previous.status.value if previous else 'none'}\n"
                    f"Previous analysis: {previous.analysis_run_id if previous else 'none'}\n"
                    f"Next planned run: {policy.window(at).next_run.isoformat()}"
                )
                return
            if args.operation == "run":
                require(settings.enabled, "Scheduler disabled; set SCHEDULER_ENABLED=true.")
            telegram_settings = TelegramSettings.from_environment()
            await db.initialize(create_parent=True)
            delivery = (
                compose_delivery(
                    db,
                    telegram_settings,
                    now=self.now,
                    sleep=self.sleep,
                    sender_factory=self.telegram_sender_factory,
                )
                if telegram_settings.enabled
                else None
            )

            async def after_completed(run_id):
                result = await delivery.deliver(run_id)
                self.output(f"Telegram delivery: {result.status.value}.")

            scheduler = Scheduler(
                settings,
                SQLiteSchedulerRepository(db),
                SQLiteAnalysisRunRepository(db),
                lambda record: compose_analysis(
                    db,
                    now=self.now,
                    provider_factory=self.provider_factory,
                    reasoner_factory=self.reasoner_factory,
                    scheduled=record,
                    sleep=self.sleep,
                ),
                now=self.now,
                sleep=self.sleep,
                output=self.output,
                on_completed=after_completed if delivery else None,
            )
            if args.operation == "run":
                await scheduler.run()
            else:
                outcome = await scheduler.once()
                if outcome.status is ScheduledStatus.FAILED:
                    raise AnalysisFailed(
                        f"Scheduled analysis failed ({outcome.failure_category.value})."
                    )
            return
        if args.command == "doctor" or args.command == "backup" and args.operation == "inspect":
            if args.command == "backup":
                require(args.path is not None and args.output is None, "Use backup inspect PATH.")
            report = await inspect_database(
                args.path if args.command == "backup" else db.path, now=self.now()
            )
            self.output(report.render())
            if report.failed:
                raise PersistenceError("Local integrity check failed; no repairs were attempted.")
            return
        if args.command == "backup":
            require(args.path is None, "Use backup --output PATH or backup inspect PATH.")
            path, _ = await backup_database(db.path, args.output, now=self.now())
            self.output(f"Verified backup: {path}")
            return
        if args.command in {"history", "status", "setups"}:
            async with readonly(db.path) as connection:
                await check_schema(connection)
        else:
            await db.initialize(create_parent=True)
        snapshots, runs = SQLiteSnapshotRepository(db), SQLiteAnalysisRunRepository(db)
        service = ConfirmedStateService(snapshots)
        current = await service.load_current()
        if args.command == "setups":
            last = await runs.latest_completed()
            self.output(
                render_setups(AnalysisResultCodec.decode(last.result_json).active, args.id)
                if last
                else "No completed analyses."
            )
            return
        if args.command == "history":
            if args.operation == "latest":
                last = await runs.latest_completed()
                self.output(
                    render_analysis(AnalysisResultCodec.decode(last.result_json))
                    if last
                    else "No completed analyses."
                )
            else:
                for stored in await runs.list_all_recent(args.limit):
                    regime = (
                        AnalysisResultCodec.decode(stored.result_json).market_regime.value
                        if stored.result_json
                        else "—"
                    )
                    self.output(
                        f"{stored.run.started_at} | {stored.run.status.value} | {regime} | "
                        f"snapshot {stored.run.snapshot_id} | run {stored.run.run_id}"
                    )
            return
        if args.command == "status":
            report = await inspect_database(db.path, now=self.now())
            self.output(
                f"Database bytes: {db.path.stat().st_size}; snapshots: {report.snapshots}; "
                f"runs: {report.runs}"
            )
            last = await runs.latest_completed()
            result = AnalysisResultCodec.decode(last.result_json) if last else None
            self.output(
                f"Database: {db.path}\nSnapshot: {current.snapshot.id if current else 'none'}\n"
                f"Revision: {current.revision if current else 0}"
            )
            count = (
                sum(o.status is LimitOrderStatus.OPEN for o in current.snapshot.orders)
                if current
                else 0
            )
            self.output(
                f"Positions: {len(current.snapshot.portfolio.positions) if current else 0}\n"
                f"Open orders: {count}"
            )
            self.output(
                f"Last completed analysis: {result.generated_at if result else 'none'}\n"
                f"Last market cutoff: {result.market_cutoff if result else 'none'}"
            )
            return
        if args.command == "analyze":
            result = await compose_analysis(
                db,
                now=self.now,
                provider_factory=self.provider_factory,
                reasoner_factory=self.reasoner_factory,
                ai_override=args.ai,
                source=args.source,
            ).execute()
            self.output(render_analysis(result))
            return
        if args.command != "setup":
            require(current is not None, "No confirmed portfolio. Run setup first.")
        if args.command in {"portfolio", "orders"} and args.operation == "show":
            self.output(f"Revision: {current.revision}\n" + render_snapshot(current.snapshot))
            return
        if args.command == "portfolio" and args.operation == "trade":
            self.output("RECORD AN ALREADY-EXECUTED SPOT TRADE\nNo exchange order will be sent.")
            trade = manual_trade_input(args, current.snapshot, self.input)
            recorder = RecordManualSpotTrade(snapshots, now=self.now)
            proposal = recorder.prepare(current, trade)
            self.output(render_manual_spot_trade(proposal))
            if self.input("Type yes to persist this state: ").strip().lower() != "yes":
                self.output("Declined; confirmed state unchanged.")
                return
            saved = await recorder.confirm(proposal)
            self.output(
                f"Confirmed snapshot {saved.snapshot.id}; revision {saved.revision}. "
                "No trade was executed by this application."
            )
            return
        at = self.now()
        if args.command == "portfolio" and args.operation == "core":
            previous = current.snapshot
            asset = parse_symbol(args.asset)
            require(asset in {"BTC", "ETH", "SOL"}, "Core must be a supported crypto asset.")
            configured = CorePosition(
                asset,
                parse_decimal(args.floor, positive=True),
                parse_decimal(args.preferred, positive=True),
            )
            snapshot = replace(
                previous,
                id=uuid4(),
                confirmed_at=at,
                core_positions=tuple(c for c in previous.core_positions if c.symbol != asset)
                + (configured,),
            )
            update_type = UpdateType.CORE_CONFIGURATION_CHANGE
        elif args.command == "setup" or args.command == "portfolio":
            if args.command == "setup":
                require(current is None, "Portfolio already exists; use portfolio replace.")
            snapshot = ConfirmedPortfolioSnapshot(
                uuid4(),
                at,
                balances(args),
                FinancialSettings(
                    parse_decimal(args.expenses, positive=True),
                    parse_decimal(args.runway, positive=True),
                ),
                current.snapshot.orders if current else (),
                current.snapshot.core_positions if current else (),
            )
            update_type = UpdateType.BALANCE_CORRECTION
        else:
            previous = current.snapshot
            if args.operation == "add":
                asset = parse_symbol(args.asset)
                require(asset in SYMBOL_IDS, "Unsupported asset.")
                order = LimitOrder(
                    uuid4(),
                    asset,
                    TradingSide(args.side),
                    parse_decimal(args.price, positive=True),
                    parse_decimal(args.quantity, positive=True),
                    LimitOrderStatus.OPEN,
                    at,
                    at,
                )
                snapshot = replace(
                    previous, id=uuid4(), confirmed_at=at, orders=previous.orders + (order,)
                )
                update_type = UpdateType.ORDER_ADDED
            else:
                old = next((o for o in previous.orders if o.id == args.id), None)
                require(
                    old is not None and old.status is LimitOrderStatus.OPEN,
                    "Exact OPEN order ID required.",
                )
                status = LimitOrderStatus(args.status)
                if status is LimitOrderStatus.FILLED:
                    require(
                        args.position is not None or args.empty,
                        "FILLED requires full reconciled balances via --position or --empty; "
                        "no proceeds are inferred.",
                    )
                    portfolio = balances(args)
                else:
                    require(
                        args.position is None and not args.empty,
                        "Use portfolio replace for independent balance corrections.",
                    )
                    portfolio = previous.portfolio
                order = replace(old, status=status, updated_at=at, resolved_at=at)
                snapshot = replace(
                    previous,
                    id=uuid4(),
                    confirmed_at=at,
                    portfolio=portfolio,
                    orders=tuple(order if o.id == old.id else o for o in previous.orders),
                )
                update_type = {
                    LimitOrderStatus.FILLED: UpdateType.ORDER_FILLED,
                    LimitOrderStatus.CANCELLED: UpdateType.ORDER_CANCELLED,
                    LimitOrderStatus.EXPIRED: UpdateType.ORDER_EXPIRED,
                }[status]
        self.output(
            "Proposed confirmed state (full balances; existing orders retained):\n"
            + render_snapshot(snapshot)
        )
        if self.input("Type yes to persist this state: ").strip().lower() != "yes":
            self.output("Declined; confirmed state unchanged.")
            return
        snapshot = replace(snapshot, confirmed_at=self.now())
        if current is None:
            saved = await service.initialize(snapshot)
        else:
            saved = await service.confirm(
                snapshot,
                expected_revision=current.revision,
                previous_snapshot_id=current.snapshot.id,
                update_type=update_type,
            )
        self.output(
            f"Confirmed snapshot {saved.snapshot.id}; revision {saved.revision}. "
            "No trades executed."
        )


def main(
    argv=None,
    *,
    input_fn=input,
    output=print,
    provider_factory=None,
    now=lambda: datetime.now(UTC),
    reasoner_factory=OpenAIReasoner,
    sleep=asyncio.sleep,
    telegram_sender_factory=None,
):
    p = parser()
    args = p.parse_args(argv)
    if args.command is None:
        p.print_help()
        return 0
    try:
        asyncio.run(
            CLI(
                input_fn=input_fn,
                output=output,
                provider_factory=provider_factory,
                now=now,
                reasoner_factory=reasoner_factory,
                sleep=sleep,
                telegram_sender_factory=telegram_sender_factory,
            ).run(args)
        )
        return 0
    except (AnalysisFailed, PersistenceError, ValueError, TelegramSendError) as error:
        output(f"Error: {error}")
        return 1
    except (OSError, sqlite3.Error):
        output("Error: local storage unavailable; check path, permissions and database integrity.")
        return 1
    except (KeyboardInterrupt, EOFError):
        output("Cancelled. No automatic trades were performed.")
        return 130
