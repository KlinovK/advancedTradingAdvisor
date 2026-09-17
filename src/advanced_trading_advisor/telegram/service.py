"""Deliver only persisted final analyses; delivery failures never mutate analysis runs."""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from advanced_trading_advisor.analysis.models import AnalysisRunStatus
from advanced_trading_advisor.analysis.result import AnalysisResultCodec
from advanced_trading_advisor.delivery.models import (
    DeliveryFailureCategory as Failure,
)
from advanced_trading_advisor.delivery.models import (
    DeliveryRecord,
    DeliveryStatus,
)
from advanced_trading_advisor.delivery.repositories import DeliveryRepository, TextSender
from advanced_trading_advisor.domain.persistence import PersistenceError

from .formatter import format_analysis
from .transport import TelegramSendError

MAX_ATTEMPTS = 3
TEST_MESSAGE = "Advanced Trading Advisor Telegram connection test. No recommendations or trades."


class TelegramDeliveryService:
    def __init__(
        self,
        settings,
        repository: DeliveryRepository,
        analysis_runs,
        sender: TextSender,
        *,
        now=lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
    ):
        self.settings, self.repository, self.analysis_runs, self.sender = (
            settings,
            repository,
            analysis_runs,
            sender,
        )
        self.now, self.sleep = now, sleep

    async def deliver(self, run_id, *, manual_retry=False):
        if not self.settings.enabled:
            raise ValueError("Telegram delivery is disabled.")
        stored = await self.analysis_runs.get(run_id)
        if stored is None or stored.run.status is not AnalysisRunStatus.COMPLETED:
            raise PersistenceError("Only a persisted completed analysis can be delivered.")
        if not stored.result_json:
            raise PersistenceError("Completed analysis has no persisted result.")
        result = AnalysisResultCodec.decode(stored.result_json)
        if (
            result.run_id != run_id
            or result.snapshot_id != stored.run.snapshot_id
            or result.generated_at != stored.run.completed_at
            or result.market_cutoff != stored.run.market_cutoff
        ):
            raise PersistenceError("Persisted analysis identity is inconsistent.")
        message = format_analysis(result)
        proposal = DeliveryRecord(uuid4(), run_id, self.settings.destination_key, self.now())
        record, claimed = await self.repository.claim(proposal, manual_retry=manual_retry)
        if not claimed:
            return record
        for attempt in range(MAX_ATTEMPTS):
            try:
                await self.repository.start_attempt(record.delivery_id)
                await self.sender.send(message)
            except asyncio.CancelledError:
                try:
                    await self.repository.finish(
                        record.delivery_id,
                        self.now(),
                        DeliveryStatus.AMBIGUOUS,
                        Failure.TELEGRAM_AMBIGUOUS,
                    )
                except Exception:
                    pass  # Stale SENDING recovery is the backstop.
                raise
            except TelegramSendError as error:
                if error.ambiguous:
                    return await self.repository.finish(
                        record.delivery_id,
                        self.now(),
                        DeliveryStatus.AMBIGUOUS,
                        Failure.TELEGRAM_AMBIGUOUS,
                    )
                if error.retryable and attempt + 1 < MAX_ATTEMPTS:
                    await self.sleep(error.retry_after or attempt + 1)
                    continue
                return await self.repository.finish(
                    record.delivery_id, self.now(), DeliveryStatus.FAILED, error.category
                )
            except PersistenceError:
                raise
            except Exception:
                return await self.repository.finish(
                    record.delivery_id,
                    self.now(),
                    DeliveryStatus.AMBIGUOUS,
                    Failure.TELEGRAM_AMBIGUOUS,
                )
            return await self.repository.finish(
                record.delivery_id, self.now(), DeliveryStatus.DELIVERED
            )
        raise AssertionError("Bounded Telegram delivery loop exhausted unexpectedly.")

    async def test_connection(self):
        if not self.settings.enabled:
            raise ValueError("Telegram delivery is disabled.")
        for attempt in range(MAX_ATTEMPTS):
            try:
                await self.sender.send(TEST_MESSAGE)
                return
            except TelegramSendError as error:
                if error.retryable and attempt + 1 < MAX_ATTEMPTS:
                    await self.sleep(error.retry_after or attempt + 1)
                    continue
                raise TelegramSendError(error.category) from None
