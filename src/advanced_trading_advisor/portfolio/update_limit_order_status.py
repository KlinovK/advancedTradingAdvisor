"""Explicit, revision-checked cancellation or expiration of an OPEN limit order."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import (
    CurrentPortfolio,
    RevisionConflictError,
    StateConflictError,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import ResourceNotFoundError, identity, require

from .confirmed_state import ConfirmedStateService
from .models import LimitOrderStatus
from .repositories import SnapshotRepository


@dataclass(frozen=True, slots=True)
class UpdateLimitOrderStatusCommand:
    expected_revision: int
    order_id: UUID
    target_status: LimitOrderStatus

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )
        identity(self.order_id)


class UpdateLimitOrderStatus:
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

    async def execute(self, command: UpdateLimitOrderStatusCommand) -> CurrentPortfolio:
        require(
            isinstance(command, UpdateLimitOrderStatusCommand),
            "Update limit order status command required.",
        )
        current = await self._confirmed.load_current()
        if current is None:
            raise ResourceNotFoundError("No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise RevisionConflictError("Current state changed; reload before confirming.")
        require(
            isinstance(command.target_status, LimitOrderStatus)
            and command.target_status in {LimitOrderStatus.CANCELLED, LimitOrderStatus.EXPIRED},
            "Only CANCELLED or EXPIRED status is supported.",
        )

        previous = current.snapshot
        old = next((order for order in previous.orders if order.id == command.order_id), None)
        if old is None:
            raise ResourceNotFoundError("Limit order does not exist in the confirmed portfolio.")
        if old.status is not LimitOrderStatus.OPEN:
            raise StateConflictError("Only an OPEN order can be resolved.")
        at = self._now()
        updated = replace(old, status=command.target_status, updated_at=at, resolved_at=at)
        snapshot = replace(
            previous,
            id=self._identity_factory(),
            confirmed_at=at,
            orders=tuple(updated if order.id == old.id else order for order in previous.orders),
        )
        update_type = {
            LimitOrderStatus.CANCELLED: UpdateType.ORDER_CANCELLED,
            LimitOrderStatus.EXPIRED: UpdateType.ORDER_EXPIRED,
        }[command.target_status]
        return await self._confirmed.confirm(
            snapshot,
            expected_revision=command.expected_revision,
            previous_snapshot_id=previous.id,
            update_type=update_type,
        )
