"""Manually reconcile one order account while confirming its OPEN order filled."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import ConflictError, CurrentPortfolio, UpdateType
from advanced_trading_advisor.domain.validation import identity, require

from .confirmed_state import ConfirmedStateService
from .models import (
    AssetPosition,
    LimitOrderStatus,
    Portfolio,
    StablecoinClassification,
    TradingSide,
)
from .repositories import SnapshotRepository

ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ConfirmLimitOrderFilledCommand:
    expected_revision: int
    order_id: UUID
    settlement_asset: str
    positions: tuple[AssetPosition, ...]

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )
        identity(self.order_id)
        require(
            StablecoinClassification().is_stable(self.settlement_asset),
            "Settlement stablecoin must be USDT or USDC.",
        )


class ConfirmLimitOrderFilled:
    def __init__(
        self,
        snapshots: SnapshotRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        identity_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._confirmed = ConfirmedStateService(snapshots)
        self._now = now
        self._identity_factory = identity_factory

    async def execute(self, command: ConfirmLimitOrderFilledCommand) -> CurrentPortfolio:
        require(
            isinstance(command, ConfirmLimitOrderFilledCommand),
            "Confirm limit order filled command required.",
        )
        current = await self._confirmed.load_current()
        require(current is not None, "No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise ConflictError("Current state changed; reload before confirming.")

        previous = current.snapshot
        order = next((item for item in previous.orders if item.id == command.order_id), None)
        require(order is not None, "Limit order does not exist in the confirmed portfolio.")
        require(order.status is LimitOrderStatus.OPEN, "Only an OPEN order can be filled.")
        accounts = previous.portfolio.accounts
        index = next(
            (i for i, account in enumerate(accounts) if account.id == order.account_id), None
        )
        require(index is not None, "Order account does not exist in the confirmed portfolio.")

        account = accounts[index]
        reconciled = replace(account, positions=command.positions)
        before = {position.symbol: position.amount for position in account.positions}
        after = {position.symbol: position.amount for position in reconciled.positions}
        if order.side is TradingSide.BUY:
            require(
                after.get(order.asset, ZERO) > before.get(order.asset, ZERO),
                "BUY requires the order asset balance to increase in its account.",
            )
            require(
                after.get(command.settlement_asset, ZERO)
                < before.get(command.settlement_asset, ZERO),
                "BUY requires the selected settlement balance to decrease in its account.",
            )
        else:
            require(
                after.get(order.asset, ZERO) < before.get(order.asset, ZERO),
                "SELL requires the order asset balance to decrease in its account.",
            )
            require(
                after.get(command.settlement_asset, ZERO)
                > before.get(command.settlement_asset, ZERO),
                "SELL requires the selected settlement balance to increase in its account.",
            )

        at = self._now()
        filled = replace(order, status=LimitOrderStatus.FILLED, updated_at=at, resolved_at=at)
        snapshot = replace(
            previous,
            id=self._identity_factory(),
            confirmed_at=at,
            portfolio=Portfolio(accounts[:index] + (reconciled,) + accounts[index + 1 :]),
            orders=tuple(filled if item.id == order.id else item for item in previous.orders),
        )
        return await self._confirmed.confirm(
            snapshot,
            expected_revision=command.expected_revision,
            previous_snapshot_id=previous.id,
            update_type=UpdateType.ORDER_FILLED,
        )
