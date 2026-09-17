from datetime import datetime
from typing import Protocol
from uuid import UUID

from advanced_trading_advisor.domain.persistence import FailureCategory, StoredAnalysisRun

from .models import AnalysisRun


class AnalysisRunRepository(Protocol):
    async def acquire(self, run: AnalysisRun) -> None: ...
    async def list_all_recent(self, limit: int = 20) -> tuple[StoredAnalysisRun, ...]: ...
    async def latest_completed(self) -> StoredAnalysisRun | None: ...
    async def create(self, run: AnalysisRun) -> None: ...
    async def get(self, run_id: UUID) -> StoredAnalysisRun | None: ...
    async def mark_completed(
        self,
        run_id: UUID,
        *,
        completed_at: datetime,
        result_json: str | None = None,
        market_cutoff: datetime | None = None,
    ) -> StoredAnalysisRun: ...
    async def mark_failed(
        self, run_id: UUID, *, completed_at: datetime, failure_category: FailureCategory
    ) -> StoredAnalysisRun: ...
    async def list_recent(
        self, snapshot_id: UUID, limit: int = 20
    ) -> tuple[StoredAnalysisRun, ...]: ...
