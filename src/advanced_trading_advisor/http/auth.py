"""Single-user bearer authentication; never render or log the configured token."""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass, field

from fastapi import Request


@dataclass(frozen=True, slots=True)
class APITokenSettings:
    token: str = field(repr=False)

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "APITokenSettings":
        token = environment.get("ADVISOR_API_TOKEN")
        if (
            not isinstance(token, str)
            or not token
            or any(character.isspace() for character in token)
        ):
            raise ValueError("ADVISOR_API_TOKEN must be configured as a nonblank token.")
        return cls(token)


class UnauthorizedError(Exception):
    """The bearer credential is absent or invalid."""


def bearer_authentication(settings: APITokenSettings):
    expected = settings.token.encode("utf-8")

    async def authenticate(request: Request) -> None:
        authorization_headers = request.headers.getlist("authorization")
        if len(authorization_headers) != 1:
            raise UnauthorizedError
        authorization = authorization_headers[0]
        scheme, separator, supplied = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not supplied
            or any(character.isspace() for character in supplied)
            or not hmac.compare_digest(expected, supplied.encode("utf-8"))
        ):
            raise UnauthorizedError

    return authenticate
