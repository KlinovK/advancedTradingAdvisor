import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from advanced_trading_advisor.analysis.active import SetupStatus
from advanced_trading_advisor.analysis.triggers import SwingTriggerEngine
from advanced_trading_advisor.domain.persistence import PayloadError
from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.market.errors import (
    MalformedMarketDataError,
    MarketCutoffMismatchError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
    StaleMarketDataError,
    UnsupportedSymbolError,
)
from advanced_trading_advisor.market.live import (
    SYMBOL_IDS,
    TRIGGER_OHLC_DAYS,
    CoinGeckoMarketDataProvider,
    decode_candles,
    decode_json,
    decode_trigger_candles,
)
from advanced_trading_advisor.market.models import Candle, DataCompleteness
from advanced_trading_advisor.market.provider import MarketDataPolicy
from advanced_trading_advisor.market.settings import CoinGeckoSettings
from advanced_trading_advisor.market.technical_features import TechnicalFeatureCalculator
from advanced_trading_advisor.portfolio.models import TradingSide
from advanced_trading_advisor.storage.market_codec import MarketSnapshotCodec

NOW = datetime(2026, 9, 9, 12, 30, tzinfo=UTC)
CUTOFF = NOW.replace(minute=0)
SETTINGS = CoinGeckoSettings("test-key-never-live")


def rows(count=60, cutoff=CUTOFF):
    return [
        [
            int((cutoff - timedelta(hours=4 * (count - 1 - i))).timestamp()) * 1000,
            100 + i,
            102 + i,
            99 + i,
            101 + i,
        ]
        for i in range(count)
    ]


def intraday_rows(count=48, cutoff=CUTOFF):
    return [
        [
            int((cutoff - timedelta(minutes=30 * (count - 1 - i))).timestamp()) * 1000,
            100 + i,
            102 + i,
            99 + i,
            101 + i,
        ]
        for i in range(count)
    ]


def quotes():
    return {
        identifier: {"usd": 100, "last_updated_at": int(NOW.timestamp())}
        for identifier in SYMBOL_IDS.values()
    }


def run_provider(
    handler,
    symbols=("BTC",),
    settings=SETTINGS,
    now=lambda: NOW,
    authoritative_cutoff=None,
):
    return asyncio.run(
        CoinGeckoMarketDataProvider(
            settings,
            transport=httpx.MockTransport(handler),
            now=now,
        ).get_market_snapshot(symbols, authoritative_cutoff=authoritative_cutoff)
    )


def test_quote_mapping_decimal_precision_and_closed_cutoff():
    seen = []

    def handler(request):
        seen.append(request)
        assert request.url.scheme == "https"
        assert request.headers["x-cg-demo-api-key"] == SETTINGS.api_key
        if request.url.path.endswith("/simple/price"):
            assert request.url.params["include_last_updated_at"] == "true"
            assert request.url.params["ids"] == "bitcoin,ethereum,solana,usd-coin,tether"
            text = json.dumps(quotes()).replace(
                '"usd": 100', '"usd": 0.99912345678901234567890123456789'
            )
            return httpx.Response(200, content=text)
        days = request.url.params["days"]
        assert days in {"1", "30"}
        return httpx.Response(
            200,
            json=list(reversed(intraday_rows() if days == "1" else rows())),
        )

    market = run_provider(handler, ("BTC", "SOL", "ETH", "USDT", "USDC", "BTC"))
    assert len(seen) == 9
    assert TRIGGER_OHLC_DAYS == "1"
    assert sum(request.url.params.get("days") == "1" for request in seen) == 3
    assert all(request.url.params.get("days") != "2" for request in seen)
    assert len(market.assets) == 5
    assert market.is_live
    assert market.completeness is DataCompleteness.COMPLETE
    assert market.effective_market_cutoff == CUTOFF
    assert market.assets[0].quoted_at == NOW != CUTOFF
    assert market.assets[0].latest_closed_price_usd == Decimal("160")
    assert len(market.assets[0].closed_1h_candles) == 24
    assert market.assets[0].closed_1h_candles[-1].closed_at == CUTOFF
    assert market.assets[0].price_usd == Decimal("0.99912345678901234567890123456789")
    assert MarketSnapshotCodec.decode(MarketSnapshotCodec.encode(market)) == market


