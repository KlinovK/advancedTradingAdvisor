"""Persistable evidence envelope; no new table or analysis workflow in this phase."""

from dataclasses import fields

from advanced_trading_advisor.domain.numeric import parse_decimal
from advanced_trading_advisor.market.models import (
    Candle,
    DailyContext,
    DailyTrend,
    DataCompleteness,
    MarketAssetData,
    MarketSnapshot,
    TechnicalFeatures,
)

from .codecs import array, boundary, date, dump, load, strict_object, utc_text


class MarketSnapshotCodec:
    @staticmethod
    @boundary
    def encode(market: MarketSnapshot) -> str:
        return dump(
            {
                "schema_version": 3
                if any(a.closed_1h_candles for a in market.assets)
                else 2
                if any(a.daily_context.latest_closed_at for a in market.assets)
                else 1,
                "kind": "market_snapshot",
                "data": {
                    "as_of": utc_text(market.as_of),
                    "is_live": market.is_live,
                    "effective_market_cutoff": utc_text(market.effective_market_cutoff)
                    if market.effective_market_cutoff is not None
                    else None,
                    "completeness": market.completeness.value,
                    "assets": [
                        {
                            "symbol": a.symbol,
                            "price_usd": format(a.price_usd, "f"),
                            "quoted_at": utc_text(a.quoted_at),
                            "latest_closed_candle_at": utc_text(a.latest_closed_candle_at)
                            if a.latest_closed_candle_at is not None
                            else None,
                            "latest_closed_price_usd": format(a.latest_closed_price_usd, "f")
                            if a.latest_closed_price_usd is not None
                            else None,
                            "completeness": a.completeness.value,
                            "technical_features": {
                                f.name: format(getattr(a.technical_features, f.name), "f")
                                if getattr(a.technical_features, f.name) is not None
                                else None
                                for f in fields(TechnicalFeatures)
                            },
                            **(
                                {
                                    "daily_context": {
                                        "latest_closed_at": utc_text(
                                            a.daily_context.latest_closed_at
                                        )
                                        if a.daily_context.latest_closed_at
                                        else None,
                                        "closed_price_usd": format(
                                            a.daily_context.closed_price_usd, "f"
                                        )
                                        if a.daily_context.closed_price_usd is not None
                                        else None,
                                        "ema20": format(a.daily_context.ema20, "f")
                                        if a.daily_context.ema20 is not None
                                        else None,
                                        "ema50": format(a.daily_context.ema50, "f")
                                        if a.daily_context.ema50 is not None
                                        else None,
                                        "support": format(a.daily_context.support, "f")
                                        if a.daily_context.support is not None
                                        else None,
                                        "resistance": format(a.daily_context.resistance, "f")
                                        if a.daily_context.resistance is not None
                                        else None,
                                        "trend": a.daily_context.trend.value,
                                    }
                                }
                                if any(x.daily_context.latest_closed_at for x in market.assets)
                                or any(x.closed_1h_candles for x in market.assets)
                                else {}
                            ),
                            **(
                                {
                                    "closed_1h_candles": [
                                        {
                                            "closed_at": utc_text(c.closed_at),
                                            "open": format(c.open, "f"),
                                            "high": format(c.high, "f"),
                                            "low": format(c.low, "f"),
                                            "close": format(c.close, "f"),
                                        }
                                        for c in a.closed_1h_candles
                                    ]
                                }
                                if any(x.closed_1h_candles for x in market.assets)
                                else {}
                            ),
                        }
                        for a in market.assets
                    ],
                },
            },
        )

    @staticmethod
    @boundary
    def decode(text: str) -> MarketSnapshot:
        root = strict_object(load(text), {"schema_version", "kind", "data"})
        if (
            root["kind"] != "market_snapshot"
            or type(root["schema_version"]) is not int
            or root["schema_version"] not in {1, 2, 3}
        ):
            raise ValueError("Unsupported market payload.")
        d = strict_object(
            root["data"],
            {"as_of", "is_live", "effective_market_cutoff", "completeness", "assets"},
        )
        assets = []
        for raw in array(d["assets"]):
            a = strict_object(
                raw,
                {
                    "symbol",
                    "price_usd",
                    "quoted_at",
                    "latest_closed_candle_at",
                    "latest_closed_price_usd",
                    "completeness",
                    "technical_features",
                }
                | ({"daily_context"} if root["schema_version"] >= 2 else set())
                | ({"closed_1h_candles"} if root["schema_version"] == 3 else set()),
            )
            features = strict_object(
                a["technical_features"], {f.name for f in fields(TechnicalFeatures)}
            )
            daily = (
                strict_object(a["daily_context"], {f.name for f in fields(DailyContext)})
                if root["schema_version"] >= 2
                else None
            )
            assets.append(
                MarketAssetData(
                    a["symbol"],
                    parse_decimal(a["price_usd"], positive=True),
                    date(a["quoted_at"]),
                    date(a["latest_closed_candle_at"])
                    if a["latest_closed_candle_at"] is not None
                    else None,
                    TechnicalFeatures(
                        **{
                            k: parse_decimal(v) if v is not None else None
                            for k, v in features.items()
                        }
                    ),
                    DataCompleteness(a["completeness"]),
                    parse_decimal(a["latest_closed_price_usd"], positive=True)
                    if a["latest_closed_price_usd"] is not None
                    else None,
                    DailyContext(
                        date(daily["latest_closed_at"]) if daily["latest_closed_at"] else None,
                        *(
                            parse_decimal(daily[k], positive=True) if daily[k] is not None else None
                            for k in ("closed_price_usd", "ema20", "ema50", "support", "resistance")
                        ),
                        DailyTrend(daily["trend"]),
                    )
                    if root["schema_version"] >= 2
                    else DailyContext(),
                    tuple(
                        Candle(
                            date(c["closed_at"]),
                            parse_decimal(c["open"], positive=True),
                            parse_decimal(c["high"], positive=True),
                            parse_decimal(c["low"], positive=True),
                            parse_decimal(c["close"], positive=True),
                        )
                        for item in array(a.get("closed_1h_candles", []))
                        for c in (
                            strict_object(item, {"closed_at", "open", "high", "low", "close"}),
                        )
                    ),
                )
            )
        result = MarketSnapshot(date(d["as_of"]), tuple(assets), d["is_live"])
        cutoff = (
            date(d["effective_market_cutoff"]) if d["effective_market_cutoff"] is not None else None
        )
        if (
            cutoff != result.effective_market_cutoff
            or d["completeness"] != result.completeness.value
        ):
            raise ValueError("Market metadata disagrees with asset evidence.")
        return result
