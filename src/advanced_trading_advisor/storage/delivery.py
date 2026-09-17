"""SQLite-backed Telegram delivery identity and conservative crash recovery."""

from datetime import timedelta
from uuid import UUID

from advanced_trading_advisor.delivery.models import (
    DeliveryFailureCategory,
    DeliveryRecord,
    DeliveryStatus,
    utc,
)
from advanced_trading_advisor.domain.persistence import ConflictError
from advanced_trading_advisor.domain.validation import identity, require

from .codecs import date, uid, utc_text
from .repositories import many, one

SENDING_STALE_AFTER = timedelta(minutes=15)


def delivery_row(row):
    return DeliveryRecord(
        uid(row["delivery_id"]),
        uid(row["analysis_run_id"]),
        row["destination_key"],
        date(row["created_at"]),
        DeliveryStatus(row["status"]),
        date(row["started_at"]) if row["started_at"] else None,
        date(row["finished_at"]) if row["finished_at"] else None,
        row["attempt_count"],
        DeliveryFailureCategory(row["failure_category"]) if row["failure_category"] else None,
        row["channel"],
    )


class SQLiteDeliveryRepository:
    def __init__(self, database):
        self.database = database

    async def _recover(self, connection, at):
        utc(at)
        rows = await many(connection, "SELECT * FROM telegram_deliveries WHERE status='SENDING'")
        for raw in rows:
            record = delivery_row(raw)
            if record.started_at + SENDING_STALE_AFTER > at:
                continue
            if record.attempt_count == 0:
                # No HTTP attempt was started; this is safe to claim again.
                await connection.execute(
                    """UPDATE telegram_deliveries SET status='PENDING', started_at=NULL
                    WHERE delivery_id=? AND status='SENDING'""",
                    (str(record.delivery_id),),
                )
            else:
                # A request may have been accepted before the process died.
                await connection.execute(
                    """UPDATE telegram_deliveries SET status='AMBIGUOUS', finished_at=?,
                    failure_category=? WHERE delivery_id=? AND status='SENDING'""",
                    (
                        utc_text(at),
                        DeliveryFailureCategory.TELEGRAM_AMBIGUOUS.value,
                        str(record.delivery_id),
                    ),
                )

    async def recover_stale(self, at):
        async with self.database.transaction() as connection:
            await self._recover(connection, at)

    async def claim(self, record: DeliveryRecord, *, manual_retry=False):
        require(record.status is DeliveryStatus.PENDING, "Claim requires a pending delivery.")
        require(type(manual_retry) is bool, "Invalid manual retry flag.")
        async with self.database.transaction() as connection:
            await self._recover(connection, record.created_at)
            row = await one(
                connection,
                """SELECT * FROM telegram_deliveries
                WHERE analysis_run_id=? AND channel='TELEGRAM' AND destination_key=?""",
                (str(record.analysis_run_id), record.destination_key),
            )
            if row is None:
                await connection.execute(
                    """INSERT INTO telegram_deliveries
                    (delivery_id, analysis_run_id, channel, destination_key, created_at,
                     status, attempt_count) VALUES (?, ?, 'TELEGRAM', ?, ?, 'PENDING', 0)""",
                    (
                        str(record.delivery_id),
                        str(record.analysis_run_id),
                        record.destination_key,
                        utc_text(record.created_at),
                    ),
                )
                delivery_id = record.delivery_id
                active = True
            else:
                existing = delivery_row(row)
                delivery_id = existing.delivery_id
                active = existing.status is DeliveryStatus.PENDING or (
                    manual_retry and existing.status is DeliveryStatus.FAILED
                )
            if active:
                await connection.execute(
                    """UPDATE telegram_deliveries SET status='SENDING', started_at=?,
                    finished_at=NULL, failure_category=NULL WHERE delivery_id=?""",
                    (utc_text(record.created_at), str(delivery_id)),
                )
            saved = await one(
                connection,
                "SELECT * FROM telegram_deliveries WHERE delivery_id=?",
                (str(delivery_id),),
            )
            return delivery_row(saved), active

    async def start_attempt(self, delivery_id: UUID):
        identity(delivery_id)
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE telegram_deliveries SET attempt_count=attempt_count+1
                WHERE delivery_id=? AND status='SENDING'""",
                (str(delivery_id),),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Delivery is no longer sending.")
            saved = await one(
                connection,
                "SELECT * FROM telegram_deliveries WHERE delivery_id=?",
                (str(delivery_id),),
            )
            return delivery_row(saved)

    async def finish(self, delivery_id, at, status, category=None):
        identity(delivery_id)
        utc(at)
        require(
            status in {DeliveryStatus.DELIVERED, DeliveryStatus.FAILED, DeliveryStatus.AMBIGUOUS},
            "Invalid delivery outcome.",
        )
        if status is DeliveryStatus.DELIVERED:
            require(category is None, "Delivered notification cannot have failure category.")
        else:
            require(isinstance(category, DeliveryFailureCategory), "Sanitized failure required.")
        async with self.database.transaction() as connection:
            cursor = await connection.execute(
                """UPDATE telegram_deliveries SET status=?, finished_at=?, failure_category=?
                WHERE delivery_id=? AND status='SENDING' AND attempt_count>0""",
                (
                    status.value,
                    utc_text(at),
                    category.value if category else None,
                    str(delivery_id),
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError("Delivery is no longer sending.")
            saved = await one(
                connection,
                "SELECT * FROM telegram_deliveries WHERE delivery_id=?",
                (str(delivery_id),),
            )
            return delivery_row(saved)

    async def latest(self):
        async with self.database.connection() as connection:
            row = await one(
                connection,
                """SELECT * FROM telegram_deliveries
                ORDER BY created_at DESC, delivery_id DESC LIMIT 1""",
            )
            return delivery_row(row) if row else None

    async def get(self, analysis_run_id, destination_key):
        identity(analysis_run_id)
        async with self.database.connection() as connection:
            row = await one(
                connection,
                """SELECT * FROM telegram_deliveries
                WHERE analysis_run_id=? AND channel='TELEGRAM' AND destination_key=?""",
                (str(analysis_run_id), destination_key),
            )
            return delivery_row(row) if row else None