def test_unsupported_asset_fails_before_network():
    def handler(request):
        pytest.fail("Unsupported asset must fail before requests")

    with pytest.raises(UnsupportedSymbolError):
        run_provider(handler, ("UNKNOWN",))
    assert run_provider(handler, ()).completeness is DataCompleteness.UNAVAILABLE


@pytest.mark.parametrize(
    "raw_cutoff,expected_count,expected_cutoff",
    [
        (CUTOFF, 24, CUTOFF),
        (CUTOFF + timedelta(minutes=30), 23, CUTOFF),
    ],
)
def test_real_30_minute_shape_aggregates_only_complete_1h_buckets(
    raw_cutoff, expected_count, expected_cutoff
):
    payload = decode_json(json.dumps(intraday_rows(cutoff=raw_cutoff)).encode())
    candles = decode_trigger_candles(
        payload,
        now=raw_cutoff + timedelta(minutes=30),
        policy=MarketDataPolicy(),
    )
    assert len(candles) == expected_count
    assert candles[-1].closed_at == expected_cutoff
    assert all(
        later.closed_at - earlier.closed_at == timedelta(hours=1)
        for earlier, later in zip(candles, candles[1:], strict=False)
    )


def test_authoritative_cutoff_caps_4h_1h_features_and_daily_context():
    expected = CUTOFF - timedelta(hours=4)

    def handler(request):
        if request.url.path.endswith("price"):
            return httpx.Response(200, json=quotes())
        return httpx.Response(
            200,
            json=intraday_rows(cutoff=CUTOFF)
            if request.url.params["days"] == "1"
            else rows(cutoff=CUTOFF),
        )

    result = run_provider(handler, authoritative_cutoff=expected)
    value = result.assets[0]
    raw_4h = decode_candles(
        decode_json(json.dumps(rows(cutoff=CUTOFF)).encode()),
        now=NOW,
        policy=MarketDataPolicy(),
    )
    capped_4h = tuple(candle for candle in raw_4h if candle.closed_at <= expected)
    calculator = TechnicalFeatureCalculator()
    assert value.latest_closed_candle_at == expected
    assert value.latest_closed_price_usd == capped_4h[-1].close
    assert value.technical_features == calculator.calculate(capped_4h)
    assert value.daily_context == calculator.daily_context(capped_4h)
    assert value.closed_1h_candles[-1].closed_at == expected
    assert all(candle.closed_at <= expected for candle in value.closed_1h_candles)


def test_same_authoritative_cutoff_is_replay_stable_across_execution_times():
    expected = CUTOFF - timedelta(hours=4)

    def collect(executed_at, trigger_end):
        def handler(request):
            if request.url.path.endswith("price"):
                current_quotes = quotes()
                for value in current_quotes.values():
                    value["last_updated_at"] = int(executed_at.timestamp())
                return httpx.Response(200, json=current_quotes)
            if request.url.params["days"] == "1":
                data = intraday_rows(cutoff=trigger_end)
                for row in data:
                    row[1:] = [100, 102, 99, 101]
                return httpx.Response(200, json=data)
            return httpx.Response(200, json=rows(cutoff=expected))

        return run_provider(
            handler,
            now=lambda: executed_at,
            authoritative_cutoff=expected,
        ).assets[0]

    early = collect(expected + timedelta(minutes=10), expected)
    delayed = collect(expected + timedelta(hours=2, minutes=54), expected + timedelta(hours=2))
    assert early.latest_closed_candle_at == delayed.latest_closed_candle_at == expected
    assert early.technical_features == delayed.technical_features
    assert early.daily_context == delayed.daily_context
    relevant_early = tuple(
        candle
        for candle in early.closed_1h_candles
        if candle.closed_at >= expected - timedelta(hours=3)
    )
    relevant_delayed = tuple(
        candle
        for candle in delayed.closed_1h_candles
        if candle.closed_at >= expected - timedelta(hours=3)
    )
    assert relevant_early == relevant_delayed

    trigger = SwingTriggerEngine()
    initial = trigger.initial_context(level=Decimal("100"), atr=Decimal("10"))
    status, persisted = trigger.evaluate(
        status=SetupStatus.CANDIDATE,
        context=initial,
        direction=TradingSide.SELL,
        candles=(
            Candle(
                expected - timedelta(hours=4),
                Decimal("100"),
                Decimal("102"),
                Decimal("99"),
                Decimal("101"),
            ),
        ),
        latest_4h_close=None,
        latest_4h_cutoff=None,
        invalidation_price=Decimal("1000"),
    )
    early_trigger = trigger.evaluate(
        status=status,
        context=persisted,
        direction=TradingSide.SELL,
        candles=early.closed_1h_candles,
        latest_4h_close=early.latest_closed_price_usd,
        latest_4h_cutoff=early.latest_closed_candle_at,
        invalidation_price=Decimal("1000"),
    )
    delayed_trigger = trigger.evaluate(
        status=status,
        context=persisted,
        direction=TradingSide.SELL,
        candles=delayed.closed_1h_candles,
        latest_4h_close=delayed.latest_closed_price_usd,
        latest_4h_cutoff=delayed.latest_closed_candle_at,
        invalidation_price=Decimal("1000"),
    )
    assert early_trigger == delayed_trigger


