"""One bounded, human-readable Telegram message from a persisted AnalysisResult."""

import re
from datetime import UTC

from advanced_trading_advisor.analysis.recommendations import ActionType
from advanced_trading_advisor.analysis.result import AnalysisResult
from advanced_trading_advisor.domain.numeric import calculation_context

MAX_MESSAGE_LENGTH = 4096
FOOTER = "No trades were executed automatically."
PASSIVE = {
    ActionType.HOLD,
    ActionType.WAIT,
    ActionType.REBALANCE,
    ActionType.REBALANCE_INFORMATIONAL,
    ActionType.KEEP_LIMIT_ORDER,
}
ACTION_NAMES = {
    ActionType.BUY: "BUY",
    ActionType.SELL: "SELL",
    ActionType.HOLD: "HOLD",
    ActionType.WAIT: "WAIT",
    ActionType.PLACE_LIMIT_ORDER: "PLACE LIMIT ORDER",
    ActionType.KEEP_LIMIT_ORDER: "KEEP LIMIT ORDER",
    ActionType.CANCEL_LIMIT_ORDER: "CANCEL LIMIT ORDER",
    ActionType.REPLACE_LIMIT_ORDER: "REPLACE LIMIT ORDER",
    ActionType.REBALANCE: "REBALANCE — informational",
    ActionType.REBALANCE_INFORMATIONAL: "REBALANCE — informational",
}
WARNING_LABELS = {
    "stable_reserve_deficit": "Cash reserve below required level",
    "no_deployable_stables": "No cash available to deploy",
    "buy_commitments_exceed_stables": "Open buy orders exceed available cash",
    "sell_quantity_exceeds_holdings": "Open sell orders exceed holdings",
    "excessive_asset_concentration": "High asset concentration",
    "incomplete_market_data": "Incomplete market data",
    "core_floor_conflicts_with_open_sell": "Open sell order conflicts with core position",
    "candidate_plan_rejected": "Trading advice unavailable; review the full analysis",
}
INTERNAL_REASON = "proposal passed whole-plan checks against the recorded snapshot"


def message_units(text):
    # Conservative for Telegram entity offsets and astral Unicode characters.
    return len(text.encode("utf-16-le")) // 2


def plain(text, limit=240):
    """Bound free text at word boundaries and suppress links/mentions without markup."""
    safe = re.sub(r"(?:https?://|www\.|t\.me/)\S+", "[link omitted]", text, flags=re.I)
    safe = "".join(char if char.isprintable() or char.isspace() else " " for char in safe)
    safe = re.sub(r"(?<!\w)@[A-Za-z0-9_]+", "[mention omitted]", safe)
    words = safe.split()
    kept = []
    for word in words:
        candidate = " ".join((*kept, word))
        if len(candidate) > limit:
            break
        kept.append(word)
    if len(kept) < len(words):
        return (" ".join(kept) + " …") if kept else "[long text omitted]"
    return " ".join(kept)


def money(value):
    return f"${value:,.2f}"


def one_decimal(value):
    return f"{value:.1f}"


def warning_label(value):
    if value in WARNING_LABELS:
        return WARNING_LABELS[value]
    if re.fullmatch(r"[a-z][a-z0-9_]*", value):
        return plain(value.replace("_", " "), 120).capitalize()
    return plain(value, 120)


def action_block(action):
    lines = [f"{action.asset or 'PORTFOLIO'} — {ACTION_NAMES[action.action_type]}"]
    if action.side is not None:
        lines.append(f"Side: {action.side.value.upper()}")
    if action.quantity_asset is not None:
        lines.append(f"Quantity: {format(action.quantity_asset, 'f')}")
    if action.target_price is not None:
        # This is an executable term: preserve every decimal place instead of rounding it.
        lines.append(f"Limit price: ${format(action.target_price, ',f')}")
    if action.target_price is not None and action.quantity_asset is not None:
        with calculation_context():
            lines.append(f"Approx value: {money(action.target_price * action.quantity_asset)}")
    if action.setup_id is not None:
        lines.append(f"Setup ID: {action.setup_id}")
    if action.existing_order_id is not None:
        lines.append(f"Existing order ID: {action.existing_order_id}")
    lines.append(f"Priority: P{action.priority}")
    if action.invalidation_condition is not None:
        lines.append(f"Invalidation: {plain(action.invalidation_condition)}")
    if action.review_at is not None:
        lines.append(f"Review: {action.review_at.astimezone(UTC):%b %d, %H:%M UTC}")
    if action.expires_at is not None:
        lines.append(f"Expires: {action.expires_at.astimezone(UTC):%b %d, %H:%M UTC}")
    if INTERNAL_REASON not in action.reason.casefold():
        lines.append(f"Why: {plain(action.reason)}")
    return "\n".join(lines)


