import hashlib
from decimal import Decimal

from advanced_trading_advisor.analysis.result import AnalysisResult
from advanced_trading_advisor.domain.numeric import calculation_context
from advanced_trading_advisor.portfolio.manual_trade import (
    SETTLEMENT_ASSETS,
    ManualSpotTradeProposal,
)
from advanced_trading_advisor.portfolio.models import TradingSide


def _setup_line(setup):
    s = setup
    breakdown = ", ".join(
        f"{name}:{points}/{maximum}" for name, points, maximum in s.score.components
    )
    ladder = ", ".join(f"L{level.priority}=${level.price} [{level.source}]" for level in s.levels)
    transition_reason = (
        s.trigger.transitions[-1].reason.value
        if s.trigger is not None and s.trigger.transitions
        else "none"
    )
    trigger = (
        f" zone=${s.trigger.zone_low}–${s.trigger.zone_high} "
        f"touched={s.trigger.touched_at or 'no'} confirmed={s.trigger.confirmation_at or 'no'} "
        f"execution_valid={'yes' if s.trigger.execution_valid else 'no'} "
        f"last_transition={transition_reason}"
        if s.trigger is not None
        else " trigger=unavailable (Phase 9 legacy)"
    )
    return (
        f"{s.asset} {s.setup_type.value.upper()} {s.status.value.upper()} "
        f"score={s.score.total}/100 ({breakdown}) "
        f"limit=${s.limit_price} [{s.price_source}] invalidation=${s.invalidation_price} "
        f"[{s.invalidation_source}] target=${s.target_price} [{s.target_source}] "
        f"R/R={s.risk_reward:.2f} max=${s.maximum_capital_usd:,.2f} "
        f"levels={ladder} setup={s.id}{trigger}"
    )


def render_setups(active, setup_id=None):
    if active is None:
        return "No persisted swing setup state (legacy analysis)."
    setups = tuple(s for s in active.setups if setup_id is None or s.id == setup_id)
    if not setups:
        return "No matching persisted swing setup."
    ranked = {setup: index + 1 for index, setup in enumerate(active.ranked_setup_ids)}
    return "\n".join(f"Rank {ranked.get(s.id, '—')}: {_setup_line(s)}" for s in setups)


def render_analysis(result: AnalysisResult) -> str:
    f = result.financial
    configuration = result.configuration
    lines = [
        "ADVANCED TRADING ADVISOR",
        *(
            [
                f"Reserve policy: v{configuration.stable_reserve_version}; "
                f"whole-plan: v{configuration.whole_plan_version}",
                f"AI contracts (context/schema/prompt): {configuration.ai_context_version}/"
                f"{configuration.ai_schema_version}/{configuration.ai_prompt_version}; "
                f"effort: {configuration.reasoning_effort}",
                "Decision configuration SHA256: "
                f"{hashlib.sha256(configuration.fingerprint_material().encode()).hexdigest()}",
                *(
                    [f"AI fallback category: {configuration.ai_failure_category}"]
                    if configuration.ai_failure_category
                    else []
                ),
            ]
            if configuration
            else ["Decision configuration: unavailable (legacy report)"]
        ),
        "Reasoning: "
        + {
            "DETERMINISTIC": "Deterministic",
            "AI_ASSISTED": "AI-assisted",
            "AI_FALLBACK": "AI fallback",
        }[result.reasoning_mode],
        *([f"Configured AI model: {result.reasoning_model}"] if result.reasoning_model else []),
        *([result.ai_warning] if result.ai_warning else []),
        "Portfolio",
        f"Total: ${f.total_portfolio_usd:,.2f}",
        f"Stables: ${f.current_stables_usd:,.2f}",
        f"Stable allocation: {f.stable_allocation_percent:.2f}%",
        "Reserve",
        f"Monthly expenses: ${f.monthly_expenses_usd:,.2f}",
        f"Target runway: {f.target_expense_runway_months} months",
        f"Actual runway: {f.actual_expense_runway_months:.2f} months",
        f"Recommended reserve: ${f.recommended_minimum_stables_usd:,.2f}",
        f"Deployable stables: ${f.deployable_stables_usd:,.2f}",
        *(
            [
                f"Hard expense reserve: ${f.hard_expense_reserve_usd:,.2f}",
                f"Market buffer: ${f.market_buffer_usd:,.2f}",
                f"Potential trading liquidity: ${f.potential_trading_liquidity_usd:,.2f}",
                f"Strategy dry powder: ${f.strategy_dry_powder_usd:,.2f}",
                f"Recommended deployment: ${f.recommended_deployment_usd:,.2f}",
            ]
            if result.active
            else []
        ),
        f"Reserve deficit: ${f.stable_reserve_deficit_usd:,.2f}",
        f"Existing BUY commitments: ${f.open_buy_commitments_usd:,.2f}",
        "Market",
        f"Cutoff: {result.market_cutoff or 'unavailable'}",
        f"Regime: {result.market_regime.value.upper()}",
        f"Regime policy: v{result.regime_policy_version}; "
        f"recommendation policy: v{result.recommendation_policy_version}",
        f"Source: {'LIVE' if result.market.is_live else 'STATIC / DEVELOPMENT ONLY'}",
        *([f"Telegram severity: {result.active.severity.value.upper()}"] if result.active else []),
        *(["Positions"] if result.active else []),
        *(
            [
                f"{d.asset}: {d.quantity} (${d.market_value_usd:,.2f}, "
                f"{d.allocation_percent:.2f}%) "
                f"decision={d.decision.value.upper()} core_floor={d.core_floor or 'none'} "
                f"preferred={d.preferred_core or 'none'} tradable={d.tradable_quantity} "
                f"1D={d.daily_trend.value.upper()}"
                for d in result.active.asset_decisions
            ]
            if result.active
            else []
        ),
        *(["Swing setups"] if result.active else []),
        *(render_setups(result.active).splitlines() if result.active else []),
        "Actions",
    ]
    with calculation_context():
        for a in result.recommendations:
            lines += [a.asset or "Portfolio", a.action_type.value.upper().replace("_", " ")]
            if a.side is not None:
                lines.append(f"Side: {a.side.value.upper()}")
            if a.existing_order_id is not None:
                lines.append(f"Order ID: {a.existing_order_id}")
            if a.setup_id is not None:
                lines.append(f"Setup ID: {a.setup_id}")
            if a.target_price is not None:
                lines += [
                    f"Target: ${a.target_price}",
                    f"Order value: ${a.target_price * a.quantity_asset:,.2f}",
                ]
            if a.quantity_asset is not None:
                lines.append(f"Quantity: {a.quantity_asset}")
            lines += [f"Priority: {a.priority}", f"Reason: {a.reason}"]
    lines += [
        "Warnings",
        *(result.warnings or ("None",)),
        f"Analysis run: {result.run_id}",
        f"Snapshot: {result.snapshot_id}",
        f"Generated: {result.generated_at}",
        "This is decision support only; no trades were executed.",
    ]
    return "\n".join(lines)


