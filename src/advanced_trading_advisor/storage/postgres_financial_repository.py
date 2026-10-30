"""Atomic PostgreSQL persistence for the immutable financial ledger."""

from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    PayloadError,
    StateConflictError,
)
from advanced_trading_advisor.domain.validation import (
    DomainValidationError,
    ResourceNotFoundError,
    identity,
    require,
)
from advanced_trading_advisor.financial.ledger import (
    BalancePosting,
    BuyFacts,
    LedgerOperation,
    OpeningFacts,
    OperationType,
    PostingRole,
    SellFacts,
)
from advanced_trading_advisor.financial.replay import replay
from advanced_trading_advisor.financial.repositories import (
    MAX_PERSISTED_SEQUENCE,
    FinancialPortfolio,
)
from advanced_trading_advisor.financial.values import (
    USDT,
    LedgerSequence,
    OperationId,
    UTCTimestamp,
)

from .financial_mapper import operation_from_rows, operation_to_rows, portfolio_from_row
from .postgres_database import PostgresDatabase


def _requested_type(facts: OpeningFacts | BuyFacts | SellFacts) -> OperationType:
    if type(facts) is OpeningFacts:
        return OperationType.OPENING_BALANCE
    if type(facts) is BuyFacts:
        return OperationType.BUY
    if type(facts) is SellFacts:
        return OperationType.SELL
    raise DomainValidationError("Financial operation facts required.")


def _postings(facts: OpeningFacts | BuyFacts | SellFacts) -> tuple[BalancePosting, ...]:
    if type(facts) is OpeningFacts:
        return (BalancePosting(1, facts.asset, facts.quantity.value, PostingRole.OPENING),)
    if type(facts) is BuyFacts:
        return (
            BalancePosting(1, facts.asset, facts.quantity.value, PostingRole.TRADED_ASSET),
            BalancePosting(2, USDT, -facts.principal_usdt.value, PostingRole.SETTLEMENT),
        )
    return (
        BalancePosting(1, facts.asset, -facts.quantity.value, PostingRole.TRADED_ASSET),
        BalancePosting(2, USDT, facts.proceeds_usdt.value, PostingRole.SETTLEMENT),
    )


def _mapped_operation(row, postings: Sequence, portfolio_id: UUID) -> LedgerOperation:
    try:
        return operation_from_rows(row, postings, portfolio_id)
    except (DomainValidationError, TypeError, AttributeError, ArithmeticError):
        raise PayloadError("Corrupt persisted financial operation.") from None


def _validate_request(
    portfolio_id: UUID,
    operation_id: OperationId,
    executed_at: UTCTimestamp,
    kind: OperationType,
    facts: OpeningFacts | BuyFacts | SellFacts,
) -> None:
    """Reconstruct all client facts before an equivalent retry may return."""
    try:
        probe = LedgerOperation(
            operation_id,
            LedgerSequence(1),
            executed_at,
            executed_at,
            kind,
            facts,
            _postings(facts),
        )
        operation_to_rows(probe, portfolio_id)
    except (TypeError, AttributeError, ArithmeticError):
        raise DomainValidationError("Invalid financial operation facts.") from None


def _next_sequence(portfolio: FinancialPortfolio) -> LedgerSequence:
    """Check the BIGINT boundary without constructing an out-of-range integer."""
    if portfolio.last_sequence == MAX_PERSISTED_SEQUENCE:
        raise StateConflictError("Financial sequence is exhausted.")
    return LedgerSequence(portfolio.last_sequence + 1)


async def _load_history(connection, portfolio: FinancialPortfolio) -> tuple[LedgerOperation, ...]:
    rows = await connection.fetch(
        "SELECT * FROM financial_operations WHERE portfolio_id=$1 ORDER BY executed_at, sequence",
        portfolio.id,
    )
    posting_rows = await connection.fetch(
        "SELECT * FROM financial_postings WHERE portfolio_id=$1 ORDER BY operation_id, ordinal",
        portfolio.id,
    )
    grouped: dict[UUID, list] = {}
    for posting in posting_rows:
        grouped.setdefault(posting["operation_id"], []).append(posting)
    operations = tuple(
        _mapped_operation(row, grouped.pop(row["operation_id"], []), portfolio.id) for row in rows
    )
    if grouped or len(operations) != portfolio.last_sequence:
        raise PayloadError("Financial ledger sequence or posting count is inconsistent.")
    for expected, operation in enumerate(
        sorted(operations, key=lambda item: item.sequence.value), 1
    ):
        if operation.sequence.value != expected:
            raise PayloadError("Financial ledger sequence is not contiguous.")
    try:
        replay(operations)
    except (DomainValidationError, ArithmeticError):
        raise PayloadError("Persisted financial history is invalid.") from None
    return operations


