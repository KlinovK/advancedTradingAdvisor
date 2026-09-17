from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from advanced_trading_advisor.domain.persistence import PersistenceError, SchemaVersionError
from advanced_trading_advisor.domain.validation import require

from .schema import (
    COLUMNS,
    DELIVERY_STATEMENTS,
    LEGACY_COLUMNS,
    SCHEDULER_COLUMNS,
    SCHEDULER_STATEMENTS,
    SCHEMA_VERSION,
    STATEMENTS,
)


class Database:
    """One short-lived connection per operation; no connection shared between tasks."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000):
        require(isinstance(path, Path), "An explicit filesystem Path is required.")
        require(
            type(busy_timeout_ms) is int and 0 < busy_timeout_ms <= 60000,
            "Busy timeout must be 1–60000 ms.",
        )
        self.path = path.expanduser().absolute()
        self.busy_timeout_ms = busy_timeout_ms

    @asynccontextmanager
    async def connection(self):
        if not self.path.is_file():
            raise PersistenceError("Initialize the database before opening repositories.")
        async with self._connect() as connection:
            yield connection

    @asynccontextmanager
    async def _connect(self):
        async with aiosqlite.connect(
            self.path, isolation_level=None, timeout=self.busy_timeout_ms / 1000
        ) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys = ON")
            yield connection

    @asynccontextmanager
    async def transaction(self):
        async with self.connection() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def initialize(self, *, create_parent: bool = False) -> None:
        require(type(create_parent) is bool, "Invalid parent-creation flag.")
        if not self.path.parent.is_dir():
            if not create_parent:
                raise PersistenceError(
                    "Database parent is missing; explicitly enable create_parent."
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self.path.is_file():
            raise PersistenceError("Database path must be a file.")
        async with self._connect() as connection:
            await connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = await connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                tables = {row[0] for row in await cursor.fetchall()}
                if not tables:
                    for statement in STATEMENTS:
                        await connection.execute(statement)
                    await connection.execute(
                        "INSERT INTO schema_metadata(singleton_id, version) VALUES (?, ?)",
                        (1, SCHEMA_VERSION),
                    )
                else:
                    if "schema_metadata" not in tables:
                        raise SchemaVersionError(
                            "Unversioned database; explicit migration required."
                        )
                    try:
                        rows = await (
                            await connection.execute(
                                "SELECT singleton_id, version FROM schema_metadata"
                            )
                        ).fetchall()
                    except aiosqlite.DatabaseError as error:
                        raise SchemaVersionError("Malformed schema metadata.") from error
                    if (
                        len(rows) != 1
                        or rows[0][0] != 1
                        or rows[0][1] not in {1, 2, SCHEMA_VERSION}
                    ):
                        raise SchemaVersionError(
                            "Unsupported database version; explicit migration required."
                        )
                    version = rows[0][1]
                    expected_columns = (
                        LEGACY_COLUMNS
                        if version == 1
                        else SCHEDULER_COLUMNS
                        if version == 2
                        else COLUMNS
                    )
                    if tables != set(expected_columns):
                        raise SchemaVersionError(
                            "Database tables do not match the declared schema."
                        )
                    for table, expected in expected_columns.items():
                        columns = await (
                            await connection.execute(
                                "SELECT name FROM pragma_table_info(?)", (table,)
                            )
                        ).fetchall()
                        if {row[0] for row in columns} != expected:
                            raise SchemaVersionError(
                                "Database columns do not match the declared schema."
                            )
                    if version == 1:
                        for statement in SCHEDULER_STATEMENTS:
                            await connection.execute(statement)
                    if version in {1, 2}:
                        for statement in DELIVERY_STATEMENTS:
                            await connection.execute(statement)
                        await connection.execute(
                            "UPDATE schema_metadata SET version=? WHERE singleton_id=1",
                            (SCHEMA_VERSION,),
                        )
                await connection.execute("""CREATE TRIGGER IF NOT EXISTS runs_terminal_no_update
                    BEFORE UPDATE ON analysis_runs WHEN OLD.status != 'running'
                    BEGIN SELECT RAISE(ABORT, 'Terminal runs are immutable'); END""")
                await connection.execute("""CREATE TRIGGER IF NOT EXISTS runs_terminal_no_delete
                    BEFORE DELETE ON analysis_runs WHEN OLD.status != 'running'
                    BEGIN SELECT RAISE(ABORT, 'Terminal runs are immutable'); END""")
                await connection.commit()
                await connection.execute("PRAGMA journal_mode = WAL")
            except BaseException:
                await connection.rollback()
                raise
