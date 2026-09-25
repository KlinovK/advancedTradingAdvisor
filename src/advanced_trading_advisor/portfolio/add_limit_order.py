"""Explicit, revision-checked recording of one account-aware limit order."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import (
    CurrentPortfolio,
    RevisionConflictError,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import ResourceNotFoundError, identity, require

from .confirmed_state import ConfirmedStateService
from .models import LimitOrder, LimitOrderStatus, TradingSide
from .repositories import SnapshotRepository

SUPPORTED_ORDER_ASSETS = frozenset({"BTC", "ETH", "SOL", "USDT", "USDC"})


@dataclass(frozen=True, slots=True)
class AddLimitOrderCommand:
    expected_revision: int
    account_id: UUID
    asset: str
    side: TradingSide
    target_price: Decimal
    quantity_asset: Decimal

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )
        identity(self.account_id)


class AddLimitOrder:
    def __init__(
        self,
        snapshots: SnapshotRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        order_identity_factory: Callable[[], UUID] = uuid4,
        snapshot_identity_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._confirmed = ConfirmedStateService(snapshots)
        self._now = now
        self._order_identity_factory = order_identity_factory
        self._snapshot_identity_factory = snapshot_identity_factory

    async def execute(self, command: AddLimitOrderCommand) -> CurrentPortfolio:
        require(isinstance(command, AddLimitOrderCommand), "Add limit order command required.")
        current = await self._confirmed.load_current()
        if current is None:
            raise ResourceNotFoundError("No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise RevisionConflictError("Current state changed; reload before confirming.")
        if not any(
            account.id == command.account_id for account in current.snapshot.portfolio.accounts
        ):
            raise ResourceNotFoundError("Order account must exist in the confirmed portfolio.")

        at = self._now()
        order = LimitOrder(
            self._order_identity_factory(),
            command.asset,
            command.side,
            command.target_price,
            command.quantity_asset,
            LimitOrderStatus.OPEN,
            at,
            at,
            account_id=command.account_id,
        )
        require(order.asset in SUPPORTED_ORDER_ASSETS, "Unsupported asset.")
        snapshot = replace(
            current.snapshot,
            id=self._snapshot_identity_factory(),
            confirmed_at=at,
            orders=current.snapshot.orders + (order,),
        )
        return await self._confirmed.confirm(
            snapshot,
            expected_revision=command.expected_revision,
            previous_snapshot_id=current.snapshot.id,
            update_type=UpdateType.ORDER_ADDED,
        )
