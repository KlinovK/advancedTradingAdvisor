"""Replay-friendly closed-candle swing trigger state; no portfolio sizing or execution."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from advanced_trading_advisor.domain.numeric import calculation_context, finite_decimal
from advanced_trading_advisor.domain.validation import items, require, timestamp
from advanced_trading_advisor.market.models import Candle
from advanced_trading_advisor.portfolio.models import TradingSide


class SetupStatus(StrEnum):
    CANDIDATE = "candidate"
    APPROACHING_ZONE = "approaching_zone"
    ZONE_TOUCHED = "zone_touched"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    CONFIRMED = "confirmed"
    ACTIVE = "active"  # Historical Phase 9 actionable state.
    FILLED_OR_CONFIRMED_MANUALLY = "filled_or_confirmed_manually"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class TriggerTimeframe(StrEnum):
    ONE_HOUR = "1h"
    FOUR_HOUR = "4h"
    MANUAL = "manual"


class TriggerReason(StrEnum):
    APPROACHED_ZONE = "approached_deterministic_zone"
    TOUCHED_ZONE = "closed_1h_range_touched_zone"
    AWAITING_CLOSED_CONFIRMATION = "awaiting_subsequent_closed_1h_confirmation"
    RESISTANCE_REJECTION = "closed_1h_resistance_rejection_confirmed"
    SUPPORT_HOLD = "closed_1h_support_hold_confirmed"
    RESISTANCE_BREAKOUT = "closed_4h_breakout_invalidated_rejection"
    SUPPORT_BREAKDOWN = "closed_4h_breakdown_invalidated_support"
    CONFIRMATION_WINDOW_EXPIRED = "confirmation_window_expired"
    EXECUTION_WINDOW_EXPIRED = "execution_window_expired"
    EXECUTION_MOVED_TOO_FAR = "execution_price_moved_too_far_from_trigger"
    MANUAL_FILL_CONFIRMED = "order_fill_confirmed_manually"
    MANUAL_CANCELLATION_CONFIRMED = "order_cancellation_confirmed_manually"


TERMINAL_TRIGGER_STATES = {
    SetupStatus.FILLED_OR_CONFIRMED_MANUALLY,
    SetupStatus.INVALIDATED,
    SetupStatus.EXPIRED,
    SetupStatus.COMPLETED,
    SetupStatus.CANCELLED,
}

ALLOWED_TRANSITIONS = {
    SetupStatus.CANDIDATE: {
        SetupStatus.APPROACHING_ZONE,
        SetupStatus.ZONE_TOUCHED,
        SetupStatus.INVALIDATED,
    },
    SetupStatus.APPROACHING_ZONE: {
        SetupStatus.ZONE_TOUCHED,
        SetupStatus.INVALIDATED,
    },
    SetupStatus.ZONE_TOUCHED: {
        SetupStatus.AWAITING_CONFIRMATION,
        SetupStatus.CONFIRMED,
        SetupStatus.INVALIDATED,
        SetupStatus.EXPIRED,
    },
    SetupStatus.AWAITING_CONFIRMATION: {
        SetupStatus.CONFIRMED,
        SetupStatus.INVALIDATED,
        SetupStatus.EXPIRED,
    },
    SetupStatus.CONFIRMED: {SetupStatus.INVALIDATED, SetupStatus.EXPIRED},
}
for _state in (
    SetupStatus.CANDIDATE,
    SetupStatus.APPROACHING_ZONE,
    SetupStatus.ZONE_TOUCHED,
    SetupStatus.AWAITING_CONFIRMATION,
    SetupStatus.CONFIRMED,
):
    ALLOWED_TRANSITIONS[_state] |= {
        SetupStatus.FILLED_OR_CONFIRMED_MANUALLY,
        SetupStatus.CANCELLED,
    }


@dataclass(frozen=True, slots=True)
class TriggerTransition:
    previous_state: SetupStatus
    current_state: SetupStatus
    transitioned_at: datetime
    market_cutoff: datetime
    timeframe: TriggerTimeframe
    reason: TriggerReason
    reference_price: Decimal

    def __post_init__(self):
        require(
            self.current_state in ALLOWED_TRANSITIONS.get(self.previous_state, set()),
            "Invalid trigger state transition.",
        )
        timestamp(self.transitioned_at)
        timestamp(self.market_cutoff)
        require(self.transitioned_at == self.market_cutoff, "Transition must use candle cutoff.")
        require(isinstance(self.timeframe, TriggerTimeframe), "Invalid trigger timeframe.")
        require(isinstance(self.reason, TriggerReason), "Invalid trigger reason.")
        finite_decimal(self.reference_price, positive=True)


@dataclass(frozen=True, slots=True)
class TriggerContext:
    version: int
    zone_low: Decimal
    zone_high: Decimal
    atr_at_creation: Decimal
    transitions: tuple[TriggerTransition, ...] = ()
    touched_at: datetime | None = None
    highest_observed_price: Decimal | None = None
    lowest_observed_price: Decimal | None = None
    trigger_reference_price: Decimal | None = None
    confirmation_at: datetime | None = None
    confirmation_candle: Candle | None = None
    expires_at: datetime | None = None
    last_processed_1h_candle_at: datetime | None = None
    execution_valid: bool = False

    def __post_init__(self):
        require(type(self.version) is int and self.version == 1, "Unsupported trigger state.")
        for value in (self.zone_low, self.zone_high, self.atr_at_creation):
            finite_decimal(value, positive=True)
        require(self.zone_low <= self.zone_high, "Trigger zone is inverted.")
        items(self.transitions, TriggerTransition)
        for value in (
            self.touched_at,
            self.confirmation_at,
            self.expires_at,
            self.last_processed_1h_candle_at,
        ):
            if value is not None:
                timestamp(value)
        for value in (
            self.highest_observed_price,
            self.lowest_observed_price,
            self.trigger_reference_price,
        ):
            if value is not None:
                finite_decimal(value, positive=True)
        if self.confirmation_candle is not None:
            require(isinstance(self.confirmation_candle, Candle), "Invalid confirmation candle.")
            require(
                self.confirmation_at == self.confirmation_candle.closed_at,
                "Confirmation candle timestamp disagrees with confirmation time.",
            )
        else:
            require(self.confirmation_at is None, "Confirmation time requires candle evidence.")
        if self.highest_observed_price is not None and self.lowest_observed_price is not None:
            require(
                self.lowest_observed_price <= self.highest_observed_price,
                "Trigger excursion is inverted.",
            )
        require(type(self.execution_valid) is bool, "Invalid execution-valid flag.")
        if self.transitions:
            require(
                self.transitions[0].previous_state is SetupStatus.CANDIDATE
                and all(
                    later.previous_state is earlier.current_state
                    for earlier, later in zip(self.transitions, self.transitions[1:], strict=False)
                ),
                "Trigger transition history is discontinuous.",
            )
            require(
                all(
                    later.market_cutoff >= earlier.market_cutoff
                    for earlier, later in zip(self.transitions, self.transitions[1:], strict=False)
                ),
                "Trigger transitions cannot move backwards in market time.",
            )
            require(
                len({(t.current_state, t.market_cutoff, t.reason) for t in self.transitions})
                == len(self.transitions),
                "Duplicate trigger transition.",
            )
        if self.touched_at is not None:
            require(
                any(
                    transition.current_state is SetupStatus.ZONE_TOUCHED
                    and transition.market_cutoff == self.touched_at
                    for transition in self.transitions
                ),
                "Touch timestamp lacks transition evidence.",
            )
        if self.confirmation_candle is not None:
            require(
                any(
                    transition.current_state is SetupStatus.CONFIRMED
                    and transition.market_cutoff == self.confirmation_candle.closed_at
                    for transition in self.transitions
                ),
                "Confirmation candle lacks transition evidence.",
            )
        if self.execution_valid:
            require(
                bool(self.transitions)
                and self.transitions[-1].current_state is SetupStatus.CONFIRMED
                and self.confirmation_candle is not None,
                "Execution validity requires current confirmation evidence.",
            )


@dataclass(frozen=True, slots=True)
class SwingTriggerPolicy:
    version: int = 1
    approach_distance_atr: Decimal = Decimal("1")
    touch_tolerance_atr: Decimal = Decimal("0.25")
    confirmation_move_atr: Decimal = Decimal("0.25")
    breakout_invalidation_atr: Decimal = Decimal("1")
    confirmation_window_candles: int = 4
    execution_window_candles: int = 4
    maximum_execution_distance_atr: Decimal = Decimal("1")

    def __post_init__(self):
        require(type(self.version) is int and self.version == 1, "Unsupported trigger policy.")
        for value in (
            self.approach_distance_atr,
            self.touch_tolerance_atr,
            self.confirmation_move_atr,
            self.breakout_invalidation_atr,
            self.maximum_execution_distance_atr,
        ):
            finite_decimal(value, positive=True)
        require(
            type(self.confirmation_window_candles) is int
            and self.confirmation_window_candles > 0
            and type(self.execution_window_candles) is int
            and self.execution_window_candles > 0,
            "Trigger candle windows must be positive integers.",
        )


class SwingTriggerEngine:
    """Pure state transition function over ordered, closed one-hour candles."""

    def __init__(self, policy: SwingTriggerPolicy | None = None):
        self.policy = policy or SwingTriggerPolicy()

    def initial_context(self, *, level: Decimal, atr: Decimal) -> TriggerContext:
        finite_decimal(level, positive=True)
        finite_decimal(atr, positive=True)
        tolerance = atr * self.policy.touch_tolerance_atr
        return TriggerContext(1, level - tolerance, level + tolerance, atr)

    def evaluate(
        self,
        *,
        status: SetupStatus,
        context: TriggerContext,
        direction: TradingSide,
        candles: tuple[Candle, ...],
        latest_4h_close: Decimal | None,
        latest_4h_cutoff: datetime | None,
        invalidation_price: Decimal,
    ) -> tuple[SetupStatus, TriggerContext]:
        require(isinstance(status, SetupStatus), "Invalid setup status.")
        require(isinstance(context, TriggerContext), "Trigger context required.")
        require(isinstance(direction, TradingSide), "Invalid trigger direction.")
        items(candles, Candle)
        require(
            all(
                b.closed_at - a.closed_at == timedelta(hours=1)
                for a, b in zip(candles, candles[1:], strict=False)
            ),
            "Trigger candles must be ordered closed 1H candles.",
        )
        finite_decimal(invalidation_price, positive=True)
        if status in TERMINAL_TRIGGER_STATES or status is SetupStatus.ACTIVE:
            return status, context

        with calculation_context():
            if latest_4h_close is not None and latest_4h_cutoff is not None:
                finite_decimal(latest_4h_close, positive=True)
                timestamp(latest_4h_cutoff)
                invalidated = (
                    direction is TradingSide.SELL and latest_4h_close >= invalidation_price
                ) or (direction is TradingSide.BUY and latest_4h_close <= invalidation_price)
                if invalidated:
                    reason = (
                        TriggerReason.RESISTANCE_BREAKOUT
                        if direction is TradingSide.SELL
                        else TriggerReason.SUPPORT_BREAKDOWN
                    )
                    return self._transition(
                        status,
                        SetupStatus.INVALIDATED,
                        context,
                        latest_4h_cutoff,
                        TriggerTimeframe.FOUR_HOUR,
                        reason,
                        latest_4h_close,
                        execution_valid=False,
                    )
            for candle in candles:
                if (
                    context.last_processed_1h_candle_at is not None
                    and candle.closed_at <= context.last_processed_1h_candle_at
                ):
                    continue
                context = replace(
                    context,
                    highest_observed_price=max(
                        candle.high, context.highest_observed_price or candle.high
                    ),
                    lowest_observed_price=min(
                        candle.low, context.lowest_observed_price or candle.low
                    ),
                    last_processed_1h_candle_at=candle.closed_at,
                )
                if status in TERMINAL_TRIGGER_STATES:
                    break
                if status is SetupStatus.CONFIRMED:
                    expired_reason = self._execution_expiry(context, direction, candle)
                    if expired_reason is not None:
                        status, context = self._transition(
                            status,
                            SetupStatus.EXPIRED,
                            context,
                            candle.closed_at,
                            TriggerTimeframe.ONE_HOUR,
                            expired_reason,
                            candle.close,
                            execution_valid=False,
                        )
                    continue
                if status in {SetupStatus.ZONE_TOUCHED, SetupStatus.AWAITING_CONFIRMATION}:
                    if self._confirmed(context, direction, candle):
                        status, context = self._transition(
                            status,
                            SetupStatus.CONFIRMED,
                            context,
                            candle.closed_at,
                            TriggerTimeframe.ONE_HOUR,
                            TriggerReason.RESISTANCE_REJECTION
                            if direction is TradingSide.SELL
                            else TriggerReason.SUPPORT_HOLD,
                            candle.close,
                            trigger_reference_price=candle.close,
                            confirmation_at=candle.closed_at,
                            confirmation_candle=candle,
                            expires_at=candle.closed_at
                            + timedelta(hours=self.policy.execution_window_candles),
                            execution_valid=True,
                        )
                    elif (
                        context.touched_at is not None
                        and candle.closed_at - context.touched_at
                        >= timedelta(hours=self.policy.confirmation_window_candles)
                    ):
                        status, context = self._transition(
                            status,
                            SetupStatus.EXPIRED,
                            context,
                            candle.closed_at,
                            TriggerTimeframe.ONE_HOUR,
                            TriggerReason.CONFIRMATION_WINDOW_EXPIRED,
                            candle.close,
                        )
                    elif status is SetupStatus.ZONE_TOUCHED:
                        status, context = self._transition(
                            status,
                            SetupStatus.AWAITING_CONFIRMATION,
                            context,
                            candle.closed_at,
                            TriggerTimeframe.ONE_HOUR,
                            TriggerReason.AWAITING_CLOSED_CONFIRMATION,
                            candle.close,
                        )
                    continue
                touched = candle.high >= context.zone_low and candle.low <= context.zone_high
                if touched:
                    status, context = self._transition(
                        status,
                        SetupStatus.ZONE_TOUCHED,
                        context,
                        candle.closed_at,
                        TriggerTimeframe.ONE_HOUR,
                        TriggerReason.TOUCHED_ZONE,
                        candle.close,
                        touched_at=candle.closed_at,
                    )
                    continue
                approaching = (
                    direction is TradingSide.SELL
                    and candle.high
                    >= context.zone_low
                    - context.atr_at_creation * self.policy.approach_distance_atr
                ) or (
                    direction is TradingSide.BUY
                    and candle.low
                    <= context.zone_high
                    + context.atr_at_creation * self.policy.approach_distance_atr
                )
                if status is SetupStatus.CANDIDATE and approaching:
                    status, context = self._transition(
                        status,
                        SetupStatus.APPROACHING_ZONE,
                        context,
                        candle.closed_at,
                        TriggerTimeframe.ONE_HOUR,
                        TriggerReason.APPROACHED_ZONE,
                        candle.close,
                    )
            return status, context

    def record_manual_transition(
        self,
        *,
        status: SetupStatus,
        context: TriggerContext,
        current: SetupStatus,
        confirmed_at: datetime,
        reference_price: Decimal,
    ) -> tuple[SetupStatus, TriggerContext]:
        require(
            current in {SetupStatus.FILLED_OR_CONFIRMED_MANUALLY, SetupStatus.CANCELLED},
            "Unsupported manual setup transition.",
        )
        return self._transition(
            status,
            current,
            context,
            confirmed_at,
            TriggerTimeframe.MANUAL,
            TriggerReason.MANUAL_FILL_CONFIRMED
            if current is SetupStatus.FILLED_OR_CONFIRMED_MANUALLY
            else TriggerReason.MANUAL_CANCELLATION_CONFIRMED,
            reference_price,
            execution_valid=False,
        )

    def invalidate(
        self,
        *,
        status: SetupStatus,
        context: TriggerContext,
        direction: TradingSide,
        cutoff: datetime,
        reference_price: Decimal,
    ) -> tuple[SetupStatus, TriggerContext]:
        return self._transition(
            status,
            SetupStatus.INVALIDATED,
            context,
            cutoff,
            TriggerTimeframe.FOUR_HOUR,
            TriggerReason.RESISTANCE_BREAKOUT
            if direction is TradingSide.SELL
            else TriggerReason.SUPPORT_BREAKDOWN,
            reference_price,
            execution_valid=False,
        )

    def _confirmed(self, context, direction, candle):
        if context.touched_at is None or candle.closed_at <= context.touched_at:
            return False
        move = context.atr_at_creation * self.policy.confirmation_move_atr
        if direction is TradingSide.SELL:
            return candle.close < candle.open and candle.close <= context.zone_low - move
        return candle.close > candle.open and candle.close >= context.zone_high + move

    def _execution_expiry(self, context, direction, candle):
        if context.expires_at is not None and candle.closed_at > context.expires_at:
            return TriggerReason.EXECUTION_WINDOW_EXPIRED
        reference = context.trigger_reference_price
        if reference is None:
            return TriggerReason.EXECUTION_WINDOW_EXPIRED
        distance = context.atr_at_creation * self.policy.maximum_execution_distance_atr
        if direction is TradingSide.SELL and reference - candle.close > distance:
            return TriggerReason.EXECUTION_MOVED_TOO_FAR
        if direction is TradingSide.BUY and candle.close - reference > distance:
            return TriggerReason.EXECUTION_MOVED_TOO_FAR
        return None

    @staticmethod
    def _transition(
        previous,
        current,
        context,
        cutoff,
        timeframe,
        reason,
        reference,
        **changes,
    ):
        transition = TriggerTransition(
            previous, current, cutoff, cutoff, timeframe, reason, reference
        )
        return current, replace(
            context,
            transitions=context.transitions + (transition,),
            **changes,
        )
