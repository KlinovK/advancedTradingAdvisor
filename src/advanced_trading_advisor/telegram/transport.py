"""Outbound Telegram Bot API transport; raw HTTP details never leave this module."""

import httpx

from advanced_trading_advisor.delivery.models import DeliveryFailureCategory as Failure


class TelegramSendError(RuntimeError):
    def __init__(self, category: Failure, *, retryable=False, ambiguous=False, retry_after=0):
        super().__init__(category.value)
        self.category = category
        self.retryable = retryable
        self.ambiguous = ambiguous
        self.retry_after = retry_after


class TelegramTransport:
    def __init__(self, settings, *, transport=None):
        self.settings = settings
        self.transport = transport

    async def send(self, text: str) -> None:
        payload = {
            "chat_id": self.settings.chat_id,
            "text": text,
            "disable_notification": self.settings.disable_notification,
            "link_preview_options": {"is_disabled": True},
        }
        url = f"https://api.telegram.org/bot{self.settings.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=httpx.Timeout(self.settings.timeout_seconds),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = await client.post(url, json=payload)
        except httpx.ConnectTimeout:
            raise TelegramSendError(Failure.TELEGRAM_TIMEOUT, retryable=True) from None
        except httpx.ConnectError:
            raise TelegramSendError(Failure.TELEGRAM_TRANSPORT, retryable=True) from None
        except httpx.HTTPError:
            # A request might have reached Telegram before the response was lost.
            raise TelegramSendError(Failure.TELEGRAM_AMBIGUOUS, ambiguous=True) from None

        if response.status_code == 401:
            raise TelegramSendError(Failure.TELEGRAM_UNAUTHORIZED)
        if response.status_code == 403:
            raise TelegramSendError(Failure.TELEGRAM_FORBIDDEN)
        if response.status_code == 400:
            raise TelegramSendError(Failure.TELEGRAM_BAD_REQUEST)
        if response.status_code == 429:
            raw = response.headers.get("Retry-After", "")
            retry_after = int(raw) if raw.isascii() and raw.isdigit() and len(raw) == 1 else 0
            if not retry_after:
                try:
                    body = response.json()
                    value = body.get("parameters", {}).get("retry_after")
                    retry_after = value if type(value) is int and 1 <= value <= 5 else 0
                except (ValueError, AttributeError, TypeError):
                    retry_after = 0
            raise TelegramSendError(
                Failure.TELEGRAM_RATE_LIMITED,
                retryable=1 <= retry_after <= 5,
                retry_after=retry_after,
            )
        if 500 <= response.status_code <= 599:
            raise TelegramSendError(Failure.TELEGRAM_PROVIDER, retryable=True)
        if response.status_code != 200:
            raise TelegramSendError(Failure.TELEGRAM_PROVIDER)
        try:
            body = response.json()
            valid = (
                isinstance(body, dict)
                and body.get("ok") is True
                and isinstance(body.get("result"), dict)
                and type(body["result"].get("message_id")) is int
                and body["result"]["message_id"] > 0
            )
        except ValueError:
            valid = False
        if not valid:
            raise TelegramSendError(Failure.TELEGRAM_AMBIGUOUS, ambiguous=True)
