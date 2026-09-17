from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from advanced_trading_advisor.domain.numeric import calculation_context, finite_decimal
from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    CurrentPortfolio,
    UpdateType,
)
from advanced_trading_advisor.domain.validation import require

from .confirmed_state import ConfirmedStateService
from .models import (
    AssetPosition,
    ConfirmedPortfolioSnapshot,
    LimitOrderStatus,
    Portfolio,
    TradingSide,
)
from .repositories import SnapshotRepository

TRADED_ASSETS = frozenset({"BTC", "ETH", "SOL"})
SETTLEMENT_ASSETS = frozenset({"USDT", "USDC"})
ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class ExecutedSpotTrade:
    """An already-executed external spot trade. It has no execution capability."""

    side: TradingSide
    asset: str
    execution_price: Decimal
    settlement_asset: str
    fee_stable: Decimal
    stable_debit: Decimal | None = None
    quantity_asset: Decimal | None = None

    def __post_init__(self) -> None:
        require(isinstance(self.side, TradingSide), "Invalid trading side.")
        require(self.asset in TRADED_ASSETS, "Supported traded assets: BTC ETH SOL.")
        require(
            self.settlement_asset in SETTLEMENT_ASSETS,
            "Settlement stablecoin must be USDT or USDC.",
        )
        finite_decimal(self.execution_price, positive=True)
        finite_decimal(self.fee_stable)
        require(self.fee_stable >= ZERO, "Fee cannot be negative.")
        if self.side is TradingSide.BUY:
            require(
                self.stable_debit is not None and self.quantity_asset is None,
                "BUY requires total stable debit and does not accept asset quantity.",
            )
            finite_decimal(self.stable_debit, positive=True)
            require(
                self.fee_stable < self.stable_debit,
                "BUY fee must be less than total stable debit.",
            )
        else:
            require(
                self.quantity_asset is not None and self.stable_debit is None,
                "SELL requires asset quantity and does not accept stable debit.",
            )
            finite_decimal(self.quantity_asset, positive=True)
            require(
                self.fee_stable < self.gross_quote_amount,
                "SELL fee must be less than gross proceeds.",
            )

    @classmethod
    def buy(
        cls,
        asset: str,
        execution_price: Decimal,
        settlement_asset: str,
        stable_debit: Decimal,
        fee_stable: Decimal = ZERO,
    ) -> "ExecutedSpotTrade":
        return cls(
            TradingSide.BUY,
            asset,
            execution_price,
            settlement_asset,
            fee_stable,
            stable_debit=stable_debit,
        )

    @classmethod
    def sell(
        cls,
        asset: str,
        execution_price: Decimal,
        settlement_asset: str,
        quantity_asset: Decimal,
        fee_stable: Decimal = ZERO,
    ) -> "ExecutedSpotTrade":
        return cls(
            TradingSide.SELL,
            asset,
            execution_price,
            settlement_asset,
            fee_stable,
            quantity_asset=quantity_asset,
        )

    @property
    def gross_quote_amount(self) -> Decimal:
        with calculation_context():
            if self.side is TradingSide.BUY:
                return self.stable_debit - self.fee_stable
            return self.quantity_asset * self.execution_price

    @property
    def executed_asset_quantity(self) -> Decimal:
        with calculation_context():
            if self.side is TradingSide.BUY:
                return self.gross_quote_amount / self.execution_price
            return self.quantity_asset

    @property
    def net_stable_credit(self) -> Decimal:
        require(self.side is TradingSide.SELL, "Net stable credit is defined only for SELL.")
        with calculation_context():
            return self.gross_quote_amount - self.fee_stable


@dataclass(frozen=True, slots=True)
class SpotTradeCalculation:
    portfolio: Portfolio
    executed_asset_quantity: Decimal
    gross_quote_amount: Decimal
    stable_balance_change: Decimal
    warnings: tuple[str, ...] = ()


