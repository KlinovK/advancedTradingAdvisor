"""Retry acquisition only; analysis/AI are never retried by this adapter."""

import asyncio

from advanced_trading_advisor.market.errors import ProviderUnavailableError, StaleMarketDataError

MARKET_ATTEMPTS = 2
MARKET_RETRY_DELAY_SECONDS = 30


class ScheduledMarketProvider:
    def __init__(self, provider, expected_cutoff, *, sleep=asyncio.sleep):
        self.provider, self.expected_cutoff, self.sleep = provider, expected_cutoff, sleep

    async def get_market_snapshot(self, symbols):
        for attempt in range(MARKET_ATTEMPTS):
            try:
                market = await self.provider.get_market_snapshot(
                    symbols, authoritative_cutoff=self.expected_cutoff
                )
                if market.effective_market_cutoff != self.expected_cutoff:
                    raise StaleMarketDataError("Market cutoff does not match the scheduled slot.")
                return market
            except (ProviderUnavailableError, StaleMarketDataError):
                if attempt + 1 == MARKET_ATTEMPTS:
                    raise
                await self.sleep(MARKET_RETRY_DELAY_SECONDS)
