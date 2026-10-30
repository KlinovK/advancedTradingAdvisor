"""Strict financial ledger row mapping; no writes or transaction orchestration."""

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from advanced_trading_advisor.domain.validation import DomainValidationError, require
from advanced_trading_advisor.financial.ledger import (
    BalancePosting,
    BuyFacts,
    Completeness,
    LedgerOperation,
    OpeningFacts,
    OperationType,
    PostingRole,
    SellFacts,
)
from advanced_trading_advisor.financial.repositories import FinancialPortfolio
from advanced_trading_advisor.financial.values import (
    AssetId,
    AssetQuantity,
    LedgerSequence,
    OperationId,
    USDTAmount,
    UTCTimestamp,
    exact_decimal,
)

MAX_BIGINT = 9223372036854775807
PORTFOLIO_COLUMNS = frozenset({"id", "last_sequence"})
OPERATION_COLUMNS = frozenset(
    {
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
    }
)
POSTING_COLUMNS = frozenset(
    {
        "portfolio_id",
        "operation_id",
        "ordinal",
        "asset",
        "delta",
        "role",
    }
)


def _fields(row: Mapping, expected: frozenset[str]) -> None:
    require(hasattr(row, "keys") and set(row.keys()) == expected, "Invalid financial row fields.")


def _uuid(value: object) -> UUID:
    require(isinstance(value, UUID) and value.int != 0, "Invalid financial UUID.")
    return value


def _sequence(value: object, *, allow_zero: bool = False) -> int:
    require(
        type(value) is int and (0 if allow_zero else 1) <= value <= MAX_BIGINT,
        "Financial sequence exceeds PostgreSQL BIGINT bounds.",
    )
    return value


def _utc(value: object) -> datetime:
    require(
        type(value) is datetime and value.tzinfo is not None and value.utcoffset() == timedelta(0),
        "Financial timestamp must be UTC.",
    )
    return value


def _decimal(value: object) -> Decimal:
    return exact_decimal(value)


def _enum(kind, value):
    require(type(value) is str, "Invalid financial enum value.")
    try:
        return kind(value)
    except ValueError:
        raise DomainValidationError("Invalid financial enum value.") from None


def portfolio_from_row(row: Mapping) -> FinancialPortfolio:
    _fields(row, PORTFOLIO_COLUMNS)
    return FinancialPortfolio(_uuid(row["id"]), _sequence(row["last_sequence"], allow_zero=True))


def portfolio_to_row(portfolio: FinancialPortfolio) -> dict[str, object]:
    require(type(portfolio) is FinancialPortfolio, "Financial portfolio required.")
    return {
        "id": _uuid(portfolio.id),
        "last_sequence": _sequence(portfolio.last_sequence, allow_zero=True),
    }


def operation_from_rows(
    row: Mapping, postings: Sequence[Mapping], portfolio_id: UUID
) -> LedgerOperation:
    _fields(row, OPERATION_COLUMNS)
    _uuid(portfolio_id)
    require(
        row["portfolio_id"] == portfolio_id and isinstance(row["portfolio_id"], UUID),
        "Cross-portfolio operation.",
    )
    kind = _enum(OperationType, row["operation_type"])
    asset = AssetId(row["asset"])
    quantity = AssetQuantity(_decimal(row["quantity"]))
    cost = row["known_cost_usdt"]
    incomplete = row["incomplete_quantity"]
    principal = row["principal_usdt"]
    proceeds = row["proceeds_usdt"]
    if kind is OperationType.OPENING_BALANCE:
        require(principal is None and proceeds is None, "Invalid opening fields.")
        facts = OpeningFacts(
            asset,
            quantity,
            USDTAmount(_decimal(cost)) if cost is not None else None,
            AssetQuantity(_decimal(incomplete)),
            _enum(Completeness, row["completeness"]),
        )
    elif kind is OperationType.BUY:
        require(
            cost is None
            and incomplete is None
            and row["completeness"] is None
            and proceeds is None,
            "Invalid buy fields.",
        )
        facts = BuyFacts(asset, quantity, USDTAmount(_decimal(principal)))
    else:
        require(
            cost is None
            and incomplete is None
            and row["completeness"] is None
            and principal is None,
            "Invalid sell fields.",
        )
        facts = SellFacts(asset, quantity, USDTAmount(_decimal(proceeds)))
    require(
        isinstance(postings, Sequence) and not isinstance(postings, (str, bytes)),
        "Invalid financial postings.",
    )
    mapped = tuple(posting_from_row(item, portfolio_id, row["operation_id"]) for item in postings)
    require(
        tuple(p.ordinal for p in mapped) == tuple(sorted(p.ordinal for p in mapped)),
        "Postings are out of order.",
    )
    return LedgerOperation(
        OperationId(_uuid(row["operation_id"])),
        LedgerSequence(_sequence(row["sequence"])),
        UTCTimestamp(_utc(row["executed_at"])),
        UTCTimestamp(_utc(row["recorded_at"])),
        kind,
        facts,
        mapped,
    )


