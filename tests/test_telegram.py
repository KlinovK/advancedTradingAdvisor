"""Offline Telegram formatting, transport, persistence and scheduler delivery."""

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

import httpx
import pytest
from test_advisor import FakeProvider, fixture
from test_portfolio_risk import CUTOFF, NOW
from test_scheduler import Clock, Provider, prepared

from advanced_trading_advisor.analysis.active import TelegramSeverity
from advanced_trading_advisor.analysis.engine import advice
from advanced_trading_advisor.analysis.models import AnalysisRunStatus
from advanced_trading_advisor.analysis.reasoning import AI_WARNING, ReasoningMode
from advanced_trading_advisor.analysis.recommendations import ActionType
from advanced_trading_advisor.analysis.use_case import AnalyzeCurrentPortfolio
from advanced_trading_advisor.cli.main import main
from advanced_trading_advisor.delivery.models import (
    DeliveryFailureCategory as Failure,
)
from advanced_trading_advisor.delivery.models import (
    DeliveryRecord,
    DeliveryStatus,
)
from advanced_trading_advisor.portfolio.confirmed_state import ConfirmedStateService
from advanced_trading_advisor.portfolio.models import TradingSide
from advanced_trading_advisor.scheduler.service import Scheduler
from advanced_trading_advisor.scheduler.settings import SchedulerSettings
from advanced_trading_advisor.storage.codecs import utc_text
from advanced_trading_advisor.storage.database import Database
from advanced_trading_advisor.storage.delivery import SQLiteDeliveryRepository
from advanced_trading_advisor.storage.maintenance import inspect_database
from advanced_trading_advisor.storage.repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteSnapshotRepository,
)
from advanced_trading_advisor.storage.scheduler import SQLiteSchedulerRepository
from advanced_trading_advisor.telegram.formatter import (
    FOOTER,
    MAX_MESSAGE_LENGTH,
    format_analysis,
    message_units,
)
from advanced_trading_advisor.telegram.service import TelegramDeliveryService
from advanced_trading_advisor.telegram.settings import TelegramConfigurationError, TelegramSettings
from advanced_trading_advisor.telegram.transport import TelegramSendError, TelegramTransport

TOKEN = "123456:" + "A" * 32  # Synthetic, never a real credential.
CHAT = "123456789"


def run(coro):
    return asyncio.run(coro)


def settings():
    return TelegramSettings(True, TOKEN, CHAT)


class Sender:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome


async def completed(path):
    db = Database(path)
    await db.initialize()
    snapshots = SQLiteSnapshotRepository(db)
    runs = SQLiteAnalysisRunRepository(db)
    confirmed = await ConfirmedStateService(snapshots).initialize(fixture()[0])
    result = await AnalyzeCurrentPortfolio(snapshots, runs, FakeProvider, now=lambda: NOW).execute()
    return db, confirmed, result


def delivery_service(db, sender, *, at=NOW, sleep=None):
    async def no_wait(seconds):
        pass

    return TelegramDeliveryService(
        settings(),
        SQLiteDeliveryRepository(db),
        SQLiteAnalysisRunRepository(db),
        sender,
        now=lambda: at,
        sleep=sleep or no_wait,
    )


