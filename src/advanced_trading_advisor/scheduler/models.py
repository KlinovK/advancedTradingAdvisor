from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from advanced_trading_advisor.domain.persistence import FailureCategory
from advanced_trading_advisor.domain.validation import identity, require

from .policy import utc


class ScheduledStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class ScheduledExecution:
    scheduled_for: datetime
    expected_cutoff: datetime
    owner_token: UUID
    started_at: datetime
    finished_at: datetime | None = None
    status: ScheduledStatus = ScheduledStatus.RUNNING
    analysis_run_id: UUID | None = None
    failure_category: FailureCategory | None = None

    def __post_init__(self):
        for at in (self.scheduled_for, self.expected_cutoff, self.started_at):
            utc(at)
        identity(self.owner_token)
        require(self.started_at >= self.scheduled_for, "Scheduled execution cannot start early.")
        require(
            self.expected_cutoff.hour % 4 == 0
            and self.expected_cutoff.minute == 0
            and self.expected_cutoff.second == self.expected_cutoff.microsecond == 0,
            "Invalid expected 4H cutoff.",
        )
        delay = self.scheduled_for - self.expected_cutoff
        require(
            timedelta(minutes=1) <= delay <= timedelta(minutes=59)
            and self.scheduled_for.second == self.scheduled_for.microsecond == 0,
            "Invalid candle delay.",
        )
        require(isinstance(self.status, ScheduledStatus), "Invalid scheduler status.")
        if self.status is ScheduledStatus.RUNNING:
            require(
                self.finished_at is None and self.failure_category is None,
                "Running slot cannot have a terminal outcome.",
            )
        else:
            utc(self.finished_at)
            require(self.finished_at >= self.started_at, "Finish precedes start.")
        if self.analysis_run_id is not None:
            identity(self.analysis_run_id)
            require(self.analysis_run_id == self.owner_token, "Analysis owner mismatch.")
        if self.status is ScheduledStatus.COMPLETED:
            require(
                self.analysis_run_id is not None and self.failure_category is None,
                "Completed slot requires an analysis.",
            )
        if self.status in {ScheduledStatus.FAILED, ScheduledStatus.SKIPPED}:
            require(
                isinstance(self.failure_category, FailureCategory), "Sanitized failure required."
            )
