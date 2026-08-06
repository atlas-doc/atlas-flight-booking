import socket
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def deny_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    real_connect = socket.socket.connect

    def deny_connect(sock: socket.socket, address: Any) -> None:
        # Windows implements socketpair(), which asyncio uses internally, with
        # a loopback TCP connection. Permit loopback so the event loop can be
        # created while continuing to reject all external network access.
        if isinstance(address, tuple) and address and address[0] in {"127.0.0.1", "::1"}:
            real_connect(sock, address)
            return
        raise AssertionError("automated tests must not access an external network")

    monkeypatch.setattr(socket.socket, "connect", deny_connect)
