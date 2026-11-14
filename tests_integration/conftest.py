from __future__ import annotations

import socket

import pytest


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    original_connect = socket.socket.connect
    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("Real external network access is forbidden in mocked integration tests")
    def blocked(*_args, **_kwargs):
        raise AssertionError("Real network access is forbidden in mocked integration tests")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
