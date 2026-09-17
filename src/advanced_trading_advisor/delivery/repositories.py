from typing import Protocol
from uuid import UUID

from .models import DeliveryFailureCategory, DeliveryRecord, DeliveryStatus


class DeliveryRepository(Protocol):
    async def claim(
        self, record: DeliveryRecord, *, manual_retry: bool = False
    ) -> tuple[DeliveryRecord, bool]: ...

    async def start_attempt(self, delivery_id: UUID) -> DeliveryRecord: ...

    async def finish(
        self,
        delivery_id: UUID,
        at,
        status: DeliveryStatus,
        category: DeliveryFailureCategory | None = None,
    ) -> DeliveryRecord: ...

    async def latest(self) -> DeliveryRecord | None: ...

    async def recover_stale(self, at) -> None: ...


class TextSender(Protocol):
    async def send(self, text: str) -> None: ...
