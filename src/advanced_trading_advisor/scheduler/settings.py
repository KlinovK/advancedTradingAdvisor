from dataclasses import dataclass

from advanced_trading_advisor.configuration import local_environment
from advanced_trading_advisor.domain.validation import require


@dataclass(frozen=True, slots=True)
class SchedulerSettings:
    enabled: bool = False
    interval_hours: int = 4
    candle_delay_minutes: int = 10
    max_startup_catchup_age_minutes: int = 30

    def __post_init__(self):
        require(type(self.enabled) is bool, "Invalid SCHEDULER_ENABLED.")
        require(
            type(self.interval_hours) is int and self.interval_hours == 4,
            "Only a 4H scheduler interval is supported.",
        )
        require(
            type(self.candle_delay_minutes) is int and 1 <= self.candle_delay_minutes <= 59,
            "Scheduler candle delay must be 1–59 minutes.",
        )
        require(
            type(self.max_startup_catchup_age_minutes) is int
            and 0 <= self.max_startup_catchup_age_minutes <= 60,
            "Scheduler catch-up window must be 0–60 minutes.",
        )

    @classmethod
    def from_environment(cls, environment=None):
        env = environment if environment is not None else local_environment()
        enabled = env.get("SCHEDULER_ENABLED", "false").lower()
        require(enabled in {"true", "false"}, "SCHEDULER_ENABLED must be true or false.")
        numbers = []
        for key, default in [
            ("SCHEDULER_INTERVAL_HOURS", "4"),
            ("SCHEDULER_CANDLE_DELAY_MINUTES", "10"),
            ("SCHEDULER_MAX_STARTUP_CATCHUP_AGE_MINUTES", "30"),
        ]:
            raw = env.get(key, default)
            require(raw.isascii() and raw.isdigit() and len(raw) <= 3, f"Invalid {key}.")
            numbers.append(int(raw))
        return cls(enabled == "true", *numbers)
