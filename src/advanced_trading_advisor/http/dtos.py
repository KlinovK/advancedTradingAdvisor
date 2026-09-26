"""Explicit, exact-value HTTP projection of confirmed portfolio state."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

from advanced_trading_advisor.domain.persistence import CurrentPortfolio
from advanced_trading_advisor.portfolio.models import (
    AccountType,
    AssetPosition,
    LimitOrderStatus,
    TradingSide,
)

from .schema import DecimalText, UTCText, UUIDText, enum_text


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class PositionDTO:
    symbol: str
    amount: DecimalText

    @classmethod
    def from_domain(cls, position: AssetPosition) -> "PositionDTO":
        return cls(position.symbol, decimal_text(position.amount))


@dataclass(frozen=True, slots=True)
class AccountDTO:
    id: UUIDText
    name: str
    type: Annotated[str, enum_text(AccountType)]
    positions: tuple[PositionDTO, ...]


@dataclass(frozen=True, slots=True)
class FinancialSettingsDTO:
    monthly_expenses_usd: DecimalText
    target_expense_runway_months: DecimalText


@dataclass(frozen=True, slots=True)
class CorePositionDTO:
    symbol: str
    hard_floor: DecimalText
    preferred_quantity: DecimalText
    policy_version: int


@dataclass(frozen=True, slots=True)
class LimitOrderDTO:
    id: UUIDText
    account_id: UUIDText
    asset: str
    side: Annotated[str, enum_text(TradingSide)]
    target_price: DecimalText
    quantity_asset: DecimalText
    status: Annotated[str, enum_text(LimitOrderStatus)]
    created_at: UTCText
    updated_at: UTCText
    resolved_at: UTCText | None


@dataclass(frozen=True, slots=True)
class CurrentPortfolioDTO:
    revision: int
    snapshot_id: UUIDText
    confirmed_at: UTCText
    accounts: tuple[AccountDTO, ...]
    aggregate_positions: tuple[PositionDTO, ...]
    financial_settings: FinancialSettingsDTO
    core_positions: tuple[CorePositionDTO, ...]
    limit_orders: tuple[LimitOrderDTO, ...]

    @classmethod
    def from_domain(cls, current: CurrentPortfolio) -> "CurrentPortfolioDTO":
        snapshot = current.snapshot
        settings = snapshot.financial_settings
        return cls(
            revision=current.revision,
            snapshot_id=str(snapshot.id),
            confirmed_at=utc_timestamp(snapshot.confirmed_at),
            accounts=tuple(
                AccountDTO(
                    id=str(account.id),
                    name=account.name,
                    type=account.type.value,
                    positions=tuple(PositionDTO.from_domain(p) for p in account.positions),
                )
                for account in snapshot.portfolio.accounts
            ),
            aggregate_positions=tuple(
                PositionDTO.from_domain(position) for position in snapshot.portfolio.positions
            ),
            financial_settings=FinancialSettingsDTO(
                monthly_expenses_usd=decimal_text(settings.monthly_expenses_usd),
                target_expense_runway_months=decimal_text(settings.target_expense_runway_months),
            ),
            core_positions=tuple(
                CorePositionDTO(
                    symbol=core.symbol,
                    hard_floor=decimal_text(core.hard_floor),
                    preferred_quantity=decimal_text(core.preferred_quantity),
                    policy_version=core.policy_version,
                )
                for core in snapshot.core_positions
            ),
            limit_orders=tuple(
                LimitOrderDTO(
                    id=str(order.id),
                    account_id=str(order.account_id),
                    asset=order.asset,
                    side=order.side.value,
                    target_price=decimal_text(order.target_price),
                    quantity_asset=decimal_text(order.quantity_asset),
                    status=order.status.value,
                    created_at=utc_timestamp(order.created_at),
                    updated_at=utc_timestamp(order.updated_at),
                    resolved_at=utc_timestamp(order.resolved_at)
                    if order.resolved_at is not None
                    else None,
                )
                for order in snapshot.orders
            ),
        )
