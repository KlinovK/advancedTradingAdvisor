"""Read-only application boundary for persisted analysis history."""

from dataclasses import dataclass
from uuid import UUID

from advanced_trading_advisor.domain.persistence import FailureCategory, StoredAnalysisRun
from advanced_trading_advisor.domain.validation import identity, require

from .models import AnalysisRun
from .repositories import AnalysisRunRepository
from .result import AnalysisResult, AnalysisResultCodec


@dataclass(frozen=True, slots=True)
class AnalysisRunDetail:
    run: AnalysisRun
    result: AnalysisResult | None
    failure_category: FailureCategory | None


class AnalysisHistoryQuery:
    def __init__(self, runs: AnalysisRunRepository) -> None:
        self._runs = runs

    async def latest_completed(self) -> AnalysisRunDetail | None:
        stored = await self._runs.latest_completed()
        return self._detail(stored) if stored is not None else None

    async def recent(self, limit: int = 20) -> tuple[AnalysisRun, ...]:
        require(type(limit) is int and limit > 0, "Limit must be positive.")
        return tuple(stored.run for stored in await self._runs.list_all_recent(limit))

    async def detail(self, run_id: UUID) -> AnalysisRunDetail | None:
        identity(run_id)
        stored = await self._runs.get(run_id)
        return self._detail(stored) if stored is not None else None

    @staticmethod
    def _detail(stored: StoredAnalysisRun) -> AnalysisRunDetail:
        result = (
            AnalysisResultCodec.decode(stored.result_json)
            if stored.result_json is not None
            else None
        )
        if result is not None:
            require(
                result.run_id == stored.run.run_id and result.snapshot_id == stored.run.snapshot_id,
                "Stored analysis identity disagrees with its run.",
            )
        return AnalysisRunDetail(stored.run, result, stored.failure_category)
