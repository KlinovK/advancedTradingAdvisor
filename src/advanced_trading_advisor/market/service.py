from advanced_trading_advisor.portfolio.calculator import market_symbols
from advanced_trading_advisor.portfolio.models import ConfirmedPortfolioSnapshot

from .errors import MarketDataError
from .models import MarketSnapshot
from .provider import MarketDataProvider


class MarketDataService:
    """Portfolio coverage boundary only; no persistence, recommendations or orchestration."""

    def __init__(self, provider: MarketDataProvider):
        self.provider = provider

    async def for_snapshot(self, snapshot: ConfirmedPortfolioSnapshot) -> MarketSnapshot:
        symbols = market_symbols(snapshot)
        market = await self.provider.get_market_snapshot(symbols)
        if {a.symbol for a in market.assets} != set(symbols):
            raise MarketDataError("Provider did not return exactly the requested market coverage.")
        return market
