"""Read-only adapter adapted from CryptoPortfolioAdvisor; no old app imports or static fallback."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import httpx

from advanced_trading_advisor.domain.validation import (
    DomainValidationError,
    require,
    symbol,
    timestamp,
)

from .errors import (
    MalformedMarketDataError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
    UnsupportedSymbolError,
)
from .models import Candle, DataCompleteness, MarketAssetData, MarketSnapshot
from .provider import MarketDataPolicy
from .settings import CoinGeckoSettings
from .technical_features import TechnicalFeatureCalculator
from .validation import (
    normalize_closed_1h_candles,
    normalize_closed_candles,
    validate_cutoffs,
    validate_snapshot_quotes,
)

SYMBOL_IDS = MappingProxyType(
    {"BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "USDT": "tether", "USDC": "usd-coin"}
)
BASE_URL = "https://api.coingecko.com/api/v3"
TRIGGER_OHLC_DAYS = "1"
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def positive(value: object) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise MalformedMarketDataError("Invalid market price.")
    return value


def unix_time(value: object, *, milliseconds: bool = False) -> datetime:
    if (
        not isinstance(value, Decimal)
        or not value.is_finite()
        or value <= 0
        or value != value.to_integral_value()
    ):
        raise MalformedMarketDataError("Invalid market timestamp.")
    try:
        delta = (
            timedelta(milliseconds=int(value)) if milliseconds else timedelta(seconds=int(value))
        )
        return EPOCH + delta
    except (OverflowError, ValueError):
        raise MalformedMarketDataError("Market timestamp outside supported range.") from None


def decode_json(content: bytes):
    def invalid(_value):
        raise ValueError

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    try:
        return json.loads(
            content,
            parse_float=Decimal,
            parse_int=Decimal,
            parse_constant=invalid,
            object_pairs_hook=unique_pairs,
        )
    except (ValueError, UnicodeDecodeError):
        raise MalformedMarketDataError("Malformed provider JSON.") from None


def decode_candle_rows(payload: object) -> tuple[Candle, ...]:
    if not isinstance(payload, list):
        raise MalformedMarketDataError("Expected OHLC array.")
    candles = []
    for row in payload:
        if not isinstance(row, list) or len(row) != 5:
            raise MalformedMarketDataError("Invalid OHLC row.")
        try:
            candles.append(
                Candle(
                    unix_time(row[0], milliseconds=True), *(positive(value) for value in row[1:])
                )
            )
        except DomainValidationError:
            raise MalformedMarketDataError("Invalid OHLC range.") from None
    return tuple(candles)


def decode_candles(payload: object, *, now: datetime, policy: MarketDataPolicy):
    return normalize_closed_candles(decode_candle_rows(payload), now=now, policy=policy)


def decode_trigger_candles(payload: object, *, now: datetime, policy: MarketDataPolicy):
    return normalize_closed_1h_candles(decode_candle_rows(payload), now=now, policy=policy)


class CoinGeckoMarketDataProvider:
    def __init__(
        self,
        settings: CoinGeckoSettings,
        *,
        policy: MarketDataPolicy | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        transport: httpx.AsyncBaseTransport | None = None,
        feature_calculator: TechnicalFeatureCalculator | None = None,
    ):
        self._settings = settings
        self._policy = policy or MarketDataPolicy()
        self._now = now
        self._transport = transport
        self._features = feature_calculator or TechnicalFeatureCalculator()

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, str]):
        for attempt in range(self._settings.max_retries + 1):
            try:
                # Total wall-clock cap per attempt, in addition to httpx per-I/O timeouts.
                async with asyncio.timeout(self._settings.timeout_seconds):
                    response = await client.get(BASE_URL + path, params=params)
            except (httpx.RequestError, TimeoutError):
                if attempt == self._settings.max_retries:
                    raise ProviderUnavailableError("Market provider transport failed.") from None
            else:
                if response.status_code == 429:
                    raise ProviderRateLimitedError("Market provider rate limit reached.")
                if 200 <= response.status_code < 300:
                    return decode_json(response.content)
                if response.status_code < 500 or attempt == self._settings.max_retries:
                    raise ProviderUnavailableError("Market provider request failed.")
            await asyncio.sleep(0.1)
        raise ProviderUnavailableError("Market provider request failed.")

    async def get_market_snapshot(
        self,
        symbols: tuple[str, ...],
        *,
        authoritative_cutoff: datetime | None = None,
    ) -> MarketSnapshot:
        require(isinstance(symbols, tuple), "Symbols must be a tuple.")
        unique = tuple(sorted(set(symbols)))
        for asset in unique:
            symbol(asset)
            if asset not in SYMBOL_IDS:
                raise UnsupportedSymbolError(f"Unsupported market symbol: {asset}.")
        at = self._now()
        timestamp(at)
        if authoritative_cutoff is not None:
            timestamp(authoritative_cutoff)
            authoritative_cutoff = authoritative_cutoff.astimezone(UTC)
            require(authoritative_cutoff <= at, "Market cutoff cannot be in the future.")
        if not unique:
            return MarketSnapshot(at.astimezone(UTC), (), is_live=True)
        async with httpx.AsyncClient(
            headers={"x-cg-demo-api-key": self._settings.api_key},
            timeout=self._settings.timeout_seconds,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            quotes = await self._get(
                client,
                "/simple/price",
                {
                    "ids": ",".join(SYMBOL_IDS[s] for s in unique),
                    "vs_currencies": "usd",
                    "include_last_updated_at": "true",
                    "precision": "full",
                },
            )
            if not isinstance(quotes, dict):
                raise MalformedMarketDataError("Expected quote object.")
            prices = {}
            for asset in unique:
                entry = quotes.get(SYMBOL_IDS[asset])
                if not isinstance(entry, dict):
                    raise MalformedMarketDataError("Required quote is missing.")
                prices[asset] = positive(entry.get("usd")), unix_time(entry.get("last_updated_at"))
            # Reject stale quotes before spending requests on history.
            quote_received_at = self._now()
            validate_snapshot_quotes(
                MarketSnapshot(
                    quote_received_at, tuple(MarketAssetData(s, *prices[s]) for s in unique)
                ),
                now=quote_received_at,
                policy=self._policy,
            )
            assets = []
            for asset in unique:
                payload = await self._get(
                    client,
                    f"/coins/{SYMBOL_IDS[asset]}/ohlc",
                    {"vs_currency": "usd", "days": "30", "precision": "full"},
                )
                candles = decode_candles(payload, now=self._now(), policy=self._policy)
                if authoritative_cutoff is not None:
                    candles = tuple(
                        candle for candle in candles if candle.closed_at <= authoritative_cutoff
                    )
                trigger_candles = ()
                if asset not in {"USDT", "USDC"}:
                    trigger_payload = await self._get(
                        client,
                        f"/coins/{SYMBOL_IDS[asset]}/ohlc",
                        {
                            "vs_currency": "usd",
                            "days": TRIGGER_OHLC_DAYS,
                            "precision": "full",
                        },
                    )
                    trigger_candles = decode_trigger_candles(
                        trigger_payload, now=self._now(), policy=self._policy
                    )
                    if authoritative_cutoff is not None:
                        trigger_candles = tuple(
                            candle
                            for candle in trigger_candles
                            if candle.closed_at <= authoritative_cutoff
                        )
                features = self._features.calculate(candles)
                present = [getattr(features, f.name) is not None for f in fields(features)]
                completeness = (
                    DataCompleteness.COMPLETE
                    if all(present)
                    else DataCompleteness.PARTIAL
                    if any(present)
                    else DataCompleteness.PRICE_ONLY
                )
                assets.append(
                    MarketAssetData(
                        asset,
                        *prices[asset],
                        candles[-1].closed_at if candles else None,
                        features,
                        completeness,
                        candles[-1].close if candles else None,
                        self._features.daily_context(candles),
                        trigger_candles,
                    )
                )
            finished = self._now()
            timestamp(finished)
            result = MarketSnapshot(finished.astimezone(UTC), tuple(assets), is_live=True)
            # Revalidate at completion, not only against the request's earlier start clock.
            validate_snapshot_quotes(result, now=finished, policy=self._policy)
            validate_cutoffs(result, now=finished, policy=self._policy)
            return result
