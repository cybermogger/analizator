from __future__ import annotations

import asyncio

import pytest

from src.hyperliquid.websocket import HyperliquidWebSocket


def test_subscription_contracts_are_exact():
    client = HyperliquidWebSocket("wss://api.hyperliquid.xyz/ws", ["SOL", "HYPE", "BTC"], ["1m", "5m", "15m", "30m", "1h", "4h"])
    subscriptions = client.subscriptions()
    assert subscriptions[0] == {"type": "allMids"}
    assert len(subscriptions) == 31
    for coin in ("SOL", "HYPE", "BTC"):
        assert {"type": "trades", "coin": coin} in subscriptions
        assert {"type": "l2Book", "coin": coin} in subscriptions
        assert {"type": "bbo", "coin": coin} in subscriptions
        assert {"type": "activeAssetCtx", "coin": coin} in subscriptions
        for interval in ("1m", "5m", "15m", "30m", "1h", "4h"):
            assert {"type": "candle", "coin": coin, "interval": interval} in subscriptions


@pytest.mark.asyncio
async def test_reconnect_loop_survives_connection_failures(monkeypatch):
    attempts = 0

    class FailedConnection:
        async def __aenter__(self):
            nonlocal attempts
            attempts += 1
            raise OSError("network disabled")
        async def __aexit__(self, *_args): return None

    monkeypatch.setattr("src.hyperliquid.websocket.websockets.connect", lambda *_args, **_kwargs: FailedConnection())
    client = HyperliquidWebSocket("wss://api.hyperliquid.xyz/ws", ["SOL"], ["1m"])
    task = asyncio.create_task(client.run_forever())
    try:
        deadline = asyncio.get_running_loop().time() + 3
        while attempts < 2 and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert attempts >= 2
        assert client.state.connected is False
    finally:
        await client.stop()
        await asyncio.wait_for(task, timeout=2)
