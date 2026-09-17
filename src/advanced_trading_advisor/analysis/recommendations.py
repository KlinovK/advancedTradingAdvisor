from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from advanced_trading_advisor.domain.numeric import calculation_context, finite_decimal
from advanced_trading_advisor.domain.validation import identity, require, symbol, timestamp
from advanced_trading_advisor.portfolio.models import TradingSide


class ActionType(StrEnum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"
    WAIT = "wait"
    PLACE_LIMIT_ORDER = "place_limit_order"
    KEEP_LIMIT_ORDER = "keep_limit_order"
    CANCEL_LIMIT_ORDER = "cancel_limit_order"
    REPLACE_LIMIT_ORDER = "replace_limit_order"
    REBALANCE = "rebalance"
    REBALANCE_INFORMATIONAL = "rebalance_informational"


@dataclass(frozen=True, slots=True)
class Recommendation:
    recommendation_id: UUID
    asset: str | None
    action_type: ActionType
    priority: int
    reason: str
    existing_order_id: UUID | None = None
    side: TradingSide | None = None
    target_price: Decimal | None = None
    quantity_asset: Decimal | None = None
    invalidation_condition: str | None = None
    review_at: datetime | None = None
    expires_at: datetime | None = None
    setup_id: UUID | None = None

    def __post_init__(self):
        identity(self.recommendation_id)
        require(isinstance(self.action_type, ActionType), "Invalid action type.")
        require(type(self.priority) is int and 1 <= self.priority <= 5, "Priority must be 1–5.")
        require(isinstance(self.reason, str) and bool(self.reason.strip()), "Reason is required.")
        if self.asset is not None:
            symbol(self.asset)
        if self.existing_order_id is not None:
            identity(self.existing_order_id)
        if self.setup_id is not None:
            identity(self.setup_id)
        if self.side is not None:
            require(isinstance(self.side, TradingSide), "Invalid side.")
        for value in (self.target_price, self.quantity_asset):
            if value is not None:
                finite_decimal(value, positive=True)
        if self.invalidation_condition is not None:
            require(
                isinstance(self.invalidation_condition, str)
                and bool(self.invalidation_condition.strip()),
                "Invalid invalidation condition.",
            )
        for value in (self.review_at, self.expires_at):
            if value is not None:
                timestamp(value)
        if self.review_at is not None and self.expires_at is not None:
            require(self.review_at <= self.expires_at, "Review must not follow expiry.")
        referenced = {
            ActionType.KEEP_LIMIT_ORDER,
            ActionType.CANCEL_LIMIT_ORDER,
            ActionType.REPLACE_LIMIT_ORDER,
        }
        require(
            (self.existing_order_id is not None) == (self.action_type in referenced),
            "Existing order reference is required only for KEEP, CANCEL and REPLACE.",
        )
        passive = {
            ActionType.HOLD,
            ActionType.WAIT,
            ActionType.REBALANCE,
            ActionType.REBALANCE_INFORMATIONAL,
        }
        if self.action_type in passive:
            require(
                self.side is None
                and self.target_price is None
                and self.quantity_asset is None
                and self.setup_id is None,
                "Informational actions cannot contain trade terms.",
            )
            return
        require(self.asset is not None, "Trading/order actions require an asset.")
        if self.action_type in {ActionType.KEEP_LIMIT_ORDER, ActionType.CANCEL_LIMIT_ORDER}:
            require(
                self.target_price is None and self.quantity_asset is None,
                "KEEP/CANCEL terms come from the referenced order.",
            )
            return
        require(
            self.side is not None and self.quantity_asset is not None,
            "Trade proposal requires side and quantity.",
        )
        if self.action_type in {ActionType.PLACE_LIMIT_ORDER, ActionType.REPLACE_LIMIT_ORDER}:
            require(self.target_price is not None, "Limit proposal requires complete price terms.")
        else:
            expected = TradingSide.BUY if self.action_type is ActionType.BUY else TradingSide.SELL
            require(self.side is expected, "Action and side disagree.")
            require(self.target_price is None, "Use a limit action for a target price.")


@dataclass(frozen=True, slots=True)
class CanonicalActionTerms:
    """Exact deterministic action authority shared by AI context and acceptance."""

    action_type: str
    asset: str | None
    side: str | None
    amount_usd: str | None
    quantity_asset: str | None
    target_price: str | None
    setup_id: str | None
    existing_order_id: str | None
    priority: int
    review_at: str | None
    expires_at: str | None

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


def canonical_action_terms(
    actions: tuple[Recommendation, ...],
) -> tuple[CanonicalActionTerms, ...]:
    def decimal(value: Decimal | None) -> str | None:
        return format(value, "f") if value is not None else None

    def date(value: datetime | None) -> str | None:
        return value.astimezone(UTC).isoformat(timespec="microseconds") if value else None

    with calculation_context():
        return tuple(
            CanonicalActionTerms(
                action_type=action.action_type.value,
                asset=action.asset,
                side=action.side.value if action.side else None,
                amount_usd=decimal(
                    action.target_price * action.quantity_asset
                    if action.target_price is not None and action.quantity_asset is not None
                    else None
                ),
                quantity_asset=decimal(action.quantity_asset),
                target_price=decimal(action.target_price),
                setup_id=str(action.setup_id) if action.setup_id else None,
                existing_order_id=str(action.existing_order_id)
                if action.existing_order_id
                else None,
                priority=action.priority,
                review_at=date(action.review_at),
                expires_at=date(action.expires_at),
            )
            for action in actions
        )
