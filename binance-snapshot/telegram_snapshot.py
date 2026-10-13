"""Explicit opt-in command for sending one complete Binance snapshot to Telegram."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

import requests
from market_data import BinanceClient, collect_snapshot
from models import MarketDataError
from snapshot import SYMBOLS
from telegram_notifier import (
    TelegramConfigError,
    TelegramDeliveryError,
    load_config,
    send_message,
)
from telegram_snapshot_formatter import TelegramSnapshotFormatError, format_telegram_snapshot


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one Binance Spot snapshot to Telegram.")
    parser.add_argument("--send", action="store_true", help="explicitly send one snapshot")
    arguments = parser.parse_args(argv)
    if not arguments.send:
        print("No message sent. Pass --send to deliver one snapshot.", file=sys.stderr)
        return 2

    try:
        with requests.Session() as session:
            snapshot = collect_snapshot(BinanceClient(session), SYMBOLS, datetime.now(UTC))
        message = format_telegram_snapshot(snapshot)
        config = load_config()
        send_message(message, config)
    except (
        MarketDataError,
        TelegramSnapshotFormatError,
        TelegramConfigError,
        TelegramDeliveryError,
    ) as error:
        print(f"Telegram snapshot unavailable: {error}", file=sys.stderr)
        return 1
    print("Telegram snapshot delivered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