def posting_from_row(row: Mapping, portfolio_id: UUID, operation_id: UUID) -> BalancePosting:
    _fields(row, POSTING_COLUMNS)
    require(
        isinstance(row["portfolio_id"], UUID)
        and row["portfolio_id"] == _uuid(portfolio_id)
        and isinstance(row["operation_id"], UUID)
        and row["operation_id"] == _uuid(operation_id),
        "Cross-portfolio posting.",
    )
    return BalancePosting(
        row["ordinal"],
        AssetId(row["asset"]),
        _decimal(row["delta"]),
        _enum(PostingRole, row["role"]),
    )


def operation_to_rows(
    operation: LedgerOperation, portfolio_id: UUID
) -> tuple[dict, tuple[dict, ...]]:
    require(type(operation) is LedgerOperation, "Financial operation required.")
    _uuid(portfolio_id)
    _sequence(operation.sequence.value)
    _utc(operation.executed_at.value)
    _utc(operation.recorded_at.value)
    facts = operation.facts
    row = {
        "portfolio_id": portfolio_id,
        "operation_id": _uuid(operation.id.value),
        "sequence": operation.sequence.value,
        "operation_type": operation.type.value,
        "executed_at": operation.executed_at.value,
        "recorded_at": operation.recorded_at.value,
        "asset": facts.asset.value,
        "quantity": _decimal(facts.quantity.value),
        "known_cost_usdt": None,
        "incomplete_quantity": None,
        "completeness": None,
        "principal_usdt": None,
        "proceeds_usdt": None,
    }
    if operation.type is OperationType.OPENING_BALANCE and isinstance(facts, OpeningFacts):
        row.update(
            known_cost_usdt=_decimal(facts.known_cost_usdt.value)
            if facts.known_cost_usdt
            else None,
            incomplete_quantity=_decimal(facts.incomplete_quantity.value),
            completeness=facts.completeness.value,
        )
    elif operation.type is OperationType.BUY and isinstance(facts, BuyFacts):
        row["principal_usdt"] = _decimal(facts.principal_usdt.value)
    elif operation.type is OperationType.SELL and isinstance(facts, SellFacts):
        row["proceeds_usdt"] = _decimal(facts.proceeds_usdt.value)
    else:
        raise DomainValidationError("Operation type and facts disagree.")
    rows = tuple(
        {
            "portfolio_id": portfolio_id,
            "operation_id": operation.id.value,
            "ordinal": posting.ordinal,
            "asset": posting.asset.value,
            "delta": _decimal(posting.delta),
            "role": posting.role.value,
        }
        for posting in operation.postings
    )
    # Reconstruct to enforce all nested domain invariants even for forged dataclasses.
    operation_from_rows(row, rows, portfolio_id)
    return row, rows
