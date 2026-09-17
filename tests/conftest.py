import socket

import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Fail fast on accidental networking; asyncio's local socketpair remains available."""
    monkeypatch.setenv("OPENAI_ENABLED", "false")

    def blocked(*args, **kwargs):
        raise AssertionError("Network access is prohibited in tests.")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
