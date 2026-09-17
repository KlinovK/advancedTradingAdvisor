from datetime import UTC, datetime, timedelta

from advanced_trading_advisor.domain.validation import items, timestamp

from .errors import MalformedMarketDataError, MarketCutoffMismatchError, StaleMarketDataError
from .models import Candle, MarketSnapshot
from .provider import MarketDataPolicy

INTERVAL = timedelta(hours=4)
TRIGGER_INTERVAL = timedelta(hours=1)
PROVIDER_INTRADAY_INTERVAL = timedelta(minutes=30)


def validate_quote_time(quoted_at: datetime, now: datetime, policy: MarketDataPolicy) -> None:
    timestamp(now)
    timestamp(quoted_at)
    if quoted_at > now:
        raise MalformedMarketDataError("Quote timestamp is in the future.")
    if now - quoted_at > policy.max_quote_age:
        raise StaleMarketDataError("Quote is stale.")


def normalize_closed_candles(
    candles: tuple[Candle, ...], *, now: datetime, policy: MarketDataPolicy
) -> tuple[Candle, ...]:
    timestamp(now)
    items(candles, Candle)
    # CoinGecko documents these timestamps as CLOSE times. Do not add another four hours.
    ordered = tuple(sorted(candles, key=lambda candle: candle.closed_at))
    for candle in ordered:
        at = candle.closed_at.astimezone(UTC)
        if at > now:
            raise MalformedMarketDataError("Future candle rejected.")
        if at.hour % 4 or at.minute or at.second or at.microsecond:
            raise MalformedMarketDataError("Candle is not on the UTC 4H grid.")
    if any(
        b.closed_at - a.closed_at != INTERVAL for a, b in zip(ordered, ordered[1:], strict=False)
    ):
        raise MalformedMarketDataError("Duplicate or missing 4H candle.")
    if ordered and now - ordered[-1].closed_at > policy.max_closed_candle_lag:
        raise StaleMarketDataError("Closed candles are stale.")
    return ordered


def normalize_closed_1h_candles(
    candles: tuple[Candle, ...], *, now: datetime, policy: MarketDataPolicy
) -> tuple[Candle, ...]:
    """Accept provider hourly OHLC or aggregate its documented 30-minute short window."""
    timestamp(now)
    items(candles, Candle)
    ordered = tuple(sorted(candles, key=lambda candle: candle.closed_at))
    if any(c.closed_at > now for c in ordered):
        raise MalformedMarketDataError("Future trigger candle rejected.")
    deltas = {b.closed_at - a.closed_at for a, b in zip(ordered, ordered[1:], strict=False)}
    if not ordered:
        return ()
    if deltas <= {INTERVAL}:
        # Provider/tier did not expose short-window intraday granularity. Keep it explicit.
        return ()
    if deltas <= {TRIGGER_INTERVAL} and all(
        c.closed_at.astimezone(UTC).minute == 0
        and c.closed_at.astimezone(UTC).second == 0
        and c.closed_at.astimezone(UTC).microsecond == 0
        for c in ordered
    ):
        hourly = ordered
    elif deltas <= {PROVIDER_INTRADAY_INTERVAL}:
        hourly = tuple(
            Candle(
                current.closed_at,
                previous.open,
                max(previous.high, current.high),
                min(previous.low, current.low),
                current.close,
            )
            for previous, current in zip(ordered, ordered[1:], strict=False)
            if current.closed_at.astimezone(UTC).minute == 0
            and current.closed_at - previous.closed_at == PROVIDER_INTRADAY_INTERVAL
        )
    else:
        raise MalformedMarketDataError("Unsupported or gapped trigger-candle cadence.")
    if any(
        b.closed_at - a.closed_at != TRIGGER_INTERVAL
        for a, b in zip(hourly, hourly[1:], strict=False)
    ):
        raise MalformedMarketDataError("Duplicate or missing 1H trigger candle.")
    if hourly and now - hourly[-1].closed_at > policy.max_closed_1h_candle_lag:
        raise StaleMarketDataError("Closed 1H trigger candles are stale.")
    return hourly


def validate_snapshot_quotes(
    market: MarketSnapshot, *, now: datetime, policy: MarketDataPolicy
) -> None:
    timestamp(now)
    if market.as_of > now:
        raise MalformedMarketDataError("Market snapshot is in the future.")
    for asset in market.assets:
        validate_quote_time(asset.quoted_at, now, policy)


def validate_cutoffs(market: MarketSnapshot, *, now: datetime, policy: MarketDataPolicy) -> None:
    cutoffs = set()
    for asset in market.assets:
        at = asset.latest_closed_candle_at
        if at is None:
            continue  # Explicitly PARTIAL; no invented cutoff.
        at = at.astimezone(UTC)
        if at > now or at.hour % 4 or at.minute or at.second or at.microsecond:
            raise MalformedMarketDataError("Invalid closed candle cutoff.")
        if now - at > policy.max_closed_candle_lag:
            raise StaleMarketDataError("Closed candle cutoff is stale.")
        cutoffs.add(at)
    if len(cutoffs) > 1:
        raise MarketCutoffMismatchError("Required assets have different closed 4H cutoffs.")
