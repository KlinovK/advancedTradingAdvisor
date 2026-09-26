"""Schema-only wire metadata; domain parsing and JSONResponse serialization stay authoritative."""

from enum import StrEnum
from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import WithJsonSchema

DecimalText = Annotated[
    str,
    WithJsonSchema({"type": "string", "description": "Exact decimal text, never a JSON number."}),
]
UUIDText = Annotated[str, WithJsonSchema({"type": "string", "format": "uuid"})]
UTCText = Annotated[
    str,
    WithJsonSchema(
        {
            "type": "string",
            "format": "date-time",
            "description": "UTC RFC3339 with six fractional digits and Z.",
        }
    ),
]


def enum_text(enum: type[StrEnum]) -> WithJsonSchema:
    return WithJsonSchema({"type": "string", "enum": [item.value for item in enum]})


class ErrorDetail(TypedDict):
    code: Literal[
        "unauthorized",
        "portfolio_not_found",
        "analysis_not_found",
        "resource_not_found",
        "revision_conflict",
        "state_conflict",
        "invalid_request",
        "validation_error",
        "internal_error",
        "not_found",
        "method_not_allowed",
        "http_error",
    ]
    message: str
    reload_required: NotRequired[Literal[True]]


class ErrorEnvelope(TypedDict):
    error: ErrorDetail


def error_schemas(*statuses: int) -> dict:
    return {status: {"model": ErrorEnvelope} for status in statuses}


WRITE_ERRORS = error_schemas(400, 401, 404, 409, 422, 500)