def test_per_asset_1h_lag_is_preserved_without_portfolio_failure():
    def handler(request):
        if request.url.path.endswith("price"):
            return httpx.Response(200, json=quotes())
        if request.url.params["days"] == "1":
            cutoff = CUTOFF if "bitcoin" in request.url.path else CUTOFF - timedelta(hours=1)
            return httpx.Response(200, json=intraday_rows(cutoff=cutoff))
        return httpx.Response(200, json=rows())

    result = run_provider(handler, symbols=("BTC", "ETH"))
    latest = {asset.symbol: asset.closed_1h_candles[-1].closed_at for asset in result.assets}
    assert latest == {"BTC": CUTOFF, "ETH": CUTOFF - timedelta(hours=1)}
    assert result.effective_market_cutoff == CUTOFF


@pytest.mark.parametrize("value", [0, -1, None, "1", True])
def test_invalid_quote_price(value):
    data = quotes()
    data["bitcoin"]["usd"] = value
    with pytest.raises(MalformedMarketDataError):
        run_provider(lambda request: httpx.Response(200, json=data))


@pytest.mark.parametrize("value", [None, True, "100", -1, 0, 1.5, 10**30])
def test_invalid_quote_timestamp(value):
    data = quotes()
    data["bitcoin"]["last_updated_at"] = value
    with pytest.raises(MalformedMarketDataError):
        run_provider(lambda request: httpx.Response(200, json=data))


@pytest.mark.parametrize(
    "delta,error", [(-601, StaleMarketDataError), (1, MalformedMarketDataError)]
)
def test_stale_or_future_quote(delta, error):
    data = quotes()
    data["bitcoin"]["last_updated_at"] += delta
    with pytest.raises(error):
        run_provider(lambda request: httpx.Response(200, json=data))


@pytest.mark.parametrize(
    "content", [b"null", b"[]", b"{}", b"not json", b'{"x":NaN}', b'{"x":1,"x":2}', b"\xff"]
)
def test_malformed_quote_response(content):
    with pytest.raises(MalformedMarketDataError):
        run_provider(lambda request: httpx.Response(200, content=content))


@pytest.mark.parametrize(
    "kind", ["future", "duplicate", "gap", "off_grid", "invalid_ohlc", "shape", "lag"]
)
def test_invalid_candles(kind):
    candles = rows()
    if kind == "future":
        candles.append(rows(1, CUTOFF + timedelta(hours=4))[0])
    elif kind == "duplicate":
        candles.append(candles[-1])
    elif kind == "gap":
        del candles[-2]
    elif kind == "off_grid":
        candles = rows(cutoff=CUTOFF - timedelta(minutes=1))
    elif kind == "invalid_ohlc":
        candles[-1][2] = 1
    elif kind == "shape":
        candles[-1] = [1, 2]
    else:
        candles = rows(cutoff=CUTOFF - timedelta(hours=8))

    def handler(request):
        return httpx.Response(200, json=quotes() if request.url.path.endswith("price") else candles)

    with pytest.raises(StaleMarketDataError if kind == "lag" else MalformedMarketDataError):
        run_provider(handler)


@pytest.mark.parametrize(
    "count,completeness",
    [
        (0, DataCompleteness.PRICE_ONLY),
        (2, DataCompleteness.PRICE_ONLY),
        (20, DataCompleteness.PARTIAL),
    ],
)
def test_insufficient_history_is_explicit(count, completeness):
    def handler(request):
        return httpx.Response(
            200,
            json=quotes()
            if request.url.path.endswith("price")
            else []
            if request.url.params["days"] == "1"
            else rows(count),
        )

    result = run_provider(handler)
    assert result.assets[0].completeness is completeness
    assert result.assets[0].technical_features.ema50_4h is None
    assert result.completeness is DataCompleteness.PARTIAL
    assert result.effective_market_cutoff == (CUTOFF if count else None)


