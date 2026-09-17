from collections import Counter
from dataclasses import dataclass
from decimal import Decimal

from advanced_trading_advisor.domain.numeric import calculation_context
from advanced_trading_advisor.market.models import MarketSnapshot
from advanced_trading_advisor.portfolio.models import (
    ConfirmedPortfolioSnapshot,
    LimitOrderStatus,
    TradingSide,
)
from advanced_trading_advisor.risk.assessment import PortfolioRiskAssessment

from .engine import advice
from .policy import RecommendationPolicy
from .recommendations import ActionType, Recommendation


@dataclass(frozen=True, slots=True)
class ValidatedPlan:
    recommendations: tuple[Recommendation, ...]
    rejected: bool


class PlanValidator:
    """Fail closed as a WHOLE plan. Cancellations never release money or units here.

    Replacement is treated as a full additional order alongside the old OPEN order.
    Any invalid candidate discards ALL candidate narrative, not just its numeric terms.
    """

    version = 1

    def __init__(self, policy: RecommendationPolicy | None = None):
        self.policy = policy or RecommendationPolicy()

    def validate(
        self,
        candidates: tuple[Recommendation, ...],
        snapshot: ConfirmedPortfolioSnapshot,
        market: MarketSnapshot,
        risk: PortfolioRiskAssessment,
        *,
        allowed_buy_prices: dict[str, set[Decimal]] | None = None,
    ) -> ValidatedPlan:
        with calculation_context():
            safe = self._safe(candidates, snapshot, market, risk, allowed_buy_prices)
        if safe:
            return ValidatedPlan(candidates, False)
        return ValidatedPlan(
            (
                advice(
                    snapshot.id,
                    None,
                    ActionType.WAIT,
                    "No validated plan is available. Keep state unchanged and review inputs.",
                ),
            ),
            True,
        )

    def _safe(self, candidates, snapshot, market, risk, allowed_buy_prices=None):
        if not candidates or not all(isinstance(a, Recommendation) for a in candidates):
            return False
        if len({a.recommendation_id for a in candidates}) != len(candidates):
            return False
        refs = Counter(a.existing_order_id for a in candidates if a.existing_order_id is not None)
        if any(n > 1 for n in refs.values()):
            return False
        orders = {o.id: o for o in snapshot.orders if o.status is LimitOrderStatus.OPEN}
        assets = {a.symbol: a for a in market.assets}
        holdings = {p.symbol: p.amount for p in snapshot.portfolio.positions}
        sells = {s.symbol: s.reserved_quantity for s in risk.valuation.open_sell_reservations}
        projected = {a.symbol: a.market_value_usd for a in risk.valuation.assets}
        for o in orders.values():
            if o.side is TradingSide.BUY:
                projected[o.asset] = (
                    projected.get(o.asset, Decimal(0))
                    + o.quantity_asset * assets[o.asset].price_usd
                )
        buy_total = Decimal(0)
        equivalents = set()
        for a in candidates:
            if a.asset is not None and a.asset not in assets:
                return False
            if a.existing_order_id is not None:
                order = orders.get(a.existing_order_id)
                if (
                    order is None
                    or order.asset != a.asset
                    or (a.side is not None and a.side is not order.side)
                ):
                    return False
                if a.action_type is ActionType.REPLACE_LIMIT_ORDER and a.side is not order.side:
                    return False
            if a.action_type in {
                ActionType.HOLD,
                ActionType.WAIT,
                ActionType.REBALANCE,
                ActionType.REBALANCE_INFORMATIONAL,
                ActionType.KEEP_LIMIT_ORDER,
                ActionType.CANCEL_LIMIT_ORDER,
            }:
                continue
            # Market BUY is deliberately unavailable: no guaranteed spend cap/slippage bound.
            if a.action_type is ActionType.BUY:
                return False
            price = a.target_price if a.target_price is not None else assets[a.asset].price_usd
            equivalent = (a.asset, a.side, price, a.quantity_asset)
            if equivalent in equivalents:
                return False
            equivalents.add(equivalent)
            if a.side is TradingSide.BUY:
                target = self.policy.buy_target(assets[a.asset], risk.market_regime.regime)
                if (
                    not risk.new_buy_capacity_available
                    or risk.reserve.stable_reserve_deficit_usd > 0
                    or target is None
                    or a.target_price
                    not in (
                        allowed_buy_prices.get(a.asset, set())
                        if allowed_buy_prices is not None
                        else {target}
                    )
                ):
                    return False
                buy_total += price * a.quantity_asset
                projected[a.asset] = projected.get(a.asset, Decimal(0)) + a.quantity_asset * max(
                    price, assets[a.asset].price_usd
                )
                if (
                    projected[a.asset]
                    > risk.valuation.total_portfolio_usd * self.policy.max_asset_allocation
                ):
                    return False
            else:
                # Stable sell recommendations could undermine the expense floor; not supported.
                if a.asset in {"USDT", "USDC"}:
                    return False
                sells[a.asset] = sells.get(a.asset, Decimal(0)) + a.quantity_asset
                if sells[a.asset] > holdings.get(a.asset, Decimal(0)):
                    return False
                core = next((c for c in snapshot.core_positions if c.symbol == a.asset), None)
                if (
                    core is not None
                    and holdings.get(a.asset, Decimal(0)) - sells[a.asset] < core.hard_floor
                ):
                    return False
        return buy_total <= risk.reserve.deployable_stables_usd * self.policy.fraction(
            risk.market_regime.regime
        )


