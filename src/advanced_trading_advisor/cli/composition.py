"""Shared manual/scheduled analysis composition. Business logic stays in the use case."""

import asyncio
from decimal import Decimal

from advanced_trading_advisor.ai.settings import OpenAISettings
from advanced_trading_advisor.analysis.use_case import AnalyzeCurrentPortfolio
from advanced_trading_advisor.market.live import CoinGeckoMarketDataProvider
from advanced_trading_advisor.market.settings import CoinGeckoSettings
from advanced_trading_advisor.market.static import StaticMarketDataProvider
from advanced_trading_advisor.scheduler.market import ScheduledMarketProvider
from advanced_trading_advisor.storage.factory import as_storage
from advanced_trading_advisor.telegram.service import TelegramDeliveryService
from advanced_trading_advisor.telegram.transport import TelegramTransport

from .report import render_analysis


def compose_analysis(
    storage,
    *,
    now,
    provider_factory,
    reasoner_factory,
    ai_override=None,
    source="live",
    scheduled=None,
    sleep=asyncio.sleep,
):
    storage = as_storage(storage)
    settings = OpenAISettings.from_environment(enabled=ai_override)
    reasoner = reasoner_factory(settings) if settings.enabled else None

    def provider():
        if provider_factory:
            result = provider_factory()
        elif source == "static":
            result = StaticMarketDataProvider(
                {
                    "BTC": Decimal("60000"),
                    "ETH": Decimal("3000"),
                    "SOL": Decimal("150"),
                    "USDT": Decimal("1"),
                    "USDC": Decimal("1"),
                },
                as_of=now(),
            )
        else:
            result = CoinGeckoMarketDataProvider(CoinGeckoSettings.from_environment())
        return (
            ScheduledMarketProvider(result, scheduled.expected_cutoff, sleep=sleep)
            if scheduled
            else result
        )

    return AnalyzeCurrentPortfolio(
        storage.snapshots,
        storage.analyses,
        provider,
        now=now,
        reasoner=reasoner,
        prepare_report=render_analysis,
        run_id=scheduled.owner_token if scheduled else None,
    )


def compose_delivery(storage, settings, *, now, sleep=asyncio.sleep, sender_factory=None):
    storage = as_storage(storage)
    sender = sender_factory(settings) if sender_factory else TelegramTransport(settings)
    return TelegramDeliveryService(
        settings,
        storage.deliveries,
        storage.analyses,
        sender,
        now=now,
        sleep=sleep,
    )
