"""Async PostgreSQL connection and schema lifecycle with sanitized failures."""

import asyncio
import importlib
import re
from contextlib import asynccontextmanager

from advanced_trading_advisor.domain.persistence import PersistenceError, SchemaVersionError
from advanced_trading_advisor.domain.validation import require

from .postgres_schema import (
    FINANCIAL_FUNCTION_CONTRACT,
    FINANCIAL_REQUIRED_TRIGGERS,
    FINANCIAL_TABLES,
    FINANCIAL_TRIGGER_CONTRACT,
    POSTGRES_COLUMNS,
    POSTGRES_LEGACY_COLUMNS,
    POSTGRES_MIGRATIONS,
    POSTGRES_SCHEMA_VERSION,
)

SCHEMA_LOCK = 6_416_892_125_109_931_071
READINESS_TIMEOUT_SECONDS = 15


class PostgresDatabase:
    """Short-lived asyncpg connections; credentials are never rendered in errors or repr."""

    def __init__(
        self,
        database_url: str,
        *,
        connect_timeout_seconds: float = 10,
        command_timeout_seconds: float = 30,
        schema: str = "public",
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
            isinstance(schema, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) is not None,
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
            "server_settings": {"search_path": self.schema},
        }
        try:
            connection = await driver.connect(**options)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Driver/client configuration errors can contain the full DSN.
            raise PersistenceError("PostgreSQL storage unavailable.") from None

        try:
            # Startup parameters are an optimization, not the isolation boundary. Activate the
            # validated schema explicitly and fail closed if the server does not retain it.
            await connection.fetchval("SELECT set_config('search_path', $1, false)", self.schema)
            effective_schema = await connection.fetchval("SELECT current_schema()")
            if effective_schema != self.schema:
                raise PersistenceError("PostgreSQL schema isolation unavailable.")
        except asyncio.CancelledError:
            try:
                await connection.close()
            except Exception:
                pass
            raise
        except Exception:
            try:
                await connection.close()
            except Exception:
                pass
            # Schema/session errors can contain connection details or raw server configuration.
            raise PersistenceError("PostgreSQL schema isolation unavailable.") from None
        return connection

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

    async def check_ready(self) -> None:
        """Check complete financial v4 readiness without changing durable state."""
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            async with self.connection() as connection:
                async with connection.transaction(readonly=True):
                    await self.validate_financial_schema(connection)

    async def check_legacy_ready(self) -> None:
        """Read-only readiness for the existing Advisor tables at v3 or v4."""
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            async with self.connection() as connection:
                async with connection.transaction(readonly=True):
                    await self.validate_legacy_schema(connection)

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

            await self.validate_financial_schema(connection)

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

    @staticmethod
    async def validate_legacy_schema(connection) -> None:
        version = await connection.fetchval(
            "SELECT version FROM schema_metadata WHERE singleton_id=1"
        )
        if type(version) is not int or version not in (3, POSTGRES_SCHEMA_VERSION):
            raise SchemaVersionError("Unsupported PostgreSQL schema version.")
        if version == POSTGRES_SCHEMA_VERSION:
            await PostgresDatabase.validate_financial_schema(connection)
            return
        rows = await connection.fetch(
            """SELECT table_name, column_name FROM information_schema.columns
               WHERE table_schema = current_schema()"""
        )
        actual: dict[str, set[str]] = {}
        for row in rows:
            actual.setdefault(row["table_name"], set()).add(row["column_name"])
        if FINANCIAL_TABLES & actual.keys() or any(
            actual.get(table) != expected for table, expected in POSTGRES_LEGACY_COLUMNS.items()
        ):
            raise SchemaVersionError("PostgreSQL legacy schema is incomplete.")
        for signature in ("financial_decimal_valid(numeric)", "financial_postings_complete()"):
            if await connection.fetchval("SELECT to_regprocedure($1)", signature):
                raise SchemaVersionError("PostgreSQL schema version disagrees with its objects.")

    @staticmethod
    async def validate_financial_schema(connection) -> None:
        """Require v4 shape and migration-defined immutable-ledger guards."""
        await PostgresDatabase._validate_schema(connection)
        rows = await connection.fetch(
            """SELECT c.relname AS table_name, t.tgname AS trigger_name,
                      t.tgenabled::text AS enabled, t.tgtype,
                      p.proname AS function_name,
                      (pn.nspname=current_schema()) AS function_in_schema,
                      (t.tgconstraint <> 0) AS is_constraint,
                      t.tgdeferrable AS deferrable, t.tginitdeferred AS initially_deferred
               FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
               JOIN pg_namespace n ON n.oid=c.relnamespace
               JOIN pg_proc p ON p.oid=t.tgfoid
               JOIN pg_namespace pn ON pn.oid=p.pronamespace
               WHERE n.nspname=current_schema() AND c.relname=ANY($1::text[])
                 AND NOT t.tgisinternal""",
            list(FINANCIAL_REQUIRED_TRIGGERS),
        )
        actual = {}
        for row in rows:
            actual[(row["table_name"], row["trigger_name"])] = row
        if any(
            (row := actual.get(key)) is None
            or row["enabled"] not in ("O", "A")
            or (
                row["tgtype"],
                row["function_name"],
                row["is_constraint"],
                row["deferrable"],
                row["initially_deferred"],
            )
            != expected
            or not row["function_in_schema"]
            for key, expected in FINANCIAL_TRIGGER_CONTRACT.items()
        ):
            raise SchemaVersionError("PostgreSQL financial safeguards are incomplete.")
        functions = await connection.fetch(
            """SELECT p.proname AS function_name, l.lanname AS language,
                      p.prorettype::regtype::text AS return_type,
                      p.provolatile::text AS volatility,
                      p.proisstrict AS strict, p.prosecdef AS security_definer,
                      p.prosrc AS source
               FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
               JOIN pg_language l ON l.oid=p.prolang
               WHERE n.nspname=current_schema()
                 AND p.oid IN (to_regprocedure('advisor_immutable_record()'),
                               to_regprocedure('financial_decimal_valid(numeric)'),
                               to_regprocedure('financial_postings_complete()'))"""
        )
        actual_functions = {row["function_name"]: row for row in functions}
        if any(
            (row := actual_functions.get(name)) is None
            or (
                row["language"],
                row["return_type"],
                row["volatility"],
                row["strict"],
                " ".join(row["source"].split()),
            )
            != expected
            or row["security_definer"]
            for name, expected in FINANCIAL_FUNCTION_CONTRACT.items()
        ):
            raise SchemaVersionError("PostgreSQL financial functions are incomplete.")
