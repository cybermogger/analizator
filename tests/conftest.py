from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
import socket


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    """Unit tests are guaranteed offline; live checks belong to bot.py --smoke-test."""
    original_connect = socket.socket.connect
    def guarded_connect(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in {"127.0.0.1", "::1"}:
            return original_connect(sock, address)
        raise AssertionError("Network access is forbidden in unit tests")
    def blocked(*_args, **_kwargs):
        raise AssertionError("External network access is forbidden in unit tests")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
def candles() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    count = 900
    returns = rng.normal(0.00015, 0.006, count)
    close = 100 * np.exp(np.cumsum(returns))
    open_ = np.r_[close[0], close[:-1]]
    spread = rng.uniform(0.002, 0.012, count) * close
    high = np.maximum(open_, close) + spread
    low = np.minimum(open_, close) - spread
    start = 1_700_000_000_000
    return pd.DataFrame({
        "coin": "SOL", "interval": "5m", "open_time": start + np.arange(count) * 300_000,
        "close_time": start + (np.arange(count) + 1) * 300_000 - 1,
        "open": open_, "high": high, "low": low, "close": close,
        "volume": rng.lognormal(5, 0.7, count), "trades": rng.integers(20, 200, count),
    })
