"""Explicit, revision-checked upsert of one global core-position policy."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import ConflictError, CurrentPortfolio, UpdateType
from advanced_trading_advisor.domain.validation import require

from .confirmed_state import ConfirmedStateService
from .models import CorePosition
from .repositories import SnapshotRepository


@dataclass(frozen=True, slots=True)
class UpdateCorePositionCommand:
    expected_revision: int
    symbol: str
    hard_floor: Decimal
    preferred_quantity: Decimal

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )


class UpdateCorePosition:
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

    async def execute(self, command: UpdateCorePositionCommand) -> CurrentPortfolio:
        require(isinstance(command, UpdateCorePositionCommand), "Update core command required.")
        current = await self._confirmed.load_current()
        require(current is not None, "No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise ConflictError("Current state changed; reload before confirming.")

        configured = CorePosition(
            command.symbol,
            command.hard_floor,
            command.preferred_quantity,
        )
        require(
            configured.symbol in {"BTC", "ETH", "SOL"}, "Core must be a supported crypto asset."
        )
        cores = tuple(
            core for core in current.snapshot.core_positions if core.symbol != configured.symbol
        ) + (configured,)
        require(cores != current.snapshot.core_positions, "Core configuration is unchanged.")
        snapshot = replace(
            current.snapshot,
            id=self._identity_factory(),
            confirmed_at=self._now(),
            core_positions=cores,
        )
        return await self._confirmed.confirm(
            snapshot,
            expected_revision=command.expected_revision,
            previous_snapshot_id=current.snapshot.id,
            update_type=UpdateType.CORE_CONFIGURATION_CHANGE,
        )
