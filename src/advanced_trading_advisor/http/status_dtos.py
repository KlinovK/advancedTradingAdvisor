"""Public, non-diagnostic operational status responses."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class HealthDTO:
    status: Literal["ok"]


@dataclass(frozen=True, slots=True)
class ReadinessDTO:
    status: Literal["ready", "not_ready"]
