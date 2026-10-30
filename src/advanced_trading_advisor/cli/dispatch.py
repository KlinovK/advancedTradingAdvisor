"""Finite cloud composition: process configuration only, no schema initialization."""

import os
from contextlib import contextmanager
from pathlib import Path

from advanced_trading_advisor.ai.settings import OpenAISettings
from advanced_trading_advisor.delivery.models import DeliveryStatus
from advanced_trading_advisor.domain.persistence import PersistenceError
from advanced_trading_advisor.domain.validation import require
from advanced_trading_advisor.market.settings import CoinGeckoSettings
from advanced_trading_advisor.scheduler.policy import SchedulerPolicy
from advanced_trading_advisor.scheduler.service import Scheduler
from advanced_trading_advisor.scheduler.settings import SchedulerSettings
from advanced_trading_advisor.storage.factory import create_storage
from advanced_trading_advisor.storage.settings import StorageBackend, StorageSettings
from advanced_trading_advisor.telegram.settings import TelegramSettings

from .composition import compose_analysis, compose_delivery


class CloudConfigurationError(PersistenceError):
    """A fixed diagnostic generated only at the configuration boundary."""


@contextmanager
def configuration_section(section):
    if section not in {"storage", "market", "openai", "telegram", "scheduler"}:
        raise RuntimeError("Unknown configuration section.")
    try:
        yield
    except ValueError:
        # Existing settings validators (including URL parsing) report invalid input as
        # ValueError subclasses. Never forward their messages or exception chains.
        raise CloudConfigurationError(f"Cloud dispatch configuration invalid: {section}.") from None


def preflight(environment):
    """Validate configuration without I/O; never load local dotenv or print inputs."""
    with configuration_section("storage"):
        storage = StorageSettings.from_environment(environment, sqlite_path=Path("unused.sqlite"))
        require(storage.backend is StorageBackend.POSTGRES, "Cloud dispatch requires PostgreSQL.")
    with configuration_section("market"):
        CoinGeckoSettings.from_environment(environment)
    with configuration_section("openai"):
        OpenAISettings.from_environment(environment)
    with configuration_section("telegram"):
        telegram = TelegramSettings.from_environment(environment)
    with configuration_section("scheduler"):
        scheduler = SchedulerSettings.from_environment(environment)
        require(scheduler.enabled, "Cloud scheduler disabled.")
    return storage, scheduler, telegram


async def run(cli):
    try:
        environment = dict(os.environ)
        storage_settings, settings, telegram_settings = preflight(environment)
    except CloudConfigurationError:
        raise
    except Exception:
        raise PersistenceError("Cloud dispatch preflight failed unexpectedly.") from None
    try:
        # Ineligible invocations don't even connect to storage. Recheck in dispatch after
        # readiness checks, so a slow connection cannot extend the catch-up window.
        if SchedulerPolicy(settings).catchup(cli.now()) is None:
            cli.output("Dispatch: no_eligible_slot")
            return
        storage = create_storage(storage_settings)
        async with storage.database.connection() as connection:
            async with connection.transaction(readonly=True):
                await storage.database.validate_legacy_schema(connection)
        require(await storage.snapshots.get_current() is not None, "Confirmed portfolio required.")
        delivery = (
            compose_delivery(
                storage,
                telegram_settings,
                now=cli.now,
                sleep=cli.sleep,
                sender_factory=cli.telegram_sender_factory,
            )
            if telegram_settings.enabled
            else None
        )
        delivery_failed = False

        async def after_completed(run_id):
            nonlocal delivery_failed
            try:
                record = await delivery.deliver(run_id)
                cli.output(f"Telegram delivery: {record.status.value}.")
                delivery_failed = record.status is not DeliveryStatus.DELIVERED
            except Exception:
                delivery_failed = True
                # Scheduler.once preserves durable analysis completion and sanitizes this.
                raise PersistenceError("Cloud delivery unavailable.") from None

        scheduler = Scheduler(
            settings,
            storage.scheduler,
            storage.analyses,
            lambda record: compose_analysis(
                storage,
                now=cli.now,
                provider_factory=cli.provider_factory,
                reasoner_factory=cli.reasoner_factory,
                scheduled=record,
                sleep=cli.sleep,
                environment=environment,
            ),
            now=cli.now,
            sleep=cli.sleep,
            output=cli.output,
            on_completed=after_completed if delivery else None,
        )
        outcome = await scheduler.dispatch()
        cli.output(f"Dispatch: {outcome}")
        if outcome == "failed" or delivery_failed:
            raise PersistenceError("Cloud dispatch did not complete successfully.")
    except Exception:
        raise PersistenceError(
            "Cloud dispatch failed; inspect persisted safe status metadata."
        ) from None
