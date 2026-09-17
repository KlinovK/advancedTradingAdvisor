"""One bounded plain-text message from a final persisted AnalysisResult."""

import re
from datetime import UTC

from advanced_trading_advisor.analysis.active import TelegramSeverity
from advanced_trading_advisor.analysis.recommendations import ActionType
from advanced_trading_advisor.analysis.result import AnalysisResult
from advanced_trading_advisor.domain.numeric import calculation_context

MAX_MESSAGE_LENGTH = 4096
FOOTER = "No trades were executed. Confirm portfolio/order changes after executing them manually."
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
    ActionType.REBALANCE: "REBALANCE",
    ActionType.REBALANCE_INFORMATIONAL: "REBALANCE (INFORMATIONAL)",
}


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


def decimal(value):
    return format(value, "f")


def action_block(action):
    name = ACTION_NAMES[action.action_type]
    prefix = "RECOMMEND: " if action.action_type not in PASSIVE else ""
    lines = [f"{action.asset or 'PORTFOLIO'} — {prefix}{name}"]
    if action.existing_order_id is not None:
        lines.append(f"Existing order ID: {action.existing_order_id}")
    if action.setup_id is not None:
        lines.append(f"Setup ID: {action.setup_id}")
    if action.side is not None:
        lines.append(f"Side: {action.side.value.upper()}")
    if action.target_price is not None:
        lines.append(f"Target: ${decimal(action.target_price)}")
    if action.quantity_asset is not None:
        lines.append(f"Quantity: {decimal(action.quantity_asset)}")
    if action.target_price is not None and action.quantity_asset is not None:
        with calculation_context():
            lines.append(f"Approx value: ${decimal(action.target_price * action.quantity_asset)}")
    lines.append(f"Priority: P{action.priority}")
    lines.append(f"Reason: {plain(action.reason)}")
    return "\n".join(lines)


def format_analysis(result: AnalysisResult) -> str:
    if result.active is not None and result.active.severity is TelegramSeverity.QUIET:
        kept = sum(a.action_type is ActionType.KEEP_LIMIT_ORDER for a in result.recommendations)
        lines = [
            "NO TRADING ACTION NEEDED",
            f"Market: {result.market_regime.value.upper()}",
            "Portfolio unchanged; no validated setup merits a trade.",
            f"Existing orders kept: {kept}.",
            "Next review: ~4h.",
        ]
        if result.reasoning_mode.value == "AI_FALLBACK":
            lines.append("Reasoning: AI fallback")
        if result.ai_warning:
            lines.append(result.ai_warning)
        lines.extend(f"Warning: {plain(warning)}" for warning in result.warnings)
        lines.append(FOOTER)
        message = "\n".join(lines)
        if message_units(message) > MAX_MESSAGE_LENGTH:
            raise ValueError("Telegram quiet report exceeds the message limit.")
        return message
    f = result.financial
    generated = result.generated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    cutoff = (
        result.market_cutoff.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
        if result.market_cutoff
        else "unavailable"
    )
    reasoning = {
        "DETERMINISTIC": "Deterministic",
        "AI_ASSISTED": "AI-assisted",
        "AI_FALLBACK": "AI fallback",
    }[result.reasoning_mode.value]
    trigger_lines = []
    if result.active is not None:
        trigger_lines = [
            f"{s.asset} {s.setup_type.value.upper()}: {s.status.value.upper()}"
            for s in result.active.setups
            if s.trigger is not None
            and s.status.value
            in {"zone_touched", "awaiting_confirmation", "confirmed", "invalidated", "expired"}
        ][:3]
    header = "\n".join(
        [
            "MARKET UPDATE — NO TRADE"
            if result.active is not None and result.active.severity is TelegramSeverity.UPDATE
            else "ADVANCED TRADING ADVISOR — ACTION"
            if result.active is not None
            else "ADVANCED TRADING ADVISOR",
            generated,
            "",
            "PORTFOLIO",
            f"Value: ${decimal(f.total_portfolio_usd)}",
            f"Stables: ${decimal(f.current_stables_usd)} ({decimal(f.stable_allocation_percent)}%)",
            "",
            "RESERVE",
            f"Required: ${decimal(f.recommended_minimum_stables_usd)}",
            f"Deployable: ${decimal(f.deployable_stables_usd)}",
            f"Deficit: ${decimal(f.stable_reserve_deficit_usd)}",
            f"Runway: {decimal(f.actual_expense_runway_months)} months",
            "",
            "MARKET",
            f"Regime: {result.market_regime.value.upper()}",
            f"Candle: {cutoff}",
            f"Reasoning: {reasoning}",
            *(["", "SWING TRIGGERS", *trigger_lines] if trigger_lines else []),
            "",
            "ACTIONS",
        ]
    )
    if message_units(header) + message_units(FOOTER) + 80 > MAX_MESSAGE_LENGTH:
        raise ValueError("Telegram report header exceeds the message limit.")
    actions = sorted(
        enumerate(result.recommendations),
        key=lambda pair: (pair[1].action_type in PASSIVE, pair[1].priority, pair[0]),
    )
    selected = []
    omitted_actions = 0
    warnings = list(result.warnings)
    if result.ai_warning:
        warnings.append(result.ai_warning)
    reserve = 100  # Space for an omission count, warnings heading and advisory footer.
    for _, action in actions:
        block = action_block(action)
        candidate = "\n\n".join((header, *selected, block))
        if message_units(candidate) + message_units(FOOTER) + reserve <= MAX_MESSAGE_LENGTH:
            selected.append(block)
        else:
            omitted_actions += 1
    lines = [header, *selected]
    if omitted_actions:
        lines.append(f"{omitted_actions} action(s) omitted for message length.")
    if warnings:
        lines.append("WARNINGS")
    omitted_warnings = 0
    for warning in warnings:
        block = "- " + plain(warning)
        if message_units("\n\n".join((*lines, block, FOOTER))) + reserve <= MAX_MESSAGE_LENGTH:
            lines.append(block)
        else:
            omitted_warnings += 1
    if omitted_warnings:
        lines.append(f"{omitted_warnings} warning(s) omitted for message length.")
    lines.append(FOOTER)
    message = "\n\n".join(lines)
    if message_units(message) > MAX_MESSAGE_LENGTH:
        raise ValueError("Telegram message exceeds the size limit.")
    return message
