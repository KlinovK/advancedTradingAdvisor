import re
from datetime import datetime
from uuid import UUID


class DomainValidationError(ValueError):
    """Input violates a domain invariant."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise DomainValidationError(message)


def symbol(value: object) -> None:
    require(
        isinstance(value, str) and re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{0,19}", value) is not None,
        "Asset symbol must be canonical uppercase ASCII, 1–20 characters.",
    )


def timestamp(value: object) -> None:
    require(
        isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None,
        "Timestamp must be timezone-aware.",
    )


def identity(value: object) -> None:
    require(isinstance(value, UUID) and value.int != 0, "Identity must be a non-nil UUID.")


def items(value: object, kind: type) -> None:
    require(
        isinstance(value, tuple) and all(isinstance(item, kind) for item in value),
        "Collection must be a tuple of validated domain values.",
    )
