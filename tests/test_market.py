import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, getcontext, localcontext

import pytest

from advanced_trading_advisor.domain.numeric import calculation_context, parse_decimal
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.market.models import (
    Candle,
    DataCompleteness,
    MarketAssetData,
    MarketSnapshot,
    TechnicalFeatures,
)
from advanced_trading_advisor.market.provider import MarketDataPolicy
from advanced_trading_advisor.market.static import StaticMarketDataProvider
from advanced_trading_advisor.market.technical_features import (
    TechnicalFeatureCalculator,
    TechnicalFeatureConfig,
)

D = Decimal
NOW = datetime(2026, 9, 9, tzinfo=UTC)


def candle(**changes):
    return Candle(
        **(dict(closed_at=NOW, open=D("10"), high=D("11"), low=D("9"), close=D("10")) | changes)
    )


def test_static_is_deterministic_and_deduplicates():
    provider = StaticMarketDataProvider.development()
    a = asyncio.run(provider.get_market_snapshot(("BTC", "USDT", "USDC", "BTC")))
    b = asyncio.run(provider.get_market_snapshot(("BTC", "USDT", "USDC", "BTC")))
    assert a == b
    assert not a.is_live
    assert len(a.assets) == 3
    assert a.assets[1].price_usd == a.assets[2].price_usd == D("1")
    assert all(x.completeness is DataCompleteness.PRICE_ONLY for x in a.assets)
    assert asyncio.run(provider.get_market_snapshot(())).assets == ()


def test_static_copies_input_and_rejects_missing_prices():
    prices = {"BTC": D("1")}
    provider = StaticMarketDataProvider(prices, as_of=NOW)
    prices["BTC"] = D("2")
    assert asyncio.run(provider.get_market_snapshot(("BTC",))).assets[0].price_usd == D("1")
    with pytest.raises(DomainValidationError):
        asyncio.run(provider.get_market_snapshot(("UNKNOWN",)))
    with pytest.raises(DomainValidationError):
        StaticMarketDataProvider({"BTC": 1.0}, as_of=NOW)


@pytest.mark.parametrize(
    "changes",
    [
        {"open": D("0")},
        {"high": D("8")},
        {"low": D("12")},
        {"close": float("nan")},
        {"closed_at": NOW.replace(tzinfo=None)},
    ],
)
def test_invalid_candle(changes):
    with pytest.raises(DomainValidationError):
        candle(**changes)


def test_candle_order_and_gap_validation():
    a = candle()
    for b in (
        a,
        replace(a, closed_at=NOW - timedelta(hours=4)),
        replace(a, closed_at=NOW + timedelta(hours=8)),
    ):
        with pytest.raises(DomainValidationError):
            TechnicalFeatureCalculator().calculate((a, b))
    with pytest.raises(DomainValidationError):
        TechnicalFeatureCalculator().calculate([a])


def test_numeric_context_is_stable_and_does_not_leak():
    with localcontext() as outer:
        outer.prec = 4
        before = getcontext().copy()
        with calculation_context() as inner:
            assert inner.prec == 38
            assert D("1") / D("3") == D("0." + "3" * 38)
        assert getcontext().prec == before.prec
        text = "0.123456789012345678901234567890123456789"
        assert str(parse_decimal(text)) == text


def test_flat_and_falling_indicators():
    candles = tuple(replace(candle(), closed_at=NOW + timedelta(hours=4 * i)) for i in range(60))
    features = TechnicalFeatureCalculator().calculate(candles)
    assert features.rsi_4h == D("50")
    assert features.change_24h_pct == D("0")
    falling = tuple(
        Candle(NOW + timedelta(hours=4 * i), D(100 - i), D(101 - i), D(99 - i), D(100 - i))
        for i in range(60)
    )
    assert TechnicalFeatureCalculator().calculate(falling).rsi_4h == D("0")
    assert TechnicalFeatureCalculator().calculate(()) == TechnicalFeatures()


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "14"])
def test_period_configuration(value):
    with pytest.raises(DomainValidationError):
        TechnicalFeatureConfig(rsi_period=value)


def test_market_metadata_and_completeness():
    asset = MarketAssetData("BTC", D("1"), NOW)
    assert MarketSnapshot(NOW, (asset,)).assets == (asset,)
    with pytest.raises(DomainValidationError):
        MarketSnapshot(NOW, (asset, asset))
    with pytest.raises(DomainValidationError):
        replace(asset, completeness=DataCompleteness.COMPLETE)
    with pytest.raises(DomainValidationError):
        replace(asset, technical_features=TechnicalFeatures(rsi_4h=D("50")))
    partial = replace(
        asset,
        latest_closed_candle_at=NOW,
        technical_features=TechnicalFeatures(rsi_4h=D("50")),
        completeness=DataCompleteness.PARTIAL,
    )
    assert partial.latest_closed_candle_at == NOW
    full = TechnicalFeatureCalculator().calculate(
        tuple(replace(candle(), closed_at=NOW - timedelta(hours=4 * (59 - i))) for i in range(60))
    )
    assert replace(partial, technical_features=full, completeness=DataCompleteness.COMPLETE)
    for changes in (
        {"rsi_4h": D("101")},
        {"atr_4h": D("-1")},
        {"ema20_4h": D("0")},
        {"change_24h_pct": D("NaN")},
    ):
        with pytest.raises(DomainValidationError):
            TechnicalFeatures(**changes)
    with pytest.raises(DomainValidationError):
        MarketDataPolicy(max_quote_age=timedelta(0))
