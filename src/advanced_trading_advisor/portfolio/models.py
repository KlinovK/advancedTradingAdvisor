from dataclasses import dataclass
from datetime import datetime
from decimal import MAX_EMAX, MIN_EMIN, Decimal, Rounded, localcontext
from enum import StrEnum
from uuid import UUID

from advanced_trading_advisor.domain.numeric import finite_decimal
from advanced_trading_advisor.domain.validation import identity, items, require, symbol, timestamp


@dataclass(frozen=True, slots=True)
class AssetPosition:
    symbol: str
    amount: Decimal

    def __post_init__(self):
        symbol(self.symbol)
        finite_decimal(self.amount, positive=True)


class AccountType(StrEnum):
    BINANCE = "binance"
    EXTERNAL_WALLET = "external_wallet"
    OTHER_EXCHANGE = "other_exchange"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Account:
    id: UUID
    name: str
    type: AccountType
    positions: tuple[AssetPosition, ...]

    def __post_init__(self):
        identity(self.id)
        require(isinstance(self.name, str) and bool(self.name.strip()), "Account name is required.")
        require(isinstance(self.type, AccountType), "Invalid account type.")
        items(self.positions, AssetPosition)
        require(
            len({position.symbol for position in self.positions}) == len(self.positions),
            "Position symbols must be unique within an account.",
        )


@dataclass(frozen=True, slots=True)
class Portfolio:
    accounts: tuple[Account, ...]

    def __post_init__(self):
        # Temporary bridge for aggregate-only CLI and snapshot codecs. Remove when they
        # construct account-backed portfolios; no aggregate is stored alongside accounts.
        if (
            isinstance(self.accounts, tuple)
            and self.accounts
            and all(isinstance(value, AssetPosition) for value in self.accounts)
        ):
            object.__setattr__(
                self,
                "accounts",
                (
                    Account(
                        UUID("00000000-0000-0000-0000-000000000001"),
                        "Manually entered aggregate",
                        AccountType.MANUAL,
                        tuple(sorted(self.accounts, key=lambda position: position.symbol)),
                    ),
                ),
            )
        items(self.accounts, Account)
        require(
            len({account.id for account in self.accounts}) == len(self.accounts),
            "Account IDs must be unique within a portfolio.",
        )

    @property
    def positions(self) -> tuple[AssetPosition, ...]:
        by_symbol: dict[str, list[Decimal]] = {}
        for account in self.accounts:
            for position in account.positions:
                by_symbol.setdefault(position.symbol, []).append(position.amount)

        positions = []
        for asset_symbol, amounts in sorted(by_symbol.items()):
            # Decimal addition is context-sensitive. This precision covers every input
            # digit plus a carry from summing the values, independent of caller context.
            minimum_exponent = min(amount.as_tuple().exponent for amount in amounts)
            maximum_adjusted = max(amount.adjusted() for amount in amounts)
            required_precision = maximum_adjusted - minimum_exponent + len(str(len(amounts))) + 1
            with localcontext() as context:
                context.prec = max(context.prec, required_precision)
                context.Emax = MAX_EMAX
                context.Emin = MIN_EMIN
                context.traps[Rounded] = True
                total = sum(amounts, Decimal(0))
            positions.append(AssetPosition(asset_symbol, total))
        return tuple(positions)


@dataclass(frozen=True, slots=True)
class StablecoinClassification:
    symbols: frozenset[str] = frozenset({"USDT", "USDC"})

    def __post_init__(self):
        require(isinstance(self.symbols, frozenset), "Stable symbols must be a frozenset.")
        for value in self.symbols:
            symbol(value)

    def is_stable(self, asset: str) -> bool:
        symbol(asset)
        return asset in self.symbols


class TradingSide(StrEnum):
    BUY = "buy"
    SELL = "sell"


class LimitOrderStatus(StrEnum):
    OPEN = "open"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class LimitOrder:
    id: UUID
    asset: str
    side: TradingSide
    target_price: Decimal
    quantity_asset: Decimal
    status: LimitOrderStatus
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None

    def __post_init__(self):
        identity(self.id)
        symbol(self.asset)
        require(isinstance(self.side, TradingSide), "Invalid trading side.")
        require(isinstance(self.status, LimitOrderStatus), "Invalid order status.")
        finite_decimal(self.target_price, positive=True)
        finite_decimal(self.quantity_asset, positive=True)
        timestamp(self.created_at)
        timestamp(self.updated_at)
        require(self.updated_at >= self.created_at, "Update precedes creation.")
        if self.status is LimitOrderStatus.OPEN:
            require(self.resolved_at is None, "Open order cannot be resolved.")
        else:
            timestamp(self.resolved_at)
            require(
                self.created_at <= self.resolved_at <= self.updated_at,
                "Resolution must be between creation and update.",
            )


@dataclass(frozen=True, slots=True)
class FinancialSettings:
    monthly_expenses_usd: Decimal
    target_expense_runway_months: Decimal = Decimal("12")

    def __post_init__(self):
        finite_decimal(self.monthly_expenses_usd, positive=True)
        finite_decimal(self.target_expense_runway_months, positive=True)


@dataclass(frozen=True, slots=True)
class CorePosition:
    """Explicit confirmed tactical sell floor, not an instruction to sell excess units."""

    symbol: str
    hard_floor: Decimal
    preferred_quantity: Decimal
    policy_version: int = 1

    def __post_init__(self):
        symbol(self.symbol)
        require(self.symbol not in {"USDT", "USDC"}, "Stablecoins cannot have a core floor.")
        finite_decimal(self.hard_floor, positive=True)
        finite_decimal(self.preferred_quantity, positive=True)
        require(self.preferred_quantity >= self.hard_floor, "Preferred core is below floor.")
        require(
            type(self.policy_version) is int and self.policy_version == 1, "Invalid core version."
        )


@dataclass(frozen=True, slots=True)
class ConfirmedPortfolioSnapshot:
    id: UUID
    confirmed_at: datetime
    portfolio: Portfolio
    financial_settings: FinancialSettings
    orders: tuple[LimitOrder, ...] = ()
    core_positions: tuple[CorePosition, ...] = ()

    def __post_init__(self):
        identity(self.id)
        timestamp(self.confirmed_at)
        require(isinstance(self.portfolio, Portfolio), "Invalid portfolio.")
        require(
            isinstance(self.financial_settings, FinancialSettings), "Invalid financial settings."
        )
        items(self.orders, LimitOrder)
        items(self.core_positions, CorePosition)
        require(
            len({c.symbol for c in self.core_positions}) == len(self.core_positions),
            "Core symbols must be unique.",
        )
        holdings = {p.symbol: p.amount for p in self.portfolio.positions}
        require(
            all(c.hard_floor <= holdings.get(c.symbol, Decimal(0)) for c in self.core_positions),
            "Confirmed core floor exceeds current holdings.",
        )
        require(len({o.id for o in self.orders}) == len(self.orders), "Order IDs must be unique.")
        require(
            all(o.updated_at <= self.confirmed_at for o in self.orders),
            "Order update cannot follow state confirmation.",
        )