def _market_decisions(result):
    if result.active is not None and "candidate_plan_rejected" not in result.warnings:
        return [
            f"{decision.asset}: {decision.decision.value.upper().replace('_', ' ')}"
            if decision.decision.value != "rebalance_informational"
            else f"{decision.asset}: REBALANCE — informational"
            for decision in result.active.asset_decisions
        ]
    seen = set()
    actionable_assets = {
        action.asset for action in result.recommendations if action.action_type not in PASSIVE
    }
    lines = []
    for action in result.recommendations:
        if (
            action.asset is not None
            and action.asset not in seen
            and action.asset not in actionable_assets
            and action.action_type in PASSIVE
        ):
            seen.add(action.asset)
            lines.append(f"{action.asset}: {ACTION_NAMES[action.action_type]}")
    return lines


def format_analysis(result: AnalysisResult) -> str:
    f = result.financial
    actionable = tuple(a for a in result.recommendations if a.action_type not in PASSIVE)
    cutoff = (
        result.market_cutoff.astimezone(UTC).strftime("%b %d, %H:%M UTC")
        if result.market_cutoff
        else "unavailable"
    )
    reasoning = {
        "DETERMINISTIC": "Deterministic analysis",
        "AI_ASSISTED": "AI-assisted analysis",
        "AI_FALLBACK": "AI fallback — deterministic analysis",
    }[result.reasoning_mode.value]
    summary = [
        "MARKET UPDATE",
        "Action required" if actionable else "No action required",
        "",
        "Portfolio",
        money(f.total_portfolio_usd),
        f"Cash: {money(f.current_stables_usd)} ({one_decimal(f.stable_allocation_percent)}%)",
        "",
        "Reserve",
        f"Required: {money(f.recommended_minimum_stables_usd)}",
        f"Available to deploy: {money(f.deployable_stables_usd)}",
        f"Runway: {one_decimal(f.actual_expense_runway_months)} months",
    ]
    if f.stable_reserve_deficit_usd > 0:
        summary.append(f"Deficit: {money(f.stable_reserve_deficit_usd)}")
    summary.extend(("", "Market", f"Regime: {result.market_regime.value.upper()}"))
    decisions = _market_decisions(result)
    summary.extend(decisions[:6])
    if len(decisions) > 6:
        summary.append(f"{len(decisions) - 6} more asset decisions in the full analysis.")
    kept_orders = sum(
        action.action_type is ActionType.KEEP_LIMIT_ORDER for action in result.recommendations
    )
    if kept_orders:
        summary.append(f"Open orders kept: {kept_orders}")
    if result.active is not None:
        setups = [
            f"{s.asset}: {s.setup_type.value.upper()} — {s.status.value.replace('_', ' ')}"
            for s in result.active.setups
            if s.trigger is not None
            and s.status.value
            in {"zone_touched", "awaiting_confirmation", "confirmed", "invalidated", "expired"}
        ]
        if setups:
            summary.extend(("", "Setups", *setups[:6]))
            if len(setups) > 6:
                summary.append(f"{len(setups) - 6} more setups in the full analysis.")
    warnings = list(result.warnings)
    if result.ai_warning:
        warnings.append(result.ai_warning)
    summary_text = "\n".join(summary)
    footer = f"Market data: {cutoff}\n{reasoning}\n\n{FOOTER}"
    sorted_actions = sorted(enumerate(actionable), key=lambda pair: (pair[1].priority, pair[0]))
    selected = []
    omitted_actions = 0

    def render(warning_lines):
        warning_section = "Warnings\n" + ("\n".join(warning_lines) if warning_lines else "None")
        action_section = "ACTION NOW\n" + (
            "\n\n".join(selected)
            if selected
            else "See the full analysis for actions."
            if omitted_actions
            else "NONE"
        )
        if omitted_actions:
            action_section += f"\n{omitted_actions} action(s) omitted; see the full analysis."
        return "\n\n".join((summary_text, warning_section, action_section, footer))

    for _, action in sorted_actions:
        block = action_block(action)
        selected.append(block)
        if message_units(render(())) + 100 > MAX_MESSAGE_LENGTH:
            selected.pop()
            omitted_actions += 1
    warning_lines = []
    omitted_warnings = 0
    for warning in warnings:
        warning_lines.append(warning_label(warning))
        if message_units(render(warning_lines)) + 60 > MAX_MESSAGE_LENGTH:
            warning_lines.pop()
            omitted_warnings += 1
    if omitted_warnings:
        warning_lines.append(f"{omitted_warnings} warning(s) omitted; see the full analysis.")
    message = render(warning_lines)
    if message_units(message) > MAX_MESSAGE_LENGTH:
        raise ValueError("Telegram report exceeds the message limit.")
    return message
