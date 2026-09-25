"""Explicit, revision-checked logical removal of an unreferenced account."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.persistence import (
    CurrentPortfolio,
    RevisionConflictError,
    StateConflictError,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import ResourceNotFoundError, identity, require

from .confirmed_state import ConfirmedStateService
from .models import Portfolio
from .repositories import SnapshotRepository


@dataclass(frozen=True, slots=True)
class RemoveAccountCommand:
    expected_revision: int
    account_id: UUID

    def __post_init__(self) -> None:
        require(
            type(self.expected_revision) is int and self.expected_revision > 0,
            "Invalid expected revision.",
        )
        identity(self.account_id)


class RemoveAccount:
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

    async def execute(self, command: RemoveAccountCommand) -> CurrentPortfolio:
        require(isinstance(command, RemoveAccountCommand), "Remove account command required.")
        current = await self._confirmed.load_current()
        if current is None:
            raise ResourceNotFoundError("No confirmed portfolio; run initial setup first.")
        if command.expected_revision != current.revision:
            raise RevisionConflictError("Current state changed; reload before confirming.")

        accounts = current.snapshot.portfolio.accounts
        index = next(
            (i for i, account in enumerate(accounts) if account.id == command.account_id), None
        )
        if index is None:
            raise ResourceNotFoundError("Account does not exist in the confirmed portfolio.")
        if len(accounts) <= 1:
            raise StateConflictError("Cannot remove the last account.")
        if any(order.account_id == command.account_id for order in current.snapshot.orders):
            raise StateConflictError("Account has referenced orders and cannot be removed.")

        portfolio = Portfolio(accounts[:index] + accounts[index + 1 :])
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
            update_type=UpdateType.ACCOUNT_REMOVED,
        )
