"""Explicit allowlist, never raw candles, state history or provider/configuration objects."""

import json
from dataclasses import fields
from datetime import datetime
from decimal import Decimal

from advanced_trading_advisor.analysis.recommendations import canonical_action_terms
from advanced_trading_advisor.domain.numeric import calculation_context
from advanced_trading_advisor.portfolio.models import LimitOrderStatus, TradingSide
from advanced_trading_advisor.risk.assessment import RiskCondition

CONTEXT_VERSION = 4


def build_context(snapshot, market, risk, policy, active=None, baseline_actions=()) -> str:
    with calculation_context():
        v, r = risk.valuation, risk.reserve
        orders = sorted(
            (o for o in snapshot.orders if o.status is LimitOrderStatus.OPEN),
            key=lambda o: str(o.id),
        )
        valuations = {a.symbol: a for a in v.assets}
        cores = {c.symbol: c for c in snapshot.core_positions}
        sells = {s.symbol: s.reserved_quantity for s in v.open_sell_reservations}
        budget = (
            r.deployable_stables_usd * policy.fraction(risk.market_regime.regime)
            if risk.new_buy_capacity_available
            else Decimal(0)
        )
        assets = []
        for a in sorted(market.assets, key=lambda a: a.symbol):
            holding = valuations.get(a.symbol)
            exposure = (holding.market_value_usd if holding else Decimal(0)) + sum(
                (
                    o.quantity_asset * a.price_usd
                    for o in orders
                    if o.asset == a.symbol and o.side is TradingSide.BUY
                ),
                Decimal(0),
            )
            room = max(v.total_portfolio_usd * policy.max_asset_allocation - exposure, Decimal(0))
            target = policy.buy_target(a, risk.market_regime.regime)
            maximum = (
                policy.quantity(min(budget, room / a.price_usd * target), target)
                if target is not None
                else Decimal(0)
            )
            assets.append(
                {
                    "symbol": a.symbol,
                    "price_usd": a.price_usd,
                    "quote_timestamp": a.quoted_at.isoformat(),
                    "closed_price_usd": a.latest_closed_price_usd,
                    "holdings": holding.quantity if holding else Decimal(0),
                    "allocation_pct": holding.allocation_pct if holding else Decimal(0),
                    "features": {
                        f.name: getattr(a.technical_features, f.name)
                        for f in fields(a.technical_features)
                    },
                    "completeness": a.completeness,
                    "daily_context": {
                        f.name: getattr(a.daily_context, f.name) for f in fields(a.daily_context)
                    },
                    "core_hard_floor": cores[a.symbol].hard_floor if a.symbol in cores else None,
                    "core_preferred_quantity": (
                        cores[a.symbol].preferred_quantity if a.symbol in cores else None
                    ),
                    "eligible_buy_limit_usd": target,
                    "max_candidate_buy_quantity": maximum,
                    "max_candidate_sell_quantity": max(
                        (holding.quantity if holding else Decimal(0))
                        - sells.get(a.symbol, Decimal(0))
                        - (cores[a.symbol].preferred_quantity if a.symbol in cores else Decimal(0)),
                        Decimal(0),
                    )
                    if a.symbol not in {"USDT", "USDC"}
                    else Decimal(0),
                }
            )
        eligible_setups = (
            tuple(
                s
                for s in active.setups
                if s.status.value in {"active", "confirmed"}
                and (s.trigger is None or s.trigger.execution_valid)
                and s.risk_reward > 0
                and s.levels
            )
            if active is not None
            else ()
        )
        data = {
            "context_version": CONTEXT_VERSION,
            "snapshot_id": str(snapshot.id),
            "portfolio": {
                "total_usd": v.total_portfolio_usd,
                "stables_usd": v.current_stables_usd,
                "stable_allocation_pct": v.stable_allocation_pct,
            },
            "financial": {
                "monthly_expenses_usd": r.monthly_expenses_usd,
                "target_runway_months": r.target_expense_runway_months,
                "actual_runway_months": r.actual_expense_runway_months,
                "minimum_stable_reserve_usd": r.recommended_minimum_stables_usd,
                "hard_expense_reserve_usd": r.hard_reserve_usd,
                "market_buffer_usd": r.market_buffer_usd,
                "potential_trading_liquidity_usd": r.potential_trading_liquidity_usd,
                "deployable_stables_usd": r.deployable_stables_usd,
                "reserve_deficit_usd": r.stable_reserve_deficit_usd,
                "open_buy_commitments_usd": v.open_buy_commitments_usd,
                "stable_sell_reservations_usd": v.open_sell_stables_reserved_usd,
            },
            "market": {
                "cutoff": market.effective_market_cutoff.isoformat()
                if market.effective_market_cutoff
                else None,
                "regime": risk.market_regime.regime,
                "regime_policy_version": risk.market_regime.policy_version,
                "is_live": market.is_live,
            },
            "assets": assets,
            "open_orders": [
                {
                    "id": str(o.id),
                    "asset": o.asset,
                    "side": o.side,
                    "target_price": o.target_price,
                    "quantity_asset": o.quantity_asset,
                    "buy_commitment_usd": o.target_price * o.quantity_asset
                    if o.side is TradingSide.BUY
                    else Decimal(0),
                    "sell_reserved_quantity": o.quantity_asset
                    if o.side is TradingSide.SELL
                    else Decimal(0),
                }
                for o in orders
            ],
            "risk": {
                "warnings": list(risk.warnings),
                "buying_blocked": not risk.new_buy_capacity_available,
                "concentration_warning": RiskCondition.CONCENTRATION in risk.warnings,
            },
            "policy": {
                "version": policy.version,
                "max_candidate_buy_budget_usd": budget,
                "max_asset_allocation_fraction": policy.max_asset_allocation,
                "quantity_quantum": policy.quantity_quantum,
                "minimum_order_usd": policy.minimum_order_usd,
                "capacity_fields_are_constraints_not_authorization": True,
            },
            "recommended_deployment_usd": (
                active.recommended_deployment_usd if active is not None else Decimal(0)
            ),
            "execution_order_actions_allowed": bool(eligible_setups),
            "deterministic_setup_levels": [
                {
                    "setup_id": str(s.id),
                    "asset": s.asset,
                    "setup_type": s.setup_type,
                    "direction": s.direction,
                    "trigger_state": s.status,
                    "zone_low": s.trigger.zone_low if s.trigger else None,
                    "zone_high": s.trigger.zone_high if s.trigger else None,
                    "touched_at": s.trigger.touched_at if s.trigger else None,
                    "confirmation_at": s.trigger.confirmation_at if s.trigger else None,
                    "execution_valid": s.trigger.execution_valid if s.trigger else None,
                    "transition_reason": s.trigger.transitions[-1].reason
                    if s.trigger and s.trigger.transitions
                    else None,
                    "limit_price": s.limit_price,
                    "price_source": s.price_source,
                    "levels": [
                        {"price": level.price, "source": level.source, "priority": level.priority}
                        for level in s.levels
                    ],
                    "invalidation_price": s.invalidation_price,
                    "target_price": s.target_price,
                    "risk_reward": s.risk_reward,
                    "score": s.score.total,
                }
                for s in eligible_setups
            ],
            "allowed_action_terms": [
                terms.as_dict() for terms in canonical_action_terms(baseline_actions)
            ],
        }

    def encode(value):
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, datetime):
            return value.isoformat()
        raise TypeError("Unexpected context value.")

    return json.dumps(data, default=encode, sort_keys=True, separators=(",", ":"), allow_nan=False)
