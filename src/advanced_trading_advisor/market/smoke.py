"""Manual diagnostics using the production provider/calculator; never called by pytest."""

import asyncio
import json
from datetime import UTC, datetime

from advanced_trading_advisor.domain.validation import DomainValidationError

from .errors import MarketDataError
from .live import CoinGeckoMarketDataProvider
from .regime import MarketRegimePolicy
from .settings import CoinGeckoSettings
from .technical_features import TechnicalFeatureCalculator


class CountingCalculator(TechnicalFeatureCalculator):
    def __init__(self):
        super().__init__()
        self.counts = []

    def calculate(self, candles):
        self.counts.append(len(candles))
        return super().calculate(candles)


async def check():
    settings = CoinGeckoSettings.from_environment()
    print("COINGECKO_API_KEY available: yes")
    calculator = CountingCalculator()
    provider = CoinGeckoMarketDataProvider(settings, feature_calculator=calculator)
    market = await provider.get_market_snapshot(("BTC", "ETH", "SOL"))
    now = datetime.now(UTC)
    policy = MarketRegimePolicy()
    regime = policy.assess(market, now=now)
    assert regime == policy.assess(market, now=now)
    assets = []
    for asset, count in zip(market.assets, calculator.counts, strict=True):
        features = asset.technical_features
        assets.append(
            {
                "symbol": asset.symbol,
                "price_usd": asset.price_usd,
                "quote_timestamp": asset.quoted_at,
                "latest_closed_4h_candle_timestamp": asset.latest_closed_candle_at,
                "usable_closed_candle_count": count,
                "rsi14": features.rsi_4h,
                "ema20": features.ema20_4h,
                "ema50": features.ema50_4h,
                "atr14": features.atr_4h,
                "support": features.support,
                "resistance": features.resistance,
                "change_24h_pct": features.change_24h_pct,
                "change_7d_pct": features.change_7d_pct,
                "data_completeness": asset.completeness,
            }
        )
    print(
        json.dumps(
            {
                "assets": assets,
                "common_market_cutoff": market.effective_market_cutoff,
                "market_regime": regime.regime,
                "regime_policy_version": regime.policy_version,
                "regime_reason": regime.reason,
            },
            default=str,
            indent=2,
        )
    )


def main():
    try:
        asyncio.run(check())
    except (MarketDataError, DomainValidationError) as error:
        # Only safe application error types; never dump settings or HTTP objects.
        print(f"Smoke failed: {type(error).__name__}: {error}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
