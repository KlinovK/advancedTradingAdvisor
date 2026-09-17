from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.validation import identity, require, timestamp


class AnalysisRunStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AnalysisRun:
    run_id: UUID
    snapshot_id: UUID
    started_at: datetime
    status: AnalysisRunStatus = AnalysisRunStatus.RUNNING
    completed_at: datetime | None = None
    market_cutoff: datetime | None = None

    def __post_init__(self):
        identity(self.run_id)
        identity(self.snapshot_id)
        require(self.run_id != self.snapshot_id, "Run identity must differ from snapshot identity.")
        timestamp(self.started_at)
        require(isinstance(self.status, AnalysisRunStatus), "Invalid run status.")
        if self.status is AnalysisRunStatus.RUNNING:
            require(self.completed_at is None, "Running analysis cannot have completion time.")
        else:
            timestamp(self.completed_at)
            require(self.completed_at >= self.started_at, "Completion precedes start.")
        if self.market_cutoff is not None:
            timestamp(self.market_cutoff)
            # A new candle can close while a live fetch is in flight.
            horizon = self.completed_at if self.completed_at is not None else self.started_at
            require(self.market_cutoff <= horizon, "Market cutoff follows available run horizon.")

    @classmethod
    def start(cls, snapshot_id: UUID, started_at: datetime) -> "AnalysisRun":
        return cls(run_id=uuid4(), snapshot_id=snapshot_id, started_at=started_at)
