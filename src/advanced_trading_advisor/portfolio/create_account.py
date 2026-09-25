"""Explicit, revision-checked confirmation of a new portfolio account."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import ConflictError, CurrentPortfolio, UpdateType
from advanced_trading_advisor.domain.validation import require

from .confirmed_state import ConfirmedStateService
from .models import Account, AccountType, AssetPosition, Portfolio
from .repositories import SnapshotRepository


@dataclass(frozen=True, slots=True)
class CreateAccountCommand:
    expected_revision: int
    account_id: UUID
    name: str
    account_type: AccountType
    positions: tuple[AssetPosition, ...]

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )


class CreateAccount:
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

    async def execute(self, command: CreateAccountCommand) -> CurrentPortfolio:
        require(isinstance(command, CreateAccountCommand), "Create account command required.")
        current = await self._confirmed.load_current()
        require(current is not None, "No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise ConflictError("Current state changed; reload before confirming.")

        account = Account(command.account_id, command.name, command.account_type, command.positions)
        portfolio = Portfolio(current.snapshot.portfolio.accounts + (account,))
        snapshot = replace(
            current.snapshot,
            id=self._identity_factory(),
            confirmed_at=self._now(),
            portfolio=portfolio,
        )
        return await self._confirmed.confirm(
            snapshot,
            expected_revision=command.expected_revision,
            previous_snapshot_id=current.snapshot.id,
            update_type=UpdateType.ACCOUNT_CREATED,
        )
