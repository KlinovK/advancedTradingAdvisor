"""Async PostgreSQL connection and schema lifecycle with sanitized failures."""

import asyncio
import importlib
import re
from contextlib import asynccontextmanager

from advanced_trading_advisor.domain.persistence import PersistenceError, SchemaVersionError
from advanced_trading_advisor.domain.validation import require

from .postgres_schema import POSTGRES_COLUMNS, POSTGRES_MIGRATIONS, POSTGRES_SCHEMA_VERSION

SCHEMA_LOCK = 6_416_892_125_109_931_071


class PostgresDatabase:
    """Short-lived asyncpg connections; credentials are never rendered in errors or repr."""

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: float = 10,
        command_timeout_seconds: float = 30,
        schema: str | None = None,
        driver=None,
    ):
        require(isinstance(database_url, str) and bool(database_url), "Database URL required.")
        require(
            isinstance(connect_timeout_seconds, int | float) and 0 < connect_timeout_seconds <= 60,
            "Invalid PostgreSQL connect timeout.",
        )
        require(
            isinstance(command_timeout_seconds, int | float) and 0 < command_timeout_seconds <= 300,
            "Invalid PostgreSQL command timeout.",
        )
        require(
            schema is None or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) is not None,
            "Invalid PostgreSQL schema name.",
        )
        self._database_url = database_url
        self.connect_timeout_seconds = connect_timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self.schema = schema
        self._driver_override = driver

    def __repr__(self) -> str:
        return f"PostgresDatabase(schema={self.schema!r})"

    def _driver(self):
        if self._driver_override is not None:
            return self._driver_override
        try:
            return importlib.import_module("asyncpg")
        except ImportError:
            raise PersistenceError(
                "PostgreSQL driver unavailable; install the project dependencies."
            ) from None

    def _is_driver_error(self, error: BaseException) -> bool:
        driver = self._driver()
        types = tuple(
            value
            for name in ("PostgresError", "InterfaceError")
            if isinstance((value := getattr(driver, name, None)), type)
        )
        return bool(types) and isinstance(error, types)

    async def _open(self):
        driver = self._driver()
        options = {
            "dsn": self._database_url,
            "timeout": self.connect_timeout_seconds,
            "command_timeout": self.command_timeout_seconds,
        }
        if self.schema is not None:
            options["server_settings"] = {"search_path": self.schema}
        try:
            return await driver.connect(**options)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Driver/client configuration errors can contain the full DSN.
            raise PersistenceError("PostgreSQL storage unavailable.") from None

    @asynccontextmanager
    async def connection(self):
        connection = await self._open()
        try:
            yield connection
        except BaseException as error:
            if self._is_driver_error(error):
                raise PersistenceError("PostgreSQL storage operation failed.") from None
            raise
        finally:
            try:
                await connection.close()
            except asyncio.CancelledError:
                raise
            except Exception:
                raise PersistenceError("PostgreSQL storage close failed.") from None

    @asynccontextmanager
    async def transaction(self):
        async with self.connection() as connection:
            async with connection.transaction():
                yield connection

    async def initialize(self, *, create_parent: bool = False) -> None:
        require(type(create_parent) is bool, "Invalid parent-creation flag.")
        async with self.transaction() as connection:
            await connection.execute("SELECT pg_advisory_xact_lock($1)", SCHEMA_LOCK)
            table_rows = await connection.fetch(
                """SELECT table_name FROM information_schema.tables
                   WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"""
            )
            tables = {row["table_name"] for row in table_rows}
            if "schema_metadata" not in tables:
                if tables:
                    raise SchemaVersionError(
                        "Unversioned PostgreSQL schema; explicit migration required."
                    )
                version = 0
            else:
                rows = await connection.fetch("SELECT singleton_id, version FROM schema_metadata")
                if len(rows) != 1 or rows[0]["singleton_id"] != 1:
                    raise SchemaVersionError("Malformed PostgreSQL schema metadata.")
                version = rows[0]["version"]
                if type(version) is not int or version not in range(1, POSTGRES_SCHEMA_VERSION + 1):
                    raise SchemaVersionError(
                        "Unsupported PostgreSQL schema version; explicit migration required."
                    )

            for target in range(version + 1, POSTGRES_SCHEMA_VERSION + 1):
                for statement in POSTGRES_MIGRATIONS[target]:
                    await connection.execute(statement)
                if target == 1:
                    await connection.execute(
                        "INSERT INTO schema_metadata(singleton_id, version) VALUES (1, $1)", target
                    )
                else:
                    await connection.execute(
                        "UPDATE schema_metadata SET version=$1 WHERE singleton_id=1", target
                    )

            await self._validate_schema(connection)

    @staticmethod
    async def _validate_schema(connection) -> None:
        version = await connection.fetchval(
            "SELECT version FROM schema_metadata WHERE singleton_id=1"
        )
        if version != POSTGRES_SCHEMA_VERSION:
            raise SchemaVersionError("PostgreSQL schema migration did not complete.")
        rows = await connection.fetch(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = current_schema()"""
        )
        actual: dict[str, set[str]] = {}
        for row in rows:
            actual.setdefault(row["table_name"], set()).add(row["column_name"])
        for table, expected in POSTGRES_COLUMNS.items():
            if actual.get(table) != expected:
                raise SchemaVersionError("PostgreSQL tables do not match the declared schema.")
