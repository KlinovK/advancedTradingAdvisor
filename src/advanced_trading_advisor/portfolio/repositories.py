from typing import Protocol
from uuid import UUID

from advanced_trading_advisor.domain.persistence import ConfirmedUpdate, CurrentPortfolio

from .models import ConfirmedPortfolioSnapshot


class SnapshotRepository(Protocol):
    async def save(self, snapshot: ConfirmedPortfolioSnapshot) -> None: ...
    async def get(self, snapshot_id: UUID) -> ConfirmedPortfolioSnapshot | None: ...
    async def get_current(self) -> CurrentPortfolio | None: ...
    async def list_recent(self, limit: int = 20) -> tuple[ConfirmedPortfolioSnapshot, ...]: ...
    async def set_current(
        self,
        snapshot: ConfirmedPortfolioSnapshot,
        *,
        expected_revision: int,
        update: ConfirmedUpdate,
    ) -> CurrentPortfolio:
        """Atomically persist snapshot, audit entry and revision-checked current pointer."""
        ...


class ConfirmedUpdateRepository(Protocol):
    async def append(self, update: ConfirmedUpdate) -> None: ...
    async def list_recent(self, limit: int = 20) -> tuple[ConfirmedUpdate, ...]: ...
