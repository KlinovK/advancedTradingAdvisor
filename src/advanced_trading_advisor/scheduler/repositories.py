from datetime import datetime
from typing import Protocol

from advanced_trading_advisor.domain.persistence import FailureCategory

from .models import ScheduledExecution, ScheduledStatus


class SchedulerRepository(Protocol):
    async def recover(self, now: datetime) -> None: ...
    async def claim(self, record: ScheduledExecution) -> bool: ...
    async def finish(
        self,
        record: ScheduledExecution,
        now: datetime,
        status: ScheduledStatus,
        category: FailureCategory | None = None,
    ) -> ScheduledExecution: ...
    async def latest(self) -> ScheduledExecution | None: ...
    async def get(self, scheduled_for: datetime) -> ScheduledExecution | None: ...
