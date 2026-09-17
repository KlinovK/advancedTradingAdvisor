from dataclasses import dataclass, fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from advanced_trading_advisor.domain.numeric import finite_decimal
from advanced_trading_advisor.domain.validation import items, require, symbol, timestamp


@dataclass(frozen=True, slots=True)
class Candle:
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self):
        timestamp(self.closed_at)
        for value in (self.open, self.high, self.low, self.close):
            finite_decimal(value, positive=True)
        require(
            self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high,
            "Inconsistent OHLC range.",
        )


@dataclass(frozen=True, slots=True)
class TechnicalFeatures:
    change_24h_pct: Decimal | None = None
    change_7d_pct: Decimal | None = None
    rsi_4h: Decimal | None = None
    ema20_4h: Decimal | None = None
    ema50_4h: Decimal | None = None
    atr_4h: Decimal | None = None
    support: Decimal | None = None
    resistance: Decimal | None = None

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if value is not None:
                finite_decimal(value)
        if self.rsi_4h is not None:
            require(0 <= self.rsi_4h <= 100, "RSI must be 0–100.")
        for value in (self.ema20_4h, self.ema50_4h, self.support, self.resistance):
            if value is not None:
                finite_decimal(value, positive=True)
        if self.atr_4h is not None:
            require(self.atr_4h >= 0, "ATR must be nonnegative.")
        if self.support is not None and self.resistance is not None:
            require(self.support <= self.resistance, "Support exceeds resistance.")


class DataCompleteness(StrEnum):
    UNAVAILABLE = "unavailable"
    PRICE_ONLY = "price_only"
    PARTIAL = "partial"
    COMPLETE = "complete"


class DailyTrend(StrEnum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    MIXED = "mixed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class DailyContext:
    """Higher-timeframe evidence derived only from six consecutive closed 4H candles/day."""

    latest_closed_at: datetime | None = None
    closed_price_usd: Decimal | None = None
    ema20: Decimal | None = None
    ema50: Decimal | None = None
    support: Decimal | None = None
    resistance: Decimal | None = None
    trend: DailyTrend = DailyTrend.INCOMPLETE

    def __post_init__(self):
        if self.latest_closed_at is not None:
            timestamp(self.latest_closed_at)
            require(
                self.latest_closed_at.utcoffset() is not None
                and self.latest_closed_at.utcoffset().total_seconds() == 0
                and self.latest_closed_at.hour == 0
                and self.latest_closed_at.minute == 0
                and self.latest_closed_at.second == 0,
                "Daily cutoff must be UTC midnight.",
            )
        for value in (self.closed_price_usd, self.ema20, self.ema50, self.support, self.resistance):
            if value is not None:
                finite_decimal(value, positive=True)
        require(isinstance(self.trend, DailyTrend), "Invalid daily trend.")
        if self.trend is not DailyTrend.INCOMPLETE:
            require(
                self.closed_price_usd is not None and self.ema20 is not None,
                "Daily trend requires closed-price and EMA evidence.",
            )


@dataclass(frozen=True, slots=True)
class MarketAssetData:
    symbol: str
    price_usd: Decimal
    quoted_at: datetime
    latest_closed_candle_at: datetime | None = None
    technical_features: TechnicalFeatures = TechnicalFeatures()
    completeness: DataCompleteness = DataCompleteness.PRICE_ONLY
    latest_closed_price_usd: Decimal | None = None
    daily_context: DailyContext = DailyContext()
    closed_1h_candles: tuple[Candle, ...] = ()

    def __post_init__(self):
        symbol(self.symbol)
        finite_decimal(self.price_usd, positive=True)
        timestamp(self.quoted_at)
        require(
            isinstance(self.technical_features, TechnicalFeatures), "Invalid technical features."
        )
        require(isinstance(self.completeness, DataCompleteness), "Invalid completeness.")
        require(
            self.completeness is not DataCompleteness.UNAVAILABLE,
            "Unavailable assets must not fabricate a price; fail the request instead.",
        )
        require(isinstance(self.daily_context, DailyContext), "Invalid daily context.")
        items(self.closed_1h_candles, Candle)
        require(
            all(
                b.closed_at - a.closed_at == timedelta(hours=1)
                for a, b in zip(self.closed_1h_candles, self.closed_1h_candles[1:], strict=False)
            ),
            "Trigger candles must be ordered exactly one hour apart.",
        )
        require(
            all(
                c.closed_at.astimezone(UTC).minute == 0
                and c.closed_at.astimezone(UTC).second == 0
                and c.closed_at.astimezone(UTC).microsecond == 0
                and c.closed_at <= self.quoted_at
                for c in self.closed_1h_candles
            ),
            "Trigger candles must be closed on the UTC 1H grid.",
        )
        if self.daily_context.latest_closed_at is not None:
            require(
                self.latest_closed_candle_at is not None
                and self.daily_context.latest_closed_at <= self.latest_closed_candle_at,
                "Daily context follows tactical cutoff.",
            )
        if self.latest_closed_candle_at is not None:
            timestamp(self.latest_closed_candle_at)
        if self.latest_closed_price_usd is not None:
            finite_decimal(self.latest_closed_price_usd, positive=True)
            require(self.latest_closed_candle_at is not None, "Closed price requires a cutoff.")
        present = [
            getattr(self.technical_features, f.name) is not None
            for f in fields(self.technical_features)
        ]
        if self.completeness is DataCompleteness.PRICE_ONLY:
            require(not any(present), "Price-only data cannot contain technical features.")
        else:
            require(self.latest_closed_candle_at is not None, "Features require a candle cutoff.")
            require(any(present), "Feature data cannot be empty.")
            if self.completeness is DataCompleteness.COMPLETE:
                require(all(present), "Complete data must contain all features.")


@dataclass(frozen=True, slots=True)
class MarketSnapshot:
    as_of: datetime
    assets: tuple[MarketAssetData, ...]
    is_live: bool = False

    def __post_init__(self):
        timestamp(self.as_of)
        items(self.assets, MarketAssetData)
        require(type(self.is_live) is bool, "Invalid source flag.")
        require(
            len({a.symbol for a in self.assets}) == len(self.assets), "Duplicate market symbols."
        )

    @property
    def effective_market_cutoff(self) -> datetime | None:
        """Only claim a common analyzed candle when every requested asset has that cutoff."""
        cutoffs = {a.latest_closed_candle_at for a in self.assets}
        return next(iter(cutoffs)) if len(cutoffs) == 1 and None not in cutoffs else None

    @property
    def completeness(self) -> DataCompleteness:
        if not self.assets:
            return DataCompleteness.UNAVAILABLE
        if self.effective_market_cutoff is not None and all(
            a.completeness is DataCompleteness.COMPLETE for a in self.assets
        ):
            return DataCompleteness.COMPLETE
        return DataCompleteness.PARTIAL