def render_snapshot(snapshot):
    lines = [
        f"Snapshot: {snapshot.id}",
        f"Confirmed: {snapshot.confirmed_at}",
        "Stable classification: USDT, USDC (fixed spot configuration)",
    ]
    lines += [f"{p.symbol}: {p.amount}" for p in snapshot.portfolio.positions]
    lines += [
        f"Core {c.symbol}: floor {c.hard_floor}; preferred {c.preferred_quantity}; "
        f"policy v{c.policy_version}"
        for c in snapshot.core_positions
    ]
    lines += [
        f"Monthly expenses: {snapshot.financial_settings.monthly_expenses_usd} USD",
        f"Target runway: {snapshot.financial_settings.target_expense_runway_months} months",
    ]
    lines += [
        f"Order {o.id}: {o.asset} {o.side.value} {o.quantity_asset} "
        f"@ ${o.target_price} [{o.status.value}]"
        for o in snapshot.orders
    ]
    return "\n".join(lines)


def _plain(value):
    return format(value, "f")


def _stable_lines(portfolio, settlement_asset):
    holdings = {position.symbol: position.amount for position in portfolio.positions}
    zero = Decimal("0")
    with calculation_context():
        total = sum((holdings.get(symbol, zero) for symbol in SETTLEMENT_ASSETS), zero)
    lines = [f"STABLES: {_plain(total)}"]
    lines += [
        f"  {symbol}: {_plain(holdings.get(symbol, zero))}"
        for symbol in ("USDT", "USDC")
        if symbol in holdings or symbol == settlement_asset
    ]
    return lines


def render_manual_spot_trade(proposal: ManualSpotTradeProposal) -> str:
    trade = proposal.trade
    calculation = proposal.calculation
    before = proposal.current.snapshot.portfolio
    after = calculation.portfolio
    before_holdings = {position.symbol: position.amount for position in before.positions}
    after_holdings = {position.symbol: position.amount for position in after.positions}
    zero = Decimal("0")
    lines = [
        "Before:",
        f"{trade.asset}: {_plain(before_holdings.get(trade.asset, zero))}",
        *_stable_lines(before, trade.settlement_asset),
        "",
        "Execution:",
        f"{trade.side.value.upper()} {trade.asset}",
        f"Price: {_plain(trade.execution_price)} {trade.settlement_asset}",
    ]
    if trade.side is TradingSide.BUY:
        lines += [
            f"Total stable debited: {_plain(trade.stable_debit)} {trade.settlement_asset}",
            f"Trade notional: {_plain(calculation.gross_quote_amount)} {trade.settlement_asset}",
            f"Fee: {_plain(trade.fee_stable)} {trade.settlement_asset}",
            f"Asset received: {_plain(calculation.executed_asset_quantity)} {trade.asset}",
        ]
    else:
        lines += [
            f"Asset sold: {_plain(calculation.executed_asset_quantity)} {trade.asset}",
            f"Gross proceeds: {_plain(calculation.gross_quote_amount)} {trade.settlement_asset}",
            f"Fee: {_plain(trade.fee_stable)} {trade.settlement_asset}",
            f"Net stable credit: {_plain(calculation.stable_balance_change)} "
            f"{trade.settlement_asset}",
        ]
    lines += [
        "",
        "After:",
        f"{trade.asset}: {_plain(after_holdings.get(trade.asset, zero))}",
        *_stable_lines(after, trade.settlement_asset),
        "",
        *calculation.warnings,
        "Monthly expenses, runway, core policies, and open orders are unchanged.",
        "No trade will be executed by this application.",
    ]
    return "\n".join(lines)
