from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import (
    ConfirmedUpdate,
    CurrentPortfolio,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import require

from .models import ConfirmedPortfolioSnapshot
from .repositories import SnapshotRepository


class ConfirmedStateService:
    """Explicit user-confirmation boundary. No recommendation input is accepted."""

    def __init__(self, snapshots: SnapshotRepository):
        self._snapshots = snapshots

    async def load_current(self) -> CurrentPortfolio | None:
        return await self._snapshots.get_current()

    async def initialize(
        self, snapshot: ConfirmedPortfolioSnapshot, *, update_id: UUID | None = None
    ) -> CurrentPortfolio:
        require(
            isinstance(snapshot, ConfirmedPortfolioSnapshot), "A confirmed snapshot is required."
        )
        update = ConfirmedUpdate(
            update_id or uuid4(), UpdateType.INITIAL_STATE, None, snapshot.id, snapshot.confirmed_at
        )
        return await self._snapshots.set_current(snapshot, expected_revision=0, update=update)

    async def confirm(
        self,
        snapshot: ConfirmedPortfolioSnapshot,
        *,
        expected_revision: int,
        previous_snapshot_id: UUID,
        update_type: UpdateType,
        update_id: UUID | None = None,
    ) -> CurrentPortfolio:
        require(
            isinstance(snapshot, ConfirmedPortfolioSnapshot), "A confirmed snapshot is required."
        )
        require(
            type(expected_revision) is int and expected_revision > 0, "Invalid expected revision."
        )
        require(update_type is not UpdateType.INITIAL_STATE, "Use initialize for initial state.")
        update = ConfirmedUpdate(
            update_id or uuid4(),
            update_type,
            previous_snapshot_id,
            snapshot.id,
            snapshot.confirmed_at,
        )
        return await self._snapshots.set_current(
            snapshot, expected_revision=expected_revision, update=update
        )
