"""Explicit cloud HTTP launcher; ASGI construction remains side-effect free."""

import os

import uvicorn


def main() -> None:
    raw_port = os.environ.get("PORT", "")
    if not raw_port.isascii() or not raw_port.isdigit() or not 1 <= int(raw_port) <= 65535:
        raise SystemExit("PORT must be an integer from 1 to 65535.")
    uvicorn.run(
        "advanced_trading_advisor.http.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=int(raw_port),
    )


if __name__ == "__main__":
    main()
