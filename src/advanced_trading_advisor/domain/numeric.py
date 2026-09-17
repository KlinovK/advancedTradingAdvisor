import re
from contextlib import contextmanager
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from .validation import require, symbol

PRECISION = 38


@contextmanager
def calculation_context():
    """Fixed arithmetic precision, independent of the caller's decimal context."""
    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)) as context:
        yield context


def finite_decimal(value: object, *, positive: bool = False) -> None:
    require(isinstance(value, Decimal), "Financial values must be Decimal.")
    require(value.is_finite(), "Financial values must be finite.")
    if positive:
        require(value > 0, "Financial value must be positive.")


def parse_decimal(text: str, *, positive: bool = False) -> Decimal:
    """Parse plain period-separated decimal text exactly, without rounding or grouping."""
    require(isinstance(text, str), "Decimal input must be text.")
    normalized = text.strip()
    require(
        re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", normalized) is not None,
        "Use plain decimal notation with a period and no grouping.",
    )
    result = Decimal(normalized)
    finite_decimal(result, positive=positive)
    return result


def parse_symbol(text: str) -> str:
    require(isinstance(text, str), "Symbol input must be text.")
    result = text.strip().upper()
    symbol(result)
    return result
