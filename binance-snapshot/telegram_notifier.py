"""Explicit, read-only configuration loading and one-shot Telegram delivery."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import requests

_TOKEN_PATTERN = re.compile(r"[0-9]+:[A-Za-z0-9_-]+\Z")
_CHAT_ID_PATTERN = re.compile(r"-?[0-9]+\Z")
_TIMEOUT = (3.0, 10.0)  # connect, read; no automatic delivery retry


class TelegramConfigError(ValueError):
    """Telegram configuration is missing or invalid."""


class TelegramDeliveryError(RuntimeError):
    """Telegram did not confirm delivery of a message."""


@dataclass(frozen=True, slots=True)
class TelegramConfig:
    bot_token: str = field(repr=False)
    chat_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _TOKEN_PATTERN.fullmatch(self.bot_token):
            raise TelegramConfigError("TELEGRAM_BOT_TOKEN is missing or invalid.")
        if not _CHAT_ID_PATTERN.fullmatch(self.chat_id):
            raise TelegramConfigError("TELEGRAM_CHAT_ID is missing or invalid.")


def load_config(path: Path | None = None) -> TelegramConfig:
    """Use an explicit file, complete environment pair, or local .env, in that order."""
    if path is None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if token is not None or chat_id is not None:
            return TelegramConfig(bot_token=token or "", chat_id=chat_id or "")

    config_path = path if path is not None else Path(__file__).with_name(".env")
    try:
        contents = config_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise TelegramConfigError("Unable to read Telegram configuration.") from None

    values: dict[str, str] = {}
    for line in contents.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, raw_value = stripped.partition("=")
        key = key.strip()
        if separator != "=" or key not in {"TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"}:
            continue
        if key in values:
            raise TelegramConfigError(f"Duplicate {key} in Telegram configuration.")
        value = raw_value.strip()
        if value.startswith(("'", '"')) and len(value) >= 2:
            if value[-1] != value[0]:
                raise TelegramConfigError(f"Invalid {key} in Telegram configuration.")
            value = value[1:-1]
        else:
            value = value.split(" #", maxsplit=1)[0].strip()
        values[key] = value

    return TelegramConfig(
        bot_token=values.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=values.get("TELEGRAM_CHAT_ID", ""),
    )


def send_message(
    message: str,
    config: TelegramConfig,
    *,
    session: requests.Session | None = None,
) -> None:
    """Send one plain-text message; never retry an uncertain delivery."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError("Telegram message must contain text.")

    sender = session if session is not None else requests
    url = f"https://api.telegram.org/bot{config.bot_token}/sendMessage"
    try:
        response = sender.post(
            url,
            data={"chat_id": config.chat_id, "text": message},
            timeout=_TIMEOUT,
            allow_redirects=False,
        )
    except requests.Timeout:
        failure = "Telegram request timed out; delivery is unknown."
    except requests.RequestException:
        failure = "Telegram network request failed; delivery is unknown."
    else:
        if response.status_code != 200:
            raise TelegramDeliveryError("Telegram rejected the message.")
        try:
            payload = response.json()
        except ValueError:
            failure = "Telegram returned an invalid response; delivery is unknown."
        else:
            if not isinstance(payload, dict) or payload.get("ok") is not True:
                raise TelegramDeliveryError("Telegram rejected the message.")
            return
    raise TelegramDeliveryError(failure)