def test_multi_asset_cutoff_mismatch_rejected():
    def handler(request):
        if request.url.path.endswith("price"):
            return httpx.Response(200, json=quotes())
        if request.url.params["days"] == "1":
            return httpx.Response(200, json=intraday_rows())
        cutoff = CUTOFF - timedelta(hours=4) if "ethereum" in request.url.path else CUTOFF
        return httpx.Response(200, json=rows(cutoff=cutoff))

    with pytest.raises(MarketCutoffMismatchError):
        run_provider(handler, ("BTC", "ETH"))


@pytest.mark.parametrize("failure", ["server", "timeout"])
def test_bounded_retry(failure):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("sensitive upstream message", request=request)
            return httpx.Response(503)
        return httpx.Response(200, json=quotes() if request.url.path.endswith("price") else rows())

    assert run_provider(handler).assets
    assert attempts == 4


@pytest.mark.parametrize(
    "status,error,count",
    [
        (429, ProviderRateLimitedError, 1),
        (400, ProviderUnavailableError, 1),
        (401, ProviderUnavailableError, 1),
        (302, ProviderUnavailableError, 1),
        (503, ProviderUnavailableError, 2),
    ],
)
def test_http_failure_policy(status, error, count):
    attempts = 0

    def handler(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, text="private upstream response")

    with pytest.raises(error) as caught:
        run_provider(handler)
    assert "private" not in str(caught.value)
    assert attempts == count


def test_cancel_propagates_and_exhausted_transport_is_sanitized():
    def cancelled(request):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        run_provider(cancelled)

    def timed_out(request):
        raise httpx.ReadTimeout("secret", request=request)

    with pytest.raises(ProviderUnavailableError) as caught:
        run_provider(timed_out)
    assert "secret" not in str(caught.value)
    assert caught.value.__suppress_context__


def test_quotes_revalidated_at_fetch_completion():
    times = iter((NOW, NOW, NOW, NOW, NOW + timedelta(minutes=11)))

    def handler(request):
        return httpx.Response(200, json=quotes() if request.url.path.endswith("price") else rows())

    with pytest.raises(StaleMarketDataError):
        run_provider(handler, now=lambda: next(times))


def test_settings_no_secret_repr_and_env_validation():
    assert SETTINGS.api_key not in repr(SETTINGS)
    assert CoinGeckoSettings.from_environment({"COINGECKO_API_KEY": "test"}).timeout_seconds == 10
    for env in (
        {},
        {"COINGECKO_API_KEY": "test", "COINGECKO_API_TIER": "pro"},
        {"COINGECKO_API_KEY": "test", "MARKET_DATA_TIMEOUT_SECONDS": "nan"},
        {"COINGECKO_API_KEY": "test", "MARKET_DATA_TIMEOUT_SECONDS": "0"},
    ):
        with pytest.raises(DomainValidationError):
            CoinGeckoSettings.from_environment(env)
    with pytest.raises(DomainValidationError):
        replace(SETTINGS, api_key="test\nheader")
    assert decode_json(b'{"price":1.0000000000000000000001}')["price"] == Decimal(
        "1.0000000000000000000001"
    )


def test_market_codec_rejects_false_metadata_and_version():
    def handler(request):
        return httpx.Response(200, json=quotes() if request.url.path.endswith("price") else rows())

    payload = json.loads(MarketSnapshotCodec.encode(run_provider(handler)))
    payload["data"]["effective_market_cutoff"] = None
    with pytest.raises(PayloadError):
        MarketSnapshotCodec.decode(json.dumps(payload))
    payload["schema_version"] = 2
    with pytest.raises(PayloadError):
        MarketSnapshotCodec.decode(json.dumps(payload))


def test_gapped_intraday_trigger_data_is_rejected_not_fabricated():
    trigger = intraday_rows()
    del trigger[-2]

    def handler(request):
        if request.url.path.endswith("price"):
            return httpx.Response(200, json=quotes())
        return httpx.Response(200, json=trigger if request.url.params["days"] == "1" else rows())

    with pytest.raises(MalformedMarketDataError):
        run_provider(handler)
