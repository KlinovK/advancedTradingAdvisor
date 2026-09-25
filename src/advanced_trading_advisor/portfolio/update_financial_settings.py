"""Explicit, revision-checked update of confirmed financial settings."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import ConflictError, CurrentPortfolio, UpdateType
from advanced_trading_advisor.domain.validation import require

from .confirmed_state import ConfirmedStateService
from .models import FinancialSettings
from .repositories import SnapshotRepository


@dataclass(frozen=True, slots=True)
class UpdateFinancialSettingsCommand:
    expected_revision: int
    monthly_expenses_usd: Decimal
    target_expense_runway_months: Decimal

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )


class UpdateFinancialSettings:
    def __init__(
        self,
        snapshots: SnapshotRepository,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        identity_factory: Callable[[], UUID] = uuid4,
    ) -> None:
        self._confirmed = ConfirmedStateService(snapshots)
        self._now = now
        self._identity_factory = identity_factory

    async def execute(self, command: UpdateFinancialSettingsCommand) -> CurrentPortfolio:
        require(
            isinstance(command, UpdateFinancialSettingsCommand),
            "Update financial settings command required.",
        )
        current = await self._confirmed.load_current()
        require(current is not None, "No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise ConflictError("Current state changed; reload before confirming.")

        settings = FinancialSettings(
            command.monthly_expenses_usd,
            command.target_expense_runway_months,
        )
        require(
            settings != current.snapshot.financial_settings, "Financial settings are unchanged."
        )
        snapshot = replace(
            current.snapshot,
            id=self._identity_factory(),
            confirmed_at=self._now(),
            financial_settings=settings,
        )
        return await self._confirmed.confirm(
            snapshot,
            expected_revision=command.expected_revision,
            previous_snapshot_id=current.snapshot.id,
            update_type=UpdateType.EXPENSE_SETTINGS_CHANGE,
        )