class SpotTradeCalculator:
    """Pure balance transformation; reusable by a future exact-order fill workflow."""

    def calculate(
        self, snapshot: ConfirmedPortfolioSnapshot, trade: ExecutedSpotTrade
    ) -> SpotTradeCalculation:
        require(isinstance(snapshot, ConfirmedPortfolioSnapshot), "Confirmed snapshot required.")
        require(isinstance(trade, ExecutedSpotTrade), "Executed spot trade required.")
        holdings = {position.symbol: position.amount for position in snapshot.portfolio.positions}
        asset_before = holdings.get(trade.asset, ZERO)
        stable_before = holdings.get(trade.settlement_asset, ZERO)

        with calculation_context():
            quantity = trade.executed_asset_quantity
            gross_quote = trade.gross_quote_amount
            if trade.side is TradingSide.BUY:
                require(
                    stable_before >= trade.stable_debit,
                    f"Insufficient {trade.settlement_asset} balance for this BUY.",
                )
                holdings[trade.asset] = asset_before + quantity
                holdings[trade.settlement_asset] = stable_before - trade.stable_debit
                stable_change = -trade.stable_debit
            else:
                require(asset_before >= quantity, f"Insufficient {trade.asset} balance for SELL.")
                asset_after = asset_before - quantity
                core = next(
                    (value for value in snapshot.core_positions if value.symbol == trade.asset),
                    None,
                )
                require(
                    core is None or asset_after >= core.hard_floor,
                    f"SELL would violate the configured {trade.asset} core hard floor.",
                )
                stable_credit = trade.net_stable_credit
                holdings[trade.asset] = asset_after
                holdings[trade.settlement_asset] = stable_before + stable_credit
                stable_change = stable_credit

        positions = []
        emitted = set()
        for position in snapshot.portfolio.positions:
            amount = holdings[position.symbol]
            emitted.add(position.symbol)
            if amount > ZERO:
                positions.append(AssetPosition(position.symbol, amount))
        for symbol in (trade.asset, trade.settlement_asset):
            if symbol not in emitted and holdings.get(symbol, ZERO) > ZERO:
                positions.append(AssetPosition(symbol, holdings[symbol]))
                emitted.add(symbol)
        portfolio = Portfolio(tuple(positions))
        return SpotTradeCalculation(
            portfolio,
            quantity,
            gross_quote,
            stable_change,
            self._open_order_warnings(snapshot, portfolio),
        )

    @staticmethod
    def _open_order_warnings(
        snapshot: ConfirmedPortfolioSnapshot, portfolio: Portfolio
    ) -> tuple[str, ...]:
        open_orders = tuple(
            order for order in snapshot.orders if order.status is LimitOrderStatus.OPEN
        )
        if not open_orders:
            return ()
        holdings = {position.symbol: position.amount for position in portfolio.positions}
        with calculation_context():
            stable_balance = sum((holdings.get(symbol, ZERO) for symbol in SETTLEMENT_ASSETS), ZERO)
            buy_commitments = sum(
                (
                    order.target_price * order.quantity_asset
                    for order in open_orders
                    if order.side is TradingSide.BUY
                ),
                ZERO,
            )
            sell_commitments: dict[str, Decimal] = {}
            for order in open_orders:
                if order.side is TradingSide.SELL:
                    sell_commitments[order.asset] = (
                        sell_commitments.get(order.asset, ZERO) + order.quantity_asset
                    )
        warnings = [
            "WARNING: Existing OPEN orders are unchanged; review them against the new balances."
        ]
        if buy_commitments > stable_balance or any(
            quantity > holdings.get(symbol, ZERO) for symbol, quantity in sell_commitments.items()
        ):
            warnings.append(
                "WARNING: Existing OPEN order commitments exceed the confirmed balances."
            )
        return tuple(warnings)


@dataclass(frozen=True, slots=True)
class ManualSpotTradeProposal:
    current: CurrentPortfolio
    trade: ExecutedSpotTrade
    calculation: SpotTradeCalculation


class RecordManualSpotTrade:
    def __init__(
        self,
        snapshots: SnapshotRepository,
        *,
        calculator: SpotTradeCalculator | None = None,
        now=lambda: datetime.now(UTC),
        identity_factory: Callable[[], UUID] = uuid4,
    ):
        self._snapshots = snapshots
        self._calculator = calculator or SpotTradeCalculator()
        self._now = now
        self._identity_factory = identity_factory

    def prepare(
        self, current: CurrentPortfolio, trade: ExecutedSpotTrade
    ) -> ManualSpotTradeProposal:
        require(isinstance(current, CurrentPortfolio), "Current confirmed portfolio required.")
        return ManualSpotTradeProposal(
            current, trade, self._calculator.calculate(current.snapshot, trade)
        )

    async def confirm(self, proposal: ManualSpotTradeProposal) -> CurrentPortfolio:
        require(isinstance(proposal, ManualSpotTradeProposal), "Manual trade proposal required.")
        confirmed_at = self._now()
        snapshot = replace(
            proposal.current.snapshot,
            id=self._identity_factory(),
            confirmed_at=confirmed_at,
            portfolio=proposal.calculation.portfolio,
        )
        try:
            return await ConfirmedStateService(self._snapshots).confirm(
                snapshot,
                expected_revision=proposal.current.revision,
                previous_snapshot_id=proposal.current.snapshot.id,
                update_type=UpdateType.SPOT_TRADE_RECORDED,
            )
        except ConflictError:
            raise ConflictError(
                "Current state changed; review the trade again before confirming."
            ) from None
