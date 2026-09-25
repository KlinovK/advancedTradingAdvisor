"""Explicit, revision-checked confirmation of an account label change."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import ConflictError, CurrentPortfolio, UpdateType
from advanced_trading_advisor.domain.validation import identity, require

from .confirmed_state import ConfirmedStateService
from .models import Portfolio
from .repositories import SnapshotRepository


@dataclass(frozen=True, slots=True)
class RenameAccountCommand:
    expected_revision: int
    account_id: UUID
    name: str

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )
        identity(self.account_id)


class RenameAccount:
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

    async def execute(self, command: RenameAccountCommand) -> CurrentPortfolio:
        require(isinstance(command, RenameAccountCommand), "Rename account command required.")
        current = await self._confirmed.load_current()
        require(current is not None, "No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise ConflictError("Current state changed; reload before confirming.")

        accounts = current.snapshot.portfolio.accounts
        index = next(
            (i for i, account in enumerate(accounts) if account.id == command.account_id), None
        )
        require(index is not None, "Account does not exist in the confirmed portfolio.")
        renamed = replace(accounts[index], name=command.name)
        require(renamed.name != accounts[index].name, "Account name is unchanged.")
        portfolio = Portfolio(accounts[:index] + (renamed,) + accounts[index + 1 :])
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
            update_type=UpdateType.ACCOUNT_RENAMED,
        )
