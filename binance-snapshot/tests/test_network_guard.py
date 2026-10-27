"""The Snapshot test suite must reject accidental Python socket traffic."""

import socket

import pytest


@pytest.mark.parametrize("api", ["create_connection", "connect", "connect_ex", "sendto"])
def test_network_guard_blocks_normal_socket_apis(api: str) -> None:
    with pytest.raises(AssertionError, match="Snapshot tests must not open network connections"):
        if api == "create_connection":
            socket.create_connection(("offline.invalid", 443))
        else:
            with socket.socket() as client:
                if api == "sendto":
                    client.sendto(b"offline", ("127.0.0.1", 9))
                else:
                    getattr(client, api)(("127.0.0.1", 9))
