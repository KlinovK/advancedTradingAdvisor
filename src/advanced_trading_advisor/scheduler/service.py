import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from advanced_trading_advisor.domain.persistence import (
    ConflictError,
    FailureCategory,
    PersistenceError,
)
from advanced_trading_advisor.domain.validation import require

from .models import ScheduledExecution, ScheduledStatus
from .policy import SchedulerPolicy
from .repositories import SchedulerRepository


class Scheduler:
    def __init__(
        self,
        settings,
        repository: SchedulerRepository,
        analysis_runs,
        analysis_factory,
        *,
        now=lambda: datetime.now(UTC),
        sleep=asyncio.sleep,
        output=print,
        on_completed=None,
    ):
        self.policy = SchedulerPolicy(settings)
        self.repository, self.analysis_runs, self.analysis_factory = (
            repository,
            analysis_runs,
            analysis_factory,
        )
        self.now, self.sleep, self.output = now, sleep, output
        self.on_completed = on_completed

    async def once(self, scheduled_for=None):
        at = self.now()
        slot = scheduled_for or self.policy.window(at).previous_run
        record = ScheduledExecution(slot, self.policy.expected_cutoff(slot), uuid4(), at)
        try:
            claimed = await self.repository.claim(record)
        except asyncio.CancelledError:
            # Commit may have won a race with cancellation. Finalize only our own token.
            try:
                current = await self.repository.get(slot)
                if current and current.owner_token == record.owner_token:
                    await self.repository.finish(
                        record, self.now(), ScheduledStatus.FAILED, FailureCategory.INTERRUPTED
                    )
            except Exception:
                # Recovery owns an unfinished slot if cancellation interrupts cleanup.
                pass
            raise
        if not claimed:
            self.output(f"[{slot.isoformat()}] Already recorded; not repeated.")
            return await self.repository.get(slot)
        try:
            self.output(f"[{slot.isoformat()}] Starting scheduled analysis...")
            analysis = self.analysis_factory(record)
            result = await analysis.execute()
        except asyncio.CancelledError:
            try:
                await self.repository.finish(
                    record, self.now(), ScheduledStatus.FAILED, FailureCategory.INTERRUPTED
                )
            except Exception:
                # Never replace cancellation with a cleanup failure.
                pass
            raise
        except Exception as error:
            stored = await self.analysis_runs.get(record.owner_token)
            category = (
                stored.failure_category
                if stored and stored.failure_category
                else FailureCategory.ANALYSIS_BUSY
                if isinstance(error, ConflictError)
                else FailureCategory.CONFIGURATION
                if isinstance(error, ValueError)
                else FailureCategory.DATABASE
                if isinstance(error, PersistenceError)
                else FailureCategory.INVALID_INPUT
                if stored is None
                else FailureCategory.ANALYSIS_FAILED
            )
            status = (
                ScheduledStatus.SKIPPED
                if category is FailureCategory.ANALYSIS_BUSY
                else ScheduledStatus.FAILED
            )
            final = await self.repository.finish(record, self.now(), status, category)
            detail = (
                final.failure_category.value
                if final.failure_category
                else "saved analysis recovered"
            )
            self.output(f"[{slot.isoformat()}] {final.status.value}: {detail}")
            return final
        # Completion is reconciled against the real durable analysis in the repository.
        # Cancellation/crash here leaves an auditable RUNNING slot for stale recovery.
        final = await self.repository.finish(record, self.now(), ScheduledStatus.COMPLETED)
        self.output(
            f"[{slot.isoformat()}] Completed\nAnalysis: {result.run_id}\n"
            f"Regime: {result.market_regime.value}\nReasoning: {result.reasoning_mode.value}"
        )
        if self.on_completed is not None:
            try:
                await self.on_completed(final.analysis_run_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Delivery owns its separate failure state; the slot remains COMPLETED.
                self.output("Telegram delivery unavailable; analysis remains completed.")
        return final

    async def run(self):
        require(self.policy.settings.enabled, "Scheduler disabled; set SCHEDULER_ENABLED=true.")
        self.output("ADVANCED TRADING ADVISOR SCHEDULER")
        await self.repository.recover(self.now())
        catchup = self.policy.catchup(self.now())
        if catchup is not None:
            await self.once(catchup)
        while True:
            target = self.policy.window(self.now()).next_run
            self.output(f"Next run: {target.isoformat()}")
            while self.now() < target:
                await self.sleep(min(60, (target - self.now()).total_seconds()))
            # After machine suspension/large clock jumps, never backfill an old target.
            # At most the current recent slot is eligible; no historical queue.
            due = self.policy.catchup(self.now())
            if due is not None:
                await self.once(due)
