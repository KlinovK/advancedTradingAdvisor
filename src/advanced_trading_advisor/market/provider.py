from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from advanced_trading_advisor.domain.validation import require

from .models import MarketSnapshot


class MarketDataProvider(Protocol):
    async def get_market_snapshot(
        self,
        symbols: tuple[str, ...],
        *,
        authoritative_cutoff: datetime | None = None,
    ) -> MarketSnapshot: ...


@dataclass(frozen=True, slots=True)
class MarketDataPolicy:
    max_quote_age: timedelta = timedelta(minutes=10)
    max_closed_candle_lag: timedelta = timedelta(hours=5)
    max_closed_1h_candle_lag: timedelta = timedelta(hours=2)

    def __post_init__(self):
        for value in (
            self.max_quote_age,
            self.max_closed_candle_lag,
            self.max_closed_1h_candle_lag,
        ):
            require(
                isinstance(value, timedelta) and value > timedelta(0), "Invalid freshness limit."
            )
