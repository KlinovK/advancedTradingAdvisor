import re
from dataclasses import dataclass, field
from hashlib import sha256

from advanced_trading_advisor.configuration import local_environment


class TelegramConfigurationError(ValueError):
    pass


def boolean(env, key, default="false"):
    raw = env.get(key, default).lower()
    if raw not in {"true", "false"}:
        raise TelegramConfigurationError(f"{key} must be true or false.")
    return raw == "true"


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    enabled: bool = False
    bot_token: str = field(default="", repr=False)
    chat_id: str = field(default="", repr=False)
    timeout_seconds: int = 10
    disable_notification: bool = False

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.disable_notification) is not bool:
            raise TelegramConfigurationError("Invalid Telegram boolean configuration.")
        if type(self.timeout_seconds) is not int or not 1 <= self.timeout_seconds <= 60:
            raise TelegramConfigurationError("TELEGRAM_TIMEOUT_SECONDS must be 1–60.")
        if self.enabled:
            if not isinstance(self.bot_token, str) or not re.fullmatch(
                r"[0-9]{1,20}:[A-Za-z0-9_-]{20,}", self.bot_token
            ):
                raise TelegramConfigurationError("TELEGRAM_BOT_TOKEN is missing or invalid.")
            if not isinstance(self.chat_id, str) or not (
                re.fullmatch(r"-?[1-9][0-9]{0,19}", self.chat_id)
                or re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", self.chat_id)
            ):
                raise TelegramConfigurationError("TELEGRAM_CHAT_ID is missing or invalid.")

    @property
    def destination_key(self):
        return sha256(self.chat_id.encode("utf-8")).hexdigest()

    @classmethod
    def from_environment(cls, environment=None):
        env = environment if environment is not None else local_environment()
        timeout = env.get("TELEGRAM_TIMEOUT_SECONDS", "10")
        if not timeout.isascii() or not timeout.isdigit() or len(timeout) > 2:
            raise TelegramConfigurationError("Invalid TELEGRAM_TIMEOUT_SECONDS.")
        return cls(
            boolean(env, "TELEGRAM_ENABLED"),
            env.get("TELEGRAM_BOT_TOKEN", ""),
            env.get("TELEGRAM_CHAT_ID", ""),
            int(timeout),
            boolean(env, "TELEGRAM_DISABLE_NOTIFICATION"),
        )