class PostgresFinancialLedgerRepository:
    """One portfolio row lock serializes all appends for that portfolio."""

    def __init__(self, database: PostgresDatabase):
        self.database = database

    async def create_portfolio(self, portfolio_id: UUID) -> FinancialPortfolio:
        identity(portfolio_id)
        async with self.database.transaction() as connection:
            await self.database.validate_financial_schema(connection)
            inserted = await connection.fetchrow(
                "INSERT INTO financial_portfolios(id, last_sequence) VALUES ($1, 0) "
                "ON CONFLICT (id) DO NOTHING RETURNING id, last_sequence",
                portfolio_id,
            )
            if inserted is None:
                raise ConflictError("Financial portfolio already exists.")
            return portfolio_from_row(inserted)

    async def load_portfolio(self, portfolio_id: UUID) -> FinancialPortfolio | None:
        identity(portfolio_id)
        async with self.database.connection() as connection:
            async with connection.transaction(readonly=True):
                await self.database.validate_financial_schema(connection)
                row = await connection.fetchrow(
                    "SELECT id, last_sequence FROM financial_portfolios WHERE id=$1", portfolio_id
                )
                if row is None:
                    return None
                try:
                    return portfolio_from_row(row)
                except DomainValidationError:
                    raise PayloadError("Corrupt persisted financial portfolio.") from None

    async def load_operations(self, portfolio_id: UUID) -> tuple[LedgerOperation, ...]:
        identity(portfolio_id)
        async with self.database.connection() as connection:
            async with connection.transaction(isolation="repeatable_read", readonly=True):
                await self.database.validate_financial_schema(connection)
                row = await connection.fetchrow(
                    "SELECT id, last_sequence FROM financial_portfolios WHERE id=$1", portfolio_id
                )
                if row is None:
                    raise ResourceNotFoundError("Financial portfolio not found.")
                try:
                    portfolio = portfolio_from_row(row)
                except DomainValidationError:
                    raise PayloadError("Corrupt persisted financial portfolio.") from None
                return await _load_history(connection, portfolio)

    async def append(
        self,
        portfolio_id: UUID,
        operation_id: OperationId,
        executed_at: UTCTimestamp,
        facts: OpeningFacts | BuyFacts | SellFacts,
    ) -> LedgerOperation:
        identity(portfolio_id)
        require(type(operation_id) is OperationId, "Operation ID required.")
        OperationId(operation_id.value)
        require(type(executed_at) is UTCTimestamp, "Execution time required.")
        UTCTimestamp(executed_at.value)
        kind = _requested_type(facts)
        _validate_request(portfolio_id, operation_id, executed_at, kind, facts)

        async with self.database.transaction() as connection:
            await self.database.validate_financial_schema(connection)
            row = await connection.fetchrow(
                "SELECT id, last_sequence FROM financial_portfolios WHERE id=$1 FOR UPDATE",
                portfolio_id,
            )
            if row is None:
                raise ResourceNotFoundError("Financial portfolio not found.")
            try:
                portfolio = portfolio_from_row(row)
            except DomainValidationError:
                raise PayloadError("Corrupt persisted financial portfolio.") from None

            # A retry must not succeed while any persisted ledger state is corrupt.
            # Full replay under the row lock is intentional, including for retries.
            history = await _load_history(connection, portfolio)
            existing = next((item for item in history if item.id == operation_id), None)
            if existing is not None:
                if (existing.type, existing.executed_at, existing.facts) != (
                    kind,
                    executed_at,
                    facts,
                ):
                    raise ConflictError("Operation ID already has different financial facts.")
                return existing

            candidate = LedgerOperation(
                operation_id,
                _next_sequence(portfolio),
                executed_at,
                UTCTimestamp(datetime.now(UTC)),
                kind,
                facts,
                _postings(facts),
            )
            operation_row, posting_rows = operation_to_rows(candidate, portfolio_id)
            try:
                replay((*history, candidate))
            except (DomainValidationError, ArithmeticError):
                raise StateConflictError(
                    "Financial operation invalidates historical state."
                ) from None

            await connection.execute(
                """INSERT INTO financial_operations
                (portfolio_id, operation_id, sequence, operation_type, executed_at, recorded_at,
                 asset, quantity, known_cost_usdt, incomplete_quantity, completeness,
                 principal_usdt, proceeds_usdt)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)""",
                *(
                    operation_row[key]
                    for key in (
                        "portfolio_id",
                        "operation_id",
                        "sequence",
                        "operation_type",
                        "executed_at",
                        "recorded_at",
                        "asset",
                        "quantity",
                        "known_cost_usdt",
                        "incomplete_quantity",
                        "completeness",
                        "principal_usdt",
                        "proceeds_usdt",
                    )
                ),
            )
            for posting in posting_rows:
                await connection.execute(
                    """INSERT INTO financial_postings
                    (portfolio_id, operation_id, ordinal, asset, delta, role)
                    VALUES ($1,$2,$3,$4,$5,$6)""",
                    *(
                        posting[key]
                        for key in (
                            "portfolio_id",
                            "operation_id",
                            "ordinal",
                            "asset",
                            "delta",
                            "role",
                        )
                    ),
                )
            await connection.execute(
                "UPDATE financial_portfolios SET last_sequence=$2 WHERE id=$1",
                portfolio_id,
                candidate.sequence.value,
            )
            return candidate
