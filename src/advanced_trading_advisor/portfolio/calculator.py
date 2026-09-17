from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from advanced_trading_advisor.domain.numeric import calculation_context
from advanced_trading_advisor.market.errors import MarketDataError
from advanced_trading_advisor.market.models import MarketSnapshot
from advanced_trading_advisor.market.provider import MarketDataPolicy
from advanced_trading_advisor.market.validation import validate_snapshot_quotes

from .models import (
    ConfirmedPortfolioSnapshot,
    LimitOrderStatus,
    StablecoinClassification,
    TradingSide,
)

ZERO = Decimal("0")


def market_symbols(snapshot: ConfirmedPortfolioSnapshot) -> tuple[str, ...]:
    """Exact union, including assets only held in open orders; no implicit BTC fetch."""
    return tuple(
        sorted(
            {p.symbol for p in snapshot.portfolio.positions}
            | {o.asset for o in snapshot.orders if o.status is LimitOrderStatus.OPEN}
        )
    )


@dataclass(frozen=True, slots=True)
class AssetValuation:
    symbol: str
    quantity: Decimal
    price_usd: Decimal
    market_value_usd: Decimal
    allocation_pct: Decimal


@dataclass(frozen=True, slots=True)
class SellReservation:
    symbol: str
    reserved_quantity: Decimal
    held_quantity: Decimal
    market_notional_usd: Decimal
    limit_notional_usd: Decimal

    @property
    def exceeds_holdings(self) -> bool:
        return self.reserved_quantity > self.held_quantity


@dataclass(frozen=True, slots=True)
class PortfolioValuation:
    total_portfolio_usd: Decimal
    current_stables_usd: Decimal
    non_stable_crypto_usd: Decimal
    stable_allocation_pct: Decimal
    assets: tuple[AssetValuation, ...]
    open_buy_commitments_usd: Decimal
    open_sell_reservations: tuple[SellReservation, ...]
    open_sell_stables_reserved_usd: Decimal


class PortfolioCalculator:
    def __init__(
        self,
        *,
        stables: StablecoinClassification | None = None,
        data_policy: MarketDataPolicy | None = None,
    ):
        self.stables = stables or StablecoinClassification()
        self.data_policy = data_policy or MarketDataPolicy()

    def calculate(
        self, snapshot: ConfirmedPortfolioSnapshot, market: MarketSnapshot, *, now: datetime
    ) -> PortfolioValuation:
        validate_snapshot_quotes(market, now=now, policy=self.data_policy)
        prices = {a.symbol: a.price_usd for a in market.assets}
        if not set(market_symbols(snapshot)) <= prices.keys():
            raise MarketDataError("Required portfolio/open-order quotes are missing.")
        holdings = {p.symbol: p.amount for p in snapshot.portfolio.positions}
        with calculation_context():
            values = {s: quantity * prices[s] for s, quantity in holdings.items()}
            total = sum(values.values(), ZERO)
            stables = sum((v for s, v in values.items() if self.stables.is_stable(s)), ZERO)
            assets = tuple(
                AssetValuation(
                    s, holdings[s], prices[s], value, value / total * 100 if total else ZERO
                )
                for s, value in sorted(values.items())
            )
            buy = ZERO
            sell_quantities, sell_limits = {}, {}
            for order in snapshot.orders:
                if order.status is not LimitOrderStatus.OPEN:
                    continue
                # Phase 1 order target_price is USD per asset; commitment is not today's quote.
                if order.side is TradingSide.BUY:
                    buy += order.target_price * order.quantity_asset
                else:
                    sell_quantities[order.asset] = (
                        sell_quantities.get(order.asset, ZERO) + order.quantity_asset
                    )
                    sell_limits[order.asset] = (
                        sell_limits.get(order.asset, ZERO)
                        + order.target_price * order.quantity_asset
                    )
            sells = tuple(
                SellReservation(s, q, holdings.get(s, ZERO), q * prices[s], sell_limits[s])
                for s, q in sorted(sell_quantities.items())
            )
            return PortfolioValuation(
                total,
                stables,
                total - stables,
                stables / total * 100 if total else ZERO,
                assets,
                buy,
                sells,
                sum(
                    (s.market_notional_usd for s in sells if self.stables.is_stable(s.symbol)), ZERO
                ),
            )
