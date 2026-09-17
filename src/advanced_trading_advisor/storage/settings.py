"""Storage selection without exposing connection credentials."""

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from advanced_trading_advisor.configuration import local_environment
from advanced_trading_advisor.domain.validation import require


class StorageBackend(StrEnum):
    SQLITE = "sqlite"
    POSTGRES = "postgres"


@dataclass(frozen=True, slots=True)
class StorageSettings:
    backend: StorageBackend
    sqlite_path: Path
    database_url: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(
        cls,
        environment: dict[str, str] | None = None,
        *,
        sqlite_path: Path,
    ) -> "StorageSettings":
        values = local_environment() if environment is None else environment
        raw_backend = values.get("ADVISOR_STORAGE_BACKEND", StorageBackend.SQLITE.value)
        require(isinstance(raw_backend, str), "Invalid storage backend configuration.")
        try:
            backend = StorageBackend(raw_backend.strip().lower())
        except ValueError:
            raise ValueError("ADVISOR_STORAGE_BACKEND must be sqlite or postgres.") from None

        require(isinstance(sqlite_path, Path), "An explicit SQLite path is required.")
        raw_url = values.get("ADVISOR_DATABASE_URL")
        database_url = raw_url.strip() if isinstance(raw_url, str) and raw_url.strip() else None
        if backend is StorageBackend.POSTGRES:
            require(database_url is not None, "PostgreSQL storage requires ADVISOR_DATABASE_URL.")
            parsed = urlsplit(database_url)
            require(
                parsed.scheme in {"postgres", "postgresql"} and bool(parsed.hostname),
                "ADVISOR_DATABASE_URL must be a PostgreSQL URL.",
            )
        return cls(backend, sqlite_path.expanduser().absolute(), database_url)
