from dataclasses import dataclass
from datetime import datetime, timedelta

from advanced_trading_advisor.domain.validation import require, timestamp

from .settings import SchedulerSettings


def utc(value):
    timestamp(value)
    require(value.utcoffset() == timedelta(0), "Scheduler requires UTC timestamps.")


@dataclass(frozen=True, slots=True)
class ScheduleWindow:
    boundary: datetime
    previous_run: datetime
    next_run: datetime


class SchedulerPolicy:
    def __init__(self, settings: SchedulerSettings):
        self.settings = settings
        self.delay = timedelta(minutes=settings.candle_delay_minutes)

    def window(self, now):
        utc(now)
        boundary = now.replace(hour=now.hour // 4 * 4, minute=0, second=0, microsecond=0)
        previous = boundary + self.delay
        if previous > now:
            previous -= timedelta(hours=4)
        return ScheduleWindow(previous - self.delay, previous, previous + timedelta(hours=4))

    def expected_cutoff(self, scheduled_for):
        utc(scheduled_for)
        cutoff = scheduled_for - self.delay
        require(
            cutoff.hour % 4 == 0 and cutoff.minute == cutoff.second == cutoff.microsecond == 0,
            "Invalid aligned scheduled slot.",
        )
        return cutoff

    def catchup(self, now):
        previous = self.window(now).previous_run
        return (
            previous
            if now - previous <= timedelta(minutes=self.settings.max_startup_catchup_age_minutes)
            else None
        )
