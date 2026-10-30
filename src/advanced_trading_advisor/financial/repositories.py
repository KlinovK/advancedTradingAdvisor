"""Financial ledger persistence boundary; no database driver in the domain."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from advanced_trading_advisor.domain.validation import identity, require

from .ledger import BuyFacts, LedgerOperation, OpeningFacts, SellFacts
from .values import OperationId, UTCTimestamp

MAX_PERSISTED_SEQUENCE = 9223372036854775807


@dataclass(frozen=True, slots=True)
class FinancialPortfolio:
    id: UUID
    last_sequence: int

    def __post_init__(self) -> None:
        identity(self.id)
        require(
            type(self.last_sequence) is int and 0 <= self.last_sequence <= MAX_PERSISTED_SEQUENCE,
            "Invalid persisted financial sequence.",
        )


class FinancialLedgerReader(Protocol):
    async def load_portfolio(self, portfolio_id: UUID) -> FinancialPortfolio | None: ...

    async def load_operations(self, portfolio_id: UUID) -> tuple[LedgerOperation, ...]: ...


class FinancialLedgerRepository(FinancialLedgerReader, Protocol):
    async def create_portfolio(self, portfolio_id: UUID) -> FinancialPortfolio: ...

    async def append(
        self,
        portfolio_id: UUID,
        operation_id: OperationId,
        executed_at: UTCTimestamp,
        facts: OpeningFacts | BuyFacts | SellFacts,
    ) -> LedgerOperation:
        """Atomically lock, deduplicate, replay, insert facts and advance sequence.

        Missing portfolios raise ResourceNotFoundError. Reusing an operation ID
        with different client facts raises ConflictError. Invalid new facts
        raise DomainValidationError; an invalid resulting history raises
        StateConflictError. Corrupt persisted rows or history raise PayloadError.
        Database failures raise PersistenceError. An equivalent caller retry
        returns the original immutable operation without changing storage.
        """
        ...
