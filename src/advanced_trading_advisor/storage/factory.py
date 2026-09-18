"""Infrastructure composition for interchangeable persistence backends."""

from dataclasses import dataclass

from advanced_trading_advisor.domain.persistence import PersistenceError

from .database import Database
from .delivery import SQLiteDeliveryRepository
from .postgres_database import PostgresDatabase
from .postgres_repositories import (
    PostgresAnalysisRunRepository,
    PostgresConfirmedUpdateRepository,
    PostgresDeliveryRepository,
    PostgresSchedulerRepository,
    PostgresSnapshotRepository,
)
from .repositories import (
    SQLiteAnalysisRunRepository,
    SQLiteConfirmedUpdateRepository,
    SQLiteSnapshotRepository,
)
from .scheduler import SQLiteSchedulerRepository
from .settings import StorageBackend, StorageSettings


@dataclass(frozen=True, slots=True)
class StorageBundle:
    settings: StorageSettings
    database: object
    snapshots: object
    confirmed_updates: object
    analyses: object
    scheduler: object
    deliveries: object

    @property
    def backend(self) -> StorageBackend:
        return self.settings.backend

    @property
    def is_sqlite(self) -> bool:
        return self.backend is StorageBackend.SQLITE

    async def initialize(self, *, create_parent: bool = False) -> None:
        await self.database.initialize(create_parent=create_parent)

    def sqlite_database(self, operation: str) -> Database:
        if not self.is_sqlite:
            raise PersistenceError(f"{operation} is available only for SQLite storage.")
        return self.database


def create_storage(
    settings: StorageSettings, *, postgres_schema: str | None = None
) -> StorageBundle:
    if settings.backend is StorageBackend.SQLITE:
        database = Database(settings.sqlite_path)
        return StorageBundle(
            settings,
            database,
            SQLiteSnapshotRepository(database),
            SQLiteConfirmedUpdateRepository(database),
            SQLiteAnalysisRunRepository(database),
            SQLiteSchedulerRepository(database),
            SQLiteDeliveryRepository(database),
        )
    database = PostgresDatabase(
        settings.database_url,
        schema="public" if postgres_schema is None else postgres_schema,
    )
    return StorageBundle(
        settings,
        database,
        PostgresSnapshotRepository(database),
        PostgresConfirmedUpdateRepository(database),
        PostgresAnalysisRunRepository(database),
        PostgresSchedulerRepository(database),
        PostgresDeliveryRepository(database),
    )


def sqlite_storage(database: Database) -> StorageBundle:
    """Compatibility adapter for existing offline composition tests."""
    settings = StorageSettings(StorageBackend.SQLITE, database.path)
    return StorageBundle(
        settings,
        database,
        SQLiteSnapshotRepository(database),
        SQLiteConfirmedUpdateRepository(database),
        SQLiteAnalysisRunRepository(database),
        SQLiteSchedulerRepository(database),
        SQLiteDeliveryRepository(database),
    )


def as_storage(value) -> StorageBundle:
    return value if isinstance(value, StorageBundle) else sqlite_storage(value)
