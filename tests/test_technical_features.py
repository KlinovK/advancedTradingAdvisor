from datetime import UTC, datetime, timedelta
from decimal import Decimal

from advanced_trading_advisor.market.models import Candle
from advanced_trading_advisor.market.technical_features import TechnicalFeatureCalculator

START = datetime(2026, 8, 25, tzinfo=UTC)


def linear_candles(count: int = 60) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            closed_at=START + timedelta(hours=4 * index),
            open=Decimal(10 + index),
            high=Decimal(11 + index),
            low=Decimal(9 + index),
            close=Decimal(10 + index),
        )
        for index in range(count)
    )


def test_change_24h_uses_six_closed_4h_intervals() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.change_24h_pct == Decimal("9.5238095238095238095238095238095238095")


def test_change_7d_uses_42_closed_4h_intervals() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.change_7d_pct == Decimal("155.55555555555555555555555555555555556")


def test_rsi14_wilder_result_for_all_gain_fixture_is_100() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.rsi_4h == Decimal("100")


def test_ema20_uses_sma_seed_and_decimal_recursion() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.ema20_4h == Decimal("59.5")


def test_ema50_uses_sma_seed_and_decimal_recursion() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.ema50_4h == Decimal("44.5")


def test_atr14_uses_wilder_smoothing_of_standard_true_range() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.atr_4h == Decimal("2")


def test_support_is_lowest_low_of_latest_20_closed_candles() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.support == Decimal("49")


def test_resistance_is_highest_high_of_latest_20_closed_candles() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles())

    assert features.resistance == Decimal("70")


def test_insufficient_history_degrades_each_unavailable_feature() -> None:
    features = TechnicalFeatureCalculator().calculate(linear_candles(10))

    assert features.change_24h_pct is not None
    assert features.change_7d_pct is None
    assert features.rsi_4h is None
    assert features.ema20_4h is None
    assert features.ema50_4h is None
    assert features.atr_4h is None
    assert features.support is None
    assert features.resistance is None
