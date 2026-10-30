"""Strict, offline financial row mapping contracts."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from test_financial_domain import buy, opening, sell

from advanced_trading_advisor.domain.validation import DomainValidationError
from advanced_trading_advisor.financial.repositories import FinancialPortfolio
from advanced_trading_advisor.storage.financial_mapper import (
    MAX_BIGINT,
    operation_from_rows,
    operation_to_rows,
    portfolio_from_row,
    portfolio_to_row,
)

PORTFOLIO_ID = UUID(int=123)


@pytest.mark.parametrize(
    "operation",
    [
        opening(1, "USDT", "1e59", "1e59"),
        opening(2, "BTC", "1e-24", None, incomplete="1e-24"),
        buy(3, "BTC", "1.000000000000000000000000", "2"),
        sell(4, "BTC", "1", "2"),
    ],
)
def test_operation_and_postings_round_trip(operation):
    row, postings = operation_to_rows(operation, PORTFOLIO_ID)
    assert type(row["quantity"]) is Decimal
    assert all(type(posting["delta"]) is Decimal for posting in postings)
    assert operation_from_rows(row, postings, PORTFOLIO_ID) == operation
    with pytest.raises(FrozenInstanceError):
        operation.facts.quantity = None


def test_portfolio_and_bigint_boundaries():
    portfolio = FinancialPortfolio(PORTFOLIO_ID, MAX_BIGINT)
    assert portfolio_from_row(portfolio_to_row(portfolio)) == portfolio
    for invalid_id, invalid_sequence in (
        (UUID(int=0), 0),
        (PORTFOLIO_ID, -1),
        (PORTFOLIO_ID, MAX_BIGINT + 1),
        (PORTFOLIO_ID, True),
    ):
        with pytest.raises(DomainValidationError):
            FinancialPortfolio(invalid_id, invalid_sequence)
    for invalid in (-1, MAX_BIGINT + 1, True):
        with pytest.raises(DomainValidationError):
            portfolio_from_row({"id": PORTFOLIO_ID, "last_sequence": invalid})
    with pytest.raises(DomainValidationError):
        portfolio_from_row({"id": UUID(int=0), "last_sequence": 0})
    operation = opening(1, "USDT", "1", "1")
    row, postings = operation_to_rows(operation, PORTFOLIO_ID)
    for invalid in (0, -1, MAX_BIGINT + 1, True):
        with pytest.raises(DomainValidationError):
            operation_from_rows({**row, "sequence": invalid}, postings, PORTFOLIO_ID)
    forged = replace(operation, sequence=type(operation.sequence)(MAX_BIGINT + 1))
    with pytest.raises(DomainValidationError):
        operation_to_rows(forged, PORTFOLIO_ID)


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", Decimal("9" * 61)),
        ("quantity", Decimal("1e-25")),
        ("quantity", Decimal("NaN")),
        ("quantity", Decimal("Infinity")),
        ("quantity", 1.0),
        ("operation_id", UUID(int=0)),
        ("operation_type", "transfer"),
        ("asset", "btc"),
        ("executed_at", datetime(2026, 1, 1)),
        ("executed_at", datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=3)))),
    ],
)
def test_corrupt_operation_fields_fail(field, value):
    row, postings = operation_to_rows(opening(1, "USDT", "1", "1"), PORTFOLIO_ID)
    with pytest.raises((DomainValidationError, TypeError, ValueError)):
        operation_from_rows({**row, field: value}, postings, PORTFOLIO_ID)


def test_missing_fields_wrong_portfolio_and_posting_order_fail():
    row, postings = operation_to_rows(buy(1, "BTC", "2", "3"), PORTFOLIO_ID)
    with pytest.raises(DomainValidationError):
        operation_from_rows(
            {key: value for key, value in row.items() if key != "asset"}, postings, PORTFOLIO_ID
        )
    with pytest.raises(DomainValidationError):
        operation_from_rows(row, postings, UUID(int=124))
    with pytest.raises(DomainValidationError):
        operation_from_rows(row, tuple(reversed(postings)), PORTFOLIO_ID)
    with pytest.raises(DomainValidationError):
        operation_from_rows(row, ({**postings[0], "role": "fee"}, postings[1]), PORTFOLIO_ID)
    with pytest.raises(DomainValidationError):
        operation_from_rows(
            row, ({**postings[0], "portfolio_id": UUID(int=124)}, postings[1]), PORTFOLIO_ID
        )
    with pytest.raises(DomainValidationError):
        operation_from_rows(
            row, ({**postings[0], "delta": Decimal("3")}, postings[1]), PORTFOLIO_ID
        )


def test_decimal_value_and_timestamp_preservation():
    operation = opening(1, "BTC", "9" * 36 + "." + "9" * 24, "0")
    row, postings = operation_to_rows(operation, PORTFOLIO_ID)
    assert row["quantity"] == operation.facts.quantity.value
    assert operation_from_rows(row, postings, PORTFOLIO_ID).facts.quantity.value == row["quantity"]
    assert row["executed_at"] == operation.executed_at.value
    assert row["recorded_at"] == operation.recorded_at.value
    with pytest.raises(DomainValidationError):
        operation_from_rows(
            {**row, "recorded_at": row["executed_at"] - timedelta(microseconds=1)},
            postings,
            PORTFOLIO_ID,
        )