class ActivePlanValidator(PlanValidator):
    """Whole-plan V2: legacy arithmetic plus deterministic setup/price allowlisting."""

    version = 3

    def validate_active(self, candidates, snapshot, market, risk, active):
        from .active import SetupStatus

        setups = {
            (s.id, s.asset, s.direction, level.price): s
            for s in active.setups
            if s.status in {SetupStatus.ACTIVE, SetupStatus.CONFIRMED}
            and (s.trigger is None or s.trigger.execution_valid)
            for level in s.levels
        }
        allowed_buys = {}
        for _, asset, side, price in setups:
            if side is TradingSide.BUY:
                allowed_buys.setdefault(asset, set()).add(price)
        result = self.validate(candidates, snapshot, market, risk, allowed_buy_prices=allowed_buys)
        if result.rejected:
            return result
        open_orders = tuple(o for o in snapshot.orders if o.status is LimitOrderStatus.OPEN)
        new_buy_cost = Decimal(0)
        setup_costs = {}
        sell_quantities = {}
        directions = {}
        for action in candidates:
            if action.action_type is ActionType.CANCEL_LIMIT_ORDER and action.setup_id is not None:
                setup = next(
                    (
                        s
                        for s in active.setups
                        if s.id == action.setup_id
                        and s.status in {SetupStatus.INVALIDATED, SetupStatus.EXPIRED}
                        and action.existing_order_id in s.related_order_ids
                    ),
                    None,
                )
                if setup is None:
                    return self._rejected(snapshot)
            if action.action_type in {ActionType.BUY, ActionType.SELL}:
                return self._rejected(snapshot)
            if action.action_type in {ActionType.PLACE_LIMIT_ORDER, ActionType.REPLACE_LIMIT_ORDER}:
                if active.policy_version >= 2 and action.setup_id is None:
                    return self._rejected(snapshot)
                setup = next(
                    (
                        value
                        for (setup_id, asset, side, price), value in setups.items()
                        if (active.policy_version < 2 or setup_id == action.setup_id)
                        and asset == action.asset
                        and side == action.side
                        and price == action.target_price
                    ),
                    None,
                )
                if setup is None or setup.risk_reward <= 0:
                    return self._rejected(snapshot)
                directions.setdefault(action.asset, set()).add(action.side)
                setup_costs[setup.id] = setup_costs.get(setup.id, Decimal(0)) + (
                    action.target_price * action.quantity_asset
                )
                if setup_costs[setup.id] > setup.maximum_capital_usd:
                    return self._rejected(snapshot)
                if action.side is TradingSide.BUY:
                    new_buy_cost += action.target_price * action.quantity_asset
                else:
                    sell_quantities[action.asset] = sell_quantities.get(action.asset, Decimal(0))
                    sell_quantities[action.asset] += action.quantity_asset
                if action.action_type is ActionType.PLACE_LIMIT_ORDER and any(
                    o.asset == action.asset and o.side is action.side for o in open_orders
                ):
                    return self._rejected(snapshot)
        if new_buy_cost > active.recommended_deployment_usd or any(
            len(sides) > 1 for sides in directions.values()
        ):
            return self._rejected(snapshot)
        tradable = {d.asset: d.tradable_quantity for d in active.asset_decisions}
        if any(qty > tradable.get(asset, Decimal(0)) for asset, qty in sell_quantities.items()):
            return self._rejected(snapshot)
        return result

    @staticmethod
    def _rejected(snapshot):
        return ValidatedPlan(
            (
                advice(
                    snapshot.id,
                    None,
                    ActionType.WAIT,
                    "No validated plan is available. Keep state unchanged and review inputs.",
                ),
            ),
            True,
        )
