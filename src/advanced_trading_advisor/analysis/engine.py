from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from advanced_trading_advisor.domain.numeric import calculation_context
from advanced_trading_advisor.market.models import DataCompleteness, MarketSnapshot
from advanced_trading_advisor.portfolio.models import (
    ConfirmedPortfolioSnapshot,
    LimitOrderStatus,
    TradingSide,
)
from advanced_trading_advisor.risk.assessment import PortfolioRiskAssessment

from .policy import RecommendationPolicy
from .recommendations import ActionType, Recommendation

ZERO = Decimal("0")


def advice(
    snapshot_id,
    asset,
    action,
    reason,
    *,
    order_id=None,
    side=None,
    price=None,
    quantity=None,
    priority=2,
    setup_id=None,
):
    key = f"{snapshot_id}:{asset}:{action}:{order_id}:{side}:{price}:{quantity}:{setup_id}"
    return Recommendation(
        uuid5(NAMESPACE_URL, key),
        asset,
        action,
        priority,
        reason,
        order_id,
        side,
        price,
        quantity,
        setup_id=setup_id,
    )


class RecommendationEngine:
    def __init__(self, policy: RecommendationPolicy | None = None):
        self.policy = policy or RecommendationPolicy()

    def generate(
        self,
        snapshot: ConfirmedPortfolioSnapshot,
        market: MarketSnapshot,
        risk: PortfolioRiskAssessment,
    ) -> tuple[Recommendation, ...]:
        p = self.policy
        by_symbol = {a.symbol: a for a in market.assets}
        regime = risk.market_regime.regime
        can_buy = (
            risk.new_buy_capacity_available
            and risk.reserve.stable_reserve_deficit_usd == 0
            and risk.data_completeness is DataCompleteness.COMPLETE
            and risk.reserve.deployable_stables_usd > 0
        )
        with calculation_context():
            budget = risk.reserve.deployable_stables_usd * p.fraction(regime)
            values = {a.symbol: a.market_value_usd for a in risk.valuation.assets}
            existing_buys = {}
            for o in snapshot.orders:
                if o.status is LimitOrderStatus.OPEN and o.side is TradingSide.BUY:
                    existing_buys[o.asset] = (
                        existing_buys.get(o.asset, ZERO)
                        + o.quantity_asset * by_symbol[o.asset].price_usd
                    )
            result = []
            sell_overcommitted = {
                s.symbol for s in risk.valuation.open_sell_reservations if s.exceeds_holdings
            }
            for o in sorted(snapshot.orders, key=lambda x: str(x.id)):
                if o.status is not LimitOrderStatus.OPEN:
                    continue
                asset = by_symbol[o.asset]
                action, reason = (
                    ActionType.KEEP_LIMIT_ORDER,
                    "Existing confirmed order remains within policy; no execution inferred.",
                )
                if o.side is TradingSide.SELL:
                    if o.asset in sell_overcommitted:
                        action, reason = (
                            ActionType.CANCEL_LIMIT_ORDER,
                            "Open SELL reservations exceed confirmed inventory. "
                            "Confirm cancellation manually.",
                        )
                    elif o.target_price <= asset.price_usd:
                        action, reason = (
                            ActionType.CANCEL_LIMIT_ORDER,
                            "SELL target is marketable. Review actual status manually; "
                            "no fill or proceeds are inferred.",
                        )
                    result.append(
                        advice(snapshot.id, o.asset, action, reason, order_id=o.id, side=o.side)
                    )
                    continue
                target = p.buy_target(asset, regime)
                allocation = values.get(o.asset, ZERO) + existing_buys.get(o.asset, ZERO)
                if (
                    target is None
                    or risk.reserve.stable_reserve_deficit_usd > 0
                    or risk.data_completeness is not DataCompleteness.COMPLETE
                    or allocation > risk.valuation.total_portfolio_usd * p.max_asset_allocation
                    or o.target_price >= asset.price_usd
                ):
                    action, reason = (
                        ActionType.CANCEL_LIMIT_ORDER,
                        "BUY terms fail reserve, allocation or technical policy. "
                        "Confirm cancellation manually.",
                    )
                elif (
                    abs(o.target_price - target)
                    > p.replacement_atr_distance * asset.technical_features.atr_4h
                ):
                    room = max(
                        risk.valuation.total_portfolio_usd * p.max_asset_allocation - allocation,
                        ZERO,
                    )
                    quantity = min(
                        o.quantity_asset,
                        p.quantity(min(budget, room * target / asset.price_usd), target),
                    )
                    cost = quantity * target
                    if can_buy and cost >= p.minimum_order_usd:
                        result.append(
                            advice(
                                snapshot.id,
                                o.asset,
                                ActionType.REPLACE_LIMIT_ORDER,
                                "Deeper entry with full additional capital reserved; "
                                "old order still counted until cancellation is confirmed.",
                                order_id=o.id,
                                side=o.side,
                                price=target,
                                quantity=quantity,
                            )
                        )
                        budget -= cost
                        existing_buys[o.asset] = (
                            existing_buys.get(o.asset, ZERO) + quantity * asset.price_usd
                        )
                        continue
                    action, reason = (
                        ActionType.CANCEL_LIMIT_ORDER,
                        "Outdated BUY target; insufficient capacity for safe replacement.",
                    )
                result.append(
                    advice(snapshot.id, o.asset, action, reason, order_id=o.id, side=o.side)
                )
            if risk.reserve.stable_reserve_deficit_usd > 0 or any(
                a.allocation_pct > p.rebalance_allocation * 100 for a in risk.valuation.assets
            ):
                result.append(
                    advice(
                        snapshot.id,
                        None,
                        ActionType.REBALANCE,
                        "Informational only: review reserve deficit or concentration. "
                        "Use unreserved holdings. No executable legs or future proceeds assumed.",
                    )
                )
            open_assets = {o.asset for o in snapshot.orders if o.status is LimitOrderStatus.OPEN}
            for asset in sorted(market.assets, key=lambda a: a.symbol):
                if asset.symbol in {"USDT", "USDC"} or asset.symbol in open_assets:
                    continue
                target = p.buy_target(asset, regime)
                quantity = ZERO
                if target is not None and can_buy:
                    room = max(
                        risk.valuation.total_portfolio_usd * p.max_asset_allocation
                        - values.get(asset.symbol, ZERO)
                        - existing_buys.get(asset.symbol, ZERO),
                        ZERO,
                    )
                    quantity = p.quantity(min(budget, room * target / asset.price_usd), target)
                if quantity > 0 and quantity * target >= p.minimum_order_usd:
                    result.append(
                        advice(
                            snapshot.id,
                            asset.symbol,
                            ActionType.PLACE_LIMIT_ORDER,
                            "Pullback entry within uncommitted capital and allocation bounds. "
                            "Manual decision only.",
                            side=TradingSide.BUY,
                            price=target,
                            quantity=quantity,
                        )
                    )
                    budget -= quantity * target
                else:
                    result.append(
                        advice(
                            snapshot.id,
                            asset.symbol,
                            ActionType.HOLD,
                            "No defensible funded setup; retain state and reassess later.",
                        )
                    )
            if not result:
                result.append(
                    advice(snapshot.id, None, ActionType.WAIT, "No defensible action available.")
                )
            return tuple(result)
