"""Offline Telegram delivery and configuration tests."""

from __future__ import annotations

import logging
import traceback
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests
import telegram_notifier
from telegram_notifier import (
    TelegramConfig,
    TelegramConfigError,
    TelegramDeliveryError,
    load_config,
    send_message,
)

TOKEN = "123456:FAKE_test-token"
CHAT_ID = "-1001234567890"


@pytest.fixture
def isolated_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Keep default config reads inside a temporary directory, never the real .env."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(telegram_notifier, "__file__", str(tmp_path / "telegram_notifier.py"))
    return tmp_path


def write_config(path: Path, *, token: str | None = TOKEN, chat_id: str | None = CHAT_ID) -> Path:
    lines = ["# offline fixture"]
    if token is not None:
        lines.append(f'TELEGRAM_BOT_TOKEN="{token}"')
    if chat_id is not None:
        lines.append(f"TELEGRAM_CHAT_ID={chat_id}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def response(status: int, payload: object) -> Mock:
    result = Mock()
    result.status_code = status
    result.json.return_value = payload
    return result


def test_valid_configuration_from_temporary_file(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / ".env"))
    assert config.bot_token == TOKEN
    assert config.chat_id == CHAT_ID
    assert TOKEN not in repr(config)
    assert CHAT_ID not in repr(config)


def test_environment_pair_is_used_without_a_file(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    config = load_config()
    assert config == TelegramConfig(TOKEN, CHAT_ID)
    assert not (isolated_config / ".env").exists()


def test_absent_environment_falls_back_to_local_file(isolated_config: Path) -> None:
    write_config(isolated_config / ".env")
    assert load_config() == TelegramConfig(TOKEN, CHAT_ID)


@pytest.mark.parametrize(
    "values,missing_key",
    [
        ({"TELEGRAM_BOT_TOKEN": TOKEN}, "TELEGRAM_CHAT_ID"),
        ({"TELEGRAM_CHAT_ID": CHAT_ID}, "TELEGRAM_BOT_TOKEN"),
        ({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": CHAT_ID}, "TELEGRAM_BOT_TOKEN"),
        ({"TELEGRAM_BOT_TOKEN": TOKEN, "TELEGRAM_CHAT_ID": ""}, "TELEGRAM_CHAT_ID"),
    ],
)
def test_partial_or_empty_environment_fails_without_file_fallback(
    isolated_config: Path,
    monkeypatch: pytest.MonkeyPatch,
    values: dict[str, str],
    missing_key: str,
) -> None:
    write_config(isolated_config / ".env")
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(TelegramConfigError, match=missing_key):
        load_config()


def test_environment_pair_takes_precedence_over_local_file(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_config(isolated_config / ".env", token="999999:FAKE_file-token", chat_id="123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    assert load_config() == TelegramConfig(TOKEN, CHAT_ID)


def test_explicit_path_takes_precedence_over_environment(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit = write_config(
        isolated_config / "explicit.env", token="999999:FAKE_file-token", chat_id="123"
    )
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    assert load_config(explicit) == TelegramConfig("999999:FAKE_file-token", "123")


@pytest.mark.parametrize(
    "key,value",
    [
        ("TELEGRAM_BOT_TOKEN", "not-a-token"),
        ("TELEGRAM_CHAT_ID", "not-a-chat-id"),
    ],
)
def test_invalid_environment_value_uses_existing_validation(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch, key: str, value: str
) -> None:
    write_config(isolated_config / ".env")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", TOKEN)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    monkeypatch.setenv(key, value)
    with pytest.raises(TelegramConfigError, match=key):
        load_config()


def test_environment_errors_never_expose_values(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "not-a-valid-token-secret"
    write_config(isolated_config / ".env")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", secret)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", CHAT_ID)
    with caplog.at_level(logging.DEBUG), pytest.raises(TelegramConfigError) as caught:
        load_config()
    exposed = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert secret not in exposed
    assert CHAT_ID not in exposed
    assert not caplog.records


def test_missing_token_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TelegramConfigError, match="TELEGRAM_BOT_TOKEN"):
        load_config(write_config(tmp_path / ".env", token=None))


def test_missing_chat_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(TelegramConfigError, match="TELEGRAM_CHAT_ID"):
        load_config(write_config(tmp_path / ".env", chat_id=None))


@pytest.mark.parametrize("token,chat_id", [("bad/token", CHAT_ID), (TOKEN, "not-an-id")])
def test_invalid_configuration_is_rejected(token: str, chat_id: str) -> None:
    with pytest.raises(TelegramConfigError):
        TelegramConfig(token, chat_id)


def test_missing_configuration_file_has_safe_error(tmp_path: Path) -> None:
    with pytest.raises(TelegramConfigError, match="Unable to read") as caught:
        load_config(tmp_path / "missing.env")
    assert str(tmp_path) not in str(caught.value)


def test_successful_delivery_uses_https_plain_text_and_bounded_timeout() -> None:
    session = Mock(spec=requests.Session)
    session.post.return_value = response(200, {"ok": True, "result": {"message_id": 1}})
    config = TelegramConfig(TOKEN, CHAT_ID)
    assert send_message("offline alert", config, session=session) is None
    session.post.assert_called_once_with(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": "offline alert"},
        timeout=(3.0, 10.0),
    )


@pytest.mark.parametrize("status,payload", [(403, {"ok": False}), (200, {"ok": False})])
def test_telegram_api_rejection_is_safe_and_not_retried(status: int, payload: object) -> None:
    session = Mock(spec=requests.Session)
    session.post.return_value = response(status, payload)
    with pytest.raises(TelegramDeliveryError, match="rejected") as caught:
        send_message("offline alert", TelegramConfig(TOKEN, CHAT_ID), session=session)
    assert TOKEN not in str(caught.value)
    assert CHAT_ID not in str(caught.value)
    session.post.assert_called_once()


def test_network_timeout_is_safe_and_not_retried() -> None:
    session = Mock(spec=requests.Session)
    session.post.side_effect = requests.Timeout(f"request URL contained {TOKEN}")
    with pytest.raises(TelegramDeliveryError, match="timed out") as caught:
        send_message("offline alert", TelegramConfig(TOKEN, CHAT_ID), session=session)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__cause__ is None
    session.post.assert_called_once()


def test_network_failure_is_safe_and_not_retried() -> None:
    session = Mock(spec=requests.Session)
    session.post.side_effect = requests.ConnectionError(f"request URL contained {TOKEN}")
    with pytest.raises(TelegramDeliveryError, match="network request failed") as caught:
        send_message("offline alert", TelegramConfig(TOKEN, CHAT_ID), session=session)
    assert TOKEN not in "".join(traceback.format_exception(caught.value))
    session.post.assert_called_once()


def test_secret_redaction_in_exceptions_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    session = Mock(spec=requests.Session)
    error = requests.ConnectionError(f"token={TOKEN}, chat={CHAT_ID}")
    session.post.side_effect = error
    with caplog.at_level(logging.DEBUG), pytest.raises(TelegramDeliveryError) as caught:
        send_message("offline alert", TelegramConfig(TOKEN, CHAT_ID), session=session)
    exposed = "".join(traceback.format_exception(caught.value)) + caplog.text
    assert TOKEN not in exposed
    assert CHAT_ID not in exposed
    assert not caplog.records


def test_empty_message_is_rejected_without_http() -> None:
    session = Mock(spec=requests.Session)
    with pytest.raises(ValueError, match="must contain text"):
        send_message("  ", TelegramConfig(TOKEN, CHAT_ID), session=session)
    session.post.assert_not_called()