def test_configuration_defaults_validation_and_secret_redaction():
    assert TelegramSettings.from_environment({}) == TelegramSettings()
    valid = TelegramSettings.from_environment(
        {
            "TELEGRAM_ENABLED": "true",
            "TELEGRAM_BOT_TOKEN": TOKEN,
            "TELEGRAM_CHAT_ID": CHAT,
            "TELEGRAM_TIMEOUT_SECONDS": "12",
            "TELEGRAM_DISABLE_NOTIFICATION": "true",
        }
    )
    assert valid.enabled and valid.timeout_seconds == 12 and valid.disable_notification
    assert TOKEN not in repr(valid) and CHAT not in repr(valid)
    assert valid.destination_key == sha256(CHAT.encode()).hexdigest()
    for mapping in (
        {"TELEGRAM_ENABLED": "true", "TELEGRAM_CHAT_ID": CHAT},
        {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": TOKEN},
        {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": "bad", "TELEGRAM_CHAT_ID": CHAT},
        {"TELEGRAM_ENABLED": "true", "TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": "bad chat"},
        {"TELEGRAM_TIMEOUT_SECONDS": "0"},
    ):
        with pytest.raises(TelegramConfigurationError) as error:
            TelegramSettings.from_environment(mapping)
        assert TOKEN not in str(error.value)


def test_process_environment_overrides_optional_local_dotenv(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_ENABLED=true\nTELEGRAM_BOT_TOKEN=999999:"
        + "B" * 32
        + "\nTELEGRAM_CHAT_ID=999999\n"
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    configured = TelegramSettings.from_environment()
    assert configured.bot_token == TOKEN and configured.chat_id == CHAT


def test_formatter_covers_all_actions_exact_terms_ids_and_advisory(tmp_path):
    _, _, base = run(completed(tmp_path / "advisor.sqlite"))
    order_id = uuid4()
    actions = (
        advice(
            base.snapshot_id,
            "BTC",
            ActionType.BUY,
            "buy manually",
            side=TradingSide.BUY,
            quantity=Decimal("0.1"),
        ),
        advice(
            base.snapshot_id,
            "ETH",
            ActionType.SELL,
            "sell manually",
            side=TradingSide.SELL,
            quantity=Decimal("0.2"),
        ),
        advice(base.snapshot_id, "BTC", ActionType.HOLD, "hold"),
        advice(base.snapshot_id, None, ActionType.WAIT, "wait"),
        advice(
            base.snapshot_id,
            "SOL",
            ActionType.PLACE_LIMIT_ORDER,
            "place manually",
            side=TradingSide.BUY,
            price=Decimal("96.40"),
            quantity=Decimal("0.518"),
        ),
        advice(base.snapshot_id, "BTC", ActionType.KEEP_LIMIT_ORDER, "keep", order_id=order_id),
        advice(
            base.snapshot_id,
            "BTC",
            ActionType.CANCEL_LIMIT_ORDER,
            "cancel manually",
            order_id=order_id,
        ),
        advice(
            base.snapshot_id,
            "BTC",
            ActionType.REPLACE_LIMIT_ORDER,
            "replace manually",
            order_id=order_id,
            side=TradingSide.SELL,
            price=Decimal("102.30"),
            quantity=Decimal("0.25"),
        ),
        advice(base.snapshot_id, None, ActionType.REBALANCE, "rebalance manually"),
    )
    result = replace(base, recommendations=actions, active=None)
    message = format_analysis(result)
    assert all(
        name in message
        for name in (
            "Portfolio",
            "Reserve",
            "Market",
            "BUY",
            "SELL",
            "PLACE LIMIT ORDER",
            "CANCEL LIMIT ORDER",
            "REPLACE LIMIT ORDER",
        )
    )
    assert "Limit price: $96.40" in message and "Quantity: 0.518" in message
    assert "Approx value: $49.94" in message
    assert str(order_id) in message and "BTC — CANCEL LIMIT ORDER" in message
    assert "ORDER CANCELLED" not in message and message.endswith(FOOTER)
    assert f"Market data: {CUTOFF:%b %d, %H:%M UTC}" in message


def test_formatter_ai_modes_warnings_links_and_length(tmp_path):
    _, _, base = run(completed(tmp_path / "advisor.sqlite"))
    fallback = replace(
        base,
        reasoning_mode=ReasoningMode.AI_FALLBACK,
        reasoning_model="offline-model",
        ai_warning=AI_WARNING,
        configuration=None,
        active=None,
    )
    message = format_analysis(fallback)
    assert "AI fallback — deterministic analysis" in message and AI_WARNING in message
    assisted = replace(fallback, reasoning_mode=ReasoningMode.AI_ASSISTED, ai_warning=None)
    assert "AI-assisted analysis" in format_analysis(assisted)
    long_reason = "Please see https://malicious.invalid/path @someone " + "word " * 300
    actions = tuple(
        replace(
            advice(
                base.snapshot_id,
                "BTC",
                ActionType.PLACE_LIMIT_ORDER,
                long_reason,
                side=TradingSide.BUY,
                price=Decimal("96.40"),
                quantity=Decimal("0.518"),
            ),
            recommendation_id=uuid4(),
            priority=1 if index == 0 else 5,
        )
        for index in range(50)
    )
    bounded = format_analysis(
        replace(base, recommendations=actions, warnings=(long_reason,) * 20, active=None)
    )
    assert len(bounded) <= MAX_MESSAGE_LENGTH and bounded.endswith(FOOTER)
    assert "action(s) omitted" in bounded and "[link omitted]" in bounded
    assert "https://malicious.invalid" not in bounded and "@someone" not in bounded
    emoji_actions = tuple(
        replace(item, reason="🚀 " * 180, recommendation_id=uuid4()) for item in actions
    )
    emoji_message = format_analysis(replace(base, recommendations=emoji_actions, active=None))
    assert message_units(emoji_message) <= MAX_MESSAGE_LENGTH


def test_formatter_no_action_uses_human_money_warnings_and_actual_cutoff(tmp_path):
    _, _, base = run(completed(tmp_path / "advisor.sqlite"))
    financial = replace(
        base.financial,
        total_portfolio_usd=Decimal("14382.175737360114732987188"),
        current_stables_usd=Decimal("2595.5207790470166301"),
        stable_allocation_percent=Decimal("18.046788"),
        recommended_minimum_stables_usd=Decimal("1320"),
        deployable_stables_usd=Decimal("1275.52"),
        actual_expense_runway_months=Decimal("12.9776038952350831505"),
    )
    result = replace(
        base,
        financial=financial,
        recommendations=(advice(base.snapshot_id, "BTC", ActionType.HOLD, "hold"),),
        warnings=("excessive_asset_concentration", "unknown_risk_signal"),
        active=None,
    )
    message = format_analysis(result)
    assert message.startswith("MARKET UPDATE\nNo action required")
    assert "$14,382.18" in message
    assert "Cash: $2,595.52 (18.0%)" in message
    assert "Required: $1,320.00" in message
    assert "Available to deploy: $1,275.52" in message
    assert "Runway: 13.0 months" in message
    assert "BTC: HOLD" in message
    assert "High asset concentration" in message
    assert "Unknown risk signal" in message
    assert "ACTION NOW\nNONE" in message
    assert f"Market data: {CUTOFF:%b %d, %H:%M UTC}" in message
    assert message.endswith("No trades were executed automatically.")
    assert "Stables:" not in message and "Deployable:" not in message

    quiet = replace(result, active=replace(base.active, severity=TelegramSeverity.QUIET))
    quiet_message = format_analysis(quiet)
    assert quiet_message.startswith("MARKET UPDATE\nNo action required")
    assert "Cash: $2,595.52 (18.0%)" in quiet_message
    assert "ACTION NOW\nNONE" in quiet_message


def test_formatter_actionable_preserves_exact_execution_terms_without_internal_prose(tmp_path):
    _, _, base = run(completed(tmp_path / "advisor.sqlite"))
    setup_id, order_id = uuid4(), uuid4()
    action = replace(
        advice(
            base.snapshot_id,
            "SOL",
            ActionType.REPLACE_LIMIT_ORDER,
            "REPLACE LIMIT ORDER proposal passed whole-plan checks against the recorded snapshot. "
            "Review manually; nothing was executed.",
            order_id=order_id,
            setup_id=setup_id,
            side=TradingSide.BUY,
            price=Decimal("96.405"),
            quantity=Decimal("0.5181234567"),
            priority=1,
        ),
        invalidation_condition="Close below support",
        review_at=CUTOFF,
        expires_at=CUTOFF + timedelta(hours=4),
    )
    message = format_analysis(replace(base, recommendations=(action,), active=None))
    assert "ACTION NOW\nSOL — REPLACE LIMIT ORDER" in message
    assert "Side: BUY" in message and "Priority: P1" in message
    assert "Quantity: 0.5181234567" in message
    assert "Limit price: $96.405" in message
    assert f"Setup ID: {setup_id}" in message
    assert f"Existing order ID: {order_id}" in message
    assert "Invalidation: Close below support" in message
    assert "Review:" in message and "Expires:" in message
    assert "proposal passed whole-plan checks" not in message


def test_transport_success_plain_text_no_preview_or_parse_mode():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    sender = TelegramTransport(settings(), transport=httpx.MockTransport(handle))
    run(sender.send("harmless test"))
    assert len(requests) == 1
    payload = json.loads(requests[0].content)
    assert payload["text"] == "harmless test"
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert "parse_mode" not in payload


@pytest.mark.parametrize(
    "status,category,retryable",
    [
        (400, Failure.TELEGRAM_BAD_REQUEST, False),
        (401, Failure.TELEGRAM_UNAUTHORIZED, False),
        (403, Failure.TELEGRAM_FORBIDDEN, False),
        (429, Failure.TELEGRAM_RATE_LIMITED, True),
        (503, Failure.TELEGRAM_PROVIDER, True),
    ],
)
def test_transport_http_categories_are_sanitized(status, category, retryable):
    def handle(request):
        return httpx.Response(status, headers={"Retry-After": "2"}, text="private response")

    sender = TelegramTransport(settings(), transport=httpx.MockTransport(handle))
    with pytest.raises(TelegramSendError) as error:
        run(sender.send("test"))
    assert error.value.category is category and error.value.retryable is retryable
    assert TOKEN not in str(error.value) and "private" not in str(error.value)


def test_transport_timeout_connect_retryable_read_ambiguous():
    for error, category, retryable, ambiguous in (
        (httpx.ConnectTimeout("private"), Failure.TELEGRAM_TIMEOUT, True, False),
        (httpx.ReadTimeout("private"), Failure.TELEGRAM_AMBIGUOUS, False, True),
        (httpx.ConnectError("private"), Failure.TELEGRAM_TRANSPORT, True, False),
    ):
        sender = TelegramTransport(
            settings(),
            transport=httpx.MockTransport(lambda request, err=error: (_ for _ in ()).throw(err)),
        )
        with pytest.raises(TelegramSendError) as caught:
            run(sender.send("test"))
        assert (caught.value.category, caught.value.retryable, caught.value.ambiguous) == (
            category,
            retryable,
            ambiguous,
        )
        assert "private" not in str(caught.value)


def test_transport_telegram_json_retry_after_is_bounded():
    def reply(seconds):
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": seconds}})

    for seconds, expected in ((2, True), (9, False), ("2", False)):
        sender = TelegramTransport(
            settings(), transport=httpx.MockTransport(lambda request, s=seconds: reply(s))
        )
        with pytest.raises(TelegramSendError) as caught:
            run(sender.send("test"))
        assert caught.value.category is Failure.TELEGRAM_RATE_LIMITED
        assert caught.value.retryable is expected


def test_persisted_final_delivery_is_idempotent_and_secret_free(tmp_path):
    async def scenario():
        db, confirmed, result = await completed(tmp_path / "advisor.sqlite")
        sender = Sender()
        service = delivery_service(db, sender)
        first = await service.deliver(result.run_id)
        second = await service.deliver(result.run_id)
        assert first == second and first.status is DeliveryStatus.DELIVERED
        assert first.attempt_count == 1 and len(sender.messages) == 1
        assert sender.messages[0] == format_analysis(result)
        assert (
            await SQLiteAnalysisRunRepository(db).get(result.run_id)
        ).run.status is AnalysisRunStatus.COMPLETED
        assert await SQLiteSnapshotRepository(db).get_current() == confirmed
        assert TOKEN.encode() not in db.path.read_bytes()
        assert CHAT.encode() not in db.path.read_bytes()
        assert not (await inspect_database(db.path, now=NOW)).failed

    run(scenario())


def test_bounded_retry_and_persisted_attempt_count(tmp_path):
    async def scenario():
        db, _, result = await completed(tmp_path / "advisor.sqlite")
        waits = []

        async def fake_sleep(seconds):
            waits.append(seconds)

        sender = Sender(
            [
                TelegramSendError(Failure.TELEGRAM_TRANSPORT, retryable=True),
                TelegramSendError(Failure.TELEGRAM_PROVIDER, retryable=True),
            ]
        )
        delivered = await delivery_service(db, sender, sleep=fake_sleep).deliver(result.run_id)
        assert delivered.status is DeliveryStatus.DELIVERED
        assert delivered.attempt_count == 3 and waits == [1, 2]
        assert len(sender.messages) == 3
        sender = Sender(
            [TelegramSendError(Failure.TELEGRAM_RATE_LIMITED, retryable=True, retry_after=2)]
        )
        second_db, _, second_result = await completed(tmp_path / "second.sqlite")
        rate = await delivery_service(second_db, sender, sleep=fake_sleep).deliver(
            second_result.run_id
        )
        assert rate.status is DeliveryStatus.DELIVERED and rate.attempt_count == 2
        assert waits[-1] == 2

    run(scenario())


@pytest.mark.parametrize(
    "category",
    [Failure.TELEGRAM_UNAUTHORIZED, Failure.TELEGRAM_FORBIDDEN, Failure.TELEGRAM_BAD_REQUEST],
)
def test_permanent_error_no_retry_and_manual_failed_retry(tmp_path, category):
    async def scenario():
        db, _, result = await completed(tmp_path / "advisor.sqlite")
        sender = Sender([TelegramSendError(category)])
        service = delivery_service(db, sender)
        failed = await service.deliver(result.run_id)
        assert failed.status is DeliveryStatus.FAILED and failed.attempt_count == 1
        assert failed.failure_category is category
        assert await service.deliver(result.run_id) == failed
        assert len(sender.messages) == 1
        retried = await service.deliver(result.run_id, manual_retry=True)
        assert retried.status is DeliveryStatus.DELIVERED
        assert retried.delivery_id == failed.delivery_id and retried.attempt_count == 2

    run(scenario())


def test_ambiguous_response_never_automatically_resends(tmp_path):
    async def scenario():
        db, _, result = await completed(tmp_path / "advisor.sqlite")
        sender = Sender([TelegramSendError(Failure.TELEGRAM_AMBIGUOUS, ambiguous=True)])
        service = delivery_service(db, sender)
        ambiguous = await service.deliver(result.run_id)
        assert ambiguous.status is DeliveryStatus.AMBIGUOUS
        assert ambiguous.failure_category is Failure.TELEGRAM_AMBIGUOUS
        assert await service.deliver(result.run_id, manual_retry=True) == ambiguous
        assert len(sender.messages) == 1
        report = await inspect_database(db.path, now=NOW)
        assert not report.failed and "WARN ambiguous Telegram" in report.render()

    run(scenario())


def test_cancellation_during_http_never_marks_delivered(tmp_path):
    async def scenario():
        db, _, result = await completed(tmp_path / "advisor.sqlite")
        sender = Sender([asyncio.CancelledError()])
        with pytest.raises(asyncio.CancelledError):
            await delivery_service(db, sender).deliver(result.run_id)
        record = await SQLiteDeliveryRepository(db).latest()
        assert record.status is DeliveryStatus.AMBIGUOUS
        assert record.attempt_count == 1
        assert (
            await SQLiteAnalysisRunRepository(db).get(result.run_id)
        ).run.status is AnalysisRunStatus.COMPLETED

    run(scenario())


def test_stale_sending_recovery_distinguishes_no_attempt_and_ambiguous(tmp_path):
    async def scenario():
        db, _, result = await completed(tmp_path / "advisor.sqlite")
        repo = SQLiteDeliveryRepository(db)
        first = DeliveryRecord(uuid4(), result.run_id, settings().destination_key, NOW)
        claimed, active = await repo.claim(first)
        assert active and claimed.status is DeliveryStatus.SENDING
        second = DeliveryRecord(uuid4(), result.run_id, sha256(b"other-chat").hexdigest(), NOW)
        claimed_second, active = await repo.claim(second)
        assert active and claimed_second.attempt_count == 0
        await repo.start_attempt(claimed.delivery_id)
        report = await inspect_database(db.path, now=NOW + timedelta(minutes=16))
        assert "WARN stale SENDING" in report.render()
        await repo.recover_stale(NOW + timedelta(minutes=16))
        ambiguous = await repo.get(result.run_id, first.destination_key)
        pending = await repo.get(result.run_id, second.destination_key)
        assert ambiguous.status is DeliveryStatus.AMBIGUOUS
        assert pending.status is DeliveryStatus.PENDING
        reopened, active = await repo.claim(
            replace(second, delivery_id=uuid4(), created_at=NOW + timedelta(minutes=17))
        )
        assert active and reopened.delivery_id == pending.delivery_id
        assert reopened.status is DeliveryStatus.SENDING

    run(scenario())


def test_scheduler_completion_delivers_and_failure_is_separate(tmp_path):
    async def scenario():
        db, confirmed = await prepared(tmp_path / "advisor.sqlite")
        clock, provider = Clock(), Provider()
        sender = Sender([TelegramSendError(Failure.TELEGRAM_FORBIDDEN)])
        service = delivery_service(db, sender)
        from advanced_trading_advisor.cli.composition import compose_analysis

        scheduler = Scheduler(
            SchedulerSettings(),
            SQLiteSchedulerRepository(db),
            SQLiteAnalysisRunRepository(db),
            lambda slot: compose_analysis(
                db,
                now=clock.now,
                provider_factory=lambda: provider,
                reasoner_factory=lambda settings: None,
                ai_override=False,
                scheduled=slot,
                sleep=clock.sleep,
            ),
            now=clock.now,
            sleep=clock.sleep,
            output=lambda value: None,
            on_completed=service.deliver,
        )
        slot = await scheduler.once()
        assert slot.status.value == "COMPLETED"
        delivery = await SQLiteDeliveryRepository(db).latest()
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.analysis_run_id == slot.analysis_run_id
        assert (
            await SQLiteAnalysisRunRepository(db).get(slot.analysis_run_id)
        ).run.status is AnalysisRunStatus.COMPLETED
        assert await SQLiteSnapshotRepository(db).get_current() == confirmed
        assert len(sender.messages) == 1
        assert (await scheduler.once()).status.value == "COMPLETED"
        assert len(sender.messages) == 1

    run(scenario())


def test_cli_status_test_send_latest_and_duplicate_no_network_analysis(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    monkeypatch.setenv("OPENAI_ENABLED", "false")
    path = tmp_path / "advisor.sqlite"
    sender = Sender()
    output = []

    def invoke(operation):
        return main(
            ["--db", str(path), "telegram", operation],
            output=output.append,
            now=lambda: NOW,
            telegram_sender_factory=lambda settings: sender,
            provider_factory=lambda: (_ for _ in ()).throw(AssertionError("no market call")),
            reasoner_factory=lambda settings: (_ for _ in ()).throw(AssertionError("no AI call")),
        )

    assert invoke("status") == 0 and not path.exists()
    assert "Token configured: yes" in output[-1] and TOKEN not in output[-1]
    assert invoke("test") == 0 and "connection test" in sender.messages[-1]
    _, _, result = run(completed(path))
    assert invoke("send-latest") == 0
    assert invoke("send-latest") == 0
    assert "already delivered" in output[-1]
    assert len(sender.messages) == 2
    assert invoke("status") == 0 and "Latest delivery: DELIVERED" in output[-1]


@pytest.mark.parametrize(
    "telegram_enabled,delivery_fails", [(False, False), (True, False), (True, True)]
)
def test_cli_scheduler_once_delivery_is_separate_from_analysis(
    tmp_path, monkeypatch, telegram_enabled, delivery_fails
):
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    monkeypatch.setenv("OPENAI_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_ENABLED", "true" if telegram_enabled else "false")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT)
    path = tmp_path / "advisor.sqlite"
    run(prepared(path))
    sender = Sender([TelegramSendError(Failure.TELEGRAM_FORBIDDEN)] if delivery_fails else ())
    provider = Provider()
    output = []
    argv = ["--db", str(path), "scheduler", "once"]
    assert (
        main(
            argv,
            output=output.append,
            now=lambda: NOW,
            provider_factory=lambda: provider,
            telegram_sender_factory=lambda settings: sender,
        )
        == 0
    )
    slot = run(SQLiteSchedulerRepository(Database(path)).latest())
    latest = run(SQLiteAnalysisRunRepository(Database(path)).latest_completed())
    delivery = run(SQLiteDeliveryRepository(Database(path)).latest())
    assert slot.status.value == "COMPLETED" and latest.run.run_id == slot.analysis_run_id
    assert provider.calls == 1
    if telegram_enabled:
        assert delivery.status is (
            DeliveryStatus.FAILED if delivery_fails else DeliveryStatus.DELIVERED
        )
        assert len(sender.messages) == 1
    else:
        assert delivery is None and sender.messages == []
    assert (
        main(
            argv,
            output=output.append,
            now=lambda: NOW,
            provider_factory=lambda: provider,
            telegram_sender_factory=lambda settings: sender,
        )
        == 0
    )
    assert provider.calls == 1 and len(sender.messages) == int(telegram_enabled)


def test_v2_schema_migration_preserves_scheduler_data(tmp_path):
    from advanced_trading_advisor.storage.schema import DELIVERY_STATEMENTS, STATEMENTS

    path = tmp_path / "phase7.sqlite"
    owner = uuid4()
    slot = NOW.replace(minute=10)
    with sqlite3.connect(path) as connection:
        for statement in STATEMENTS[: -len(DELIVERY_STATEMENTS)]:
            connection.execute(statement)
        connection.execute("INSERT INTO schema_metadata VALUES (1,2)")
        connection.execute(
            "INSERT INTO scheduler_executions VALUES (?,?,?,?,?,?,?,?)",
            (
                utc_text(slot),
                utc_text(CUTOFF),
                str(owner),
                utc_text(NOW),
                utc_text(NOW),
                "FAILED",
                None,
                "interrupted",
            ),
        )

    async def scenario():
        db = Database(path)
        await db.initialize()
        async with db.connection() as connection:
            row = await (await connection.execute("SELECT version FROM schema_metadata")).fetchone()
            assert row[0] == 3
        saved = await SQLiteSchedulerRepository(db).get(slot)
        assert saved.owner_token == owner and saved.status.value == "FAILED"
        assert await SQLiteDeliveryRepository(db).latest() is None
        assert not (await inspect_database(path, now=NOW)).failed

    run(scenario())


def test_doctor_detects_dangling_delivery_reference(tmp_path):
    async def scenario():
        db, _, result = await completed(tmp_path / "advisor.sqlite")
        await delivery_service(db, Sender()).deliver(result.run_id)
        assert not (await inspect_database(db.path, now=NOW)).failed
        # Deliberately corrupt a disposable test DB to verify inspection catches it.
        with sqlite3.connect(db.path) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DROP TRIGGER telegram_transition_guard")
            connection.execute("UPDATE telegram_deliveries SET analysis_run_id=?", (str(uuid4()),))
        report = await inspect_database(db.path, now=NOW)
        assert report.failed and "FAIL Telegram delivery records/guards" in report.render()

    run(scenario())
