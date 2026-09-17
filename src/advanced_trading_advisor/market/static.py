from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from advanced_trading_advisor.domain.numeric import finite_decimal
from advanced_trading_advisor.domain.validation import require, symbol, timestamp

from .models import MarketAssetData, MarketSnapshot


class StaticMarketDataProvider:
    """Offline fixture provider adapted from CryptoPortfolioAdvisor's static adapter."""

    def __init__(self, prices: Mapping[str, Decimal], *, as_of: datetime):
        require(isinstance(prices, Mapping), "Prices must be a mapping.")
        timestamp(as_of)
        for asset, price in prices.items():
            symbol(asset)
            finite_decimal(price, positive=True)
        self._prices = dict(prices)
        self._as_of = as_of

    @classmethod
    def development(cls) -> "StaticMarketDataProvider":
        return cls(
            {
                "BTC": Decimal("60000"),
                "ETH": Decimal("3000"),
                "SOL": Decimal("150"),
                "LINK": Decimal("15"),
                "USDT": Decimal("1"),
                "USDC": Decimal("1"),
            },
            as_of=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def get_market_snapshot(
        self,
        symbols: tuple[str, ...],
        *,
        authoritative_cutoff: datetime | None = None,
    ) -> MarketSnapshot:
        require(isinstance(symbols, tuple), "Symbols must be a tuple.")
        if authoritative_cutoff is not None:
            timestamp(authoritative_cutoff)
        for asset in symbols:
            symbol(asset)
            require(asset in self._prices, f"Static price unavailable for {asset}.")
        return MarketSnapshot(
            as_of=self._as_of,
            assets=tuple(
                MarketAssetData(symbol=asset, price_usd=self._prices[asset], quoted_at=self._as_of)
                for asset in dict.fromkeys(symbols)
            ),
        )
