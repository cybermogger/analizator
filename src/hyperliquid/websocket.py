from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import socket
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

import websockets

from .schemas import AssetContext, BboSnapshot, BookSnapshot, Candle, SchemaError, Trade, parse_ws_message


@dataclass
class LiveMarketState:
    books: dict[str, BookSnapshot] = field(default_factory=dict)
    bbo: dict[str, BboSnapshot] = field(default_factory=dict)
    contexts: dict[str, AssetContext] = field(default_factory=dict)
    candles: dict[tuple[str, str], Candle] = field(default_factory=dict)
    trades: dict[str, deque[Trade]] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=100_000)))
    trade_keys: dict[str, set[tuple[int, int]]] = field(default_factory=lambda: defaultdict(set))
    mids: dict[str, float] = field(default_factory=dict)
    book_received_at_ms: dict[str, int] = field(default_factory=dict)
    candle_received_at_ms: dict[tuple[str, str], int] = field(default_factory=dict)
    context_received_at_ms: dict[str, int] = field(default_factory=dict)
    trade_received_at_ms: dict[str, int] = field(default_factory=dict)
    bbo_received_at_ms: dict[str, int] = field(default_factory=dict)
    mids_received_at_ms: int = 0
    connected: bool = False
    last_update_ms: int = 0

    def apply(self, channel: str, data: Any, received_at_ms: int | None = None) -> Any:
        received = int(received_at_ms or time.time() * 1000)
        self.last_update_ms = received
        if channel == "trades":
            accepted: list[Trade] = []
            for trade in data:
                trade = replace(trade, received_at_ms=received)
                key = (trade.timestamp, trade.trade_id)
                keys = self.trade_keys[trade.coin]
                queue = self.trades[trade.coin]
                if key in keys:
                    continue
                if queue.maxlen is not None and len(queue) >= queue.maxlen:
                    evicted = queue.popleft()
                    keys.discard((evicted.timestamp, evicted.trade_id))
                queue.append(trade)
                keys.add(key)
                accepted.append(trade)
                self.trade_received_at_ms[trade.coin] = received
            return accepted
        elif channel == "l2Book":
            data = replace(data, received_at_ms=received)
            self.books[data.coin] = data
            self.book_received_at_ms[data.coin] = received
        elif channel == "candle":
            normalized = []
            for candle in data:
                candle = replace(candle, received_at_ms=received)
                self.candles[(candle.coin, candle.interval)] = candle
                self.candle_received_at_ms[(candle.coin, candle.interval)] = received
                normalized.append(candle)
            data = normalized
        elif channel == "bbo" and isinstance(data, BboSnapshot):
            data = replace(data, received_at_ms=received)
            self.bbo[data.coin] = data
            self.bbo_received_at_ms[data.coin] = received
        elif channel == "allMids" and isinstance(data, dict):
            self.mids = {str(k): float(v) for k, v in data.items()}
            self.mids_received_at_ms = received
        elif channel == "activeAssetCtx" and isinstance(data, AssetContext):
            data = replace(data, received_at_ms=received)
            self.contexts[data.coin] = data
            self.context_received_at_ms[data.coin] = received
        return data


class HyperliquidWebSocket:
    """Reconnectable official WebSocket market-data consumer."""

    def __init__(
        self,
        url: str,
        coins: list[str],
        intervals: list[str],
        state: LiveMarketState | None = None,
        on_event: Callable[[str, Any], Awaitable[None]] | None = None,
    ):
        self.url = url
        self.coins = coins
        self.intervals = intervals
        self.state = state or LiveMarketState()
        self.on_event = on_event
        self._stop = asyncio.Event()

    def subscriptions(self) -> list[dict[str, Any]]:
        subscriptions: list[dict[str, Any]] = [{"type": "allMids"}]
        for coin in self.coins:
            subscriptions.extend([
                {"type": "trades", "coin": coin}, {"type": "l2Book", "coin": coin},
                {"type": "bbo", "coin": coin}, {"type": "activeAssetCtx", "coin": coin},
            ])
            subscriptions.extend({"type": "candle", "coin": coin, "interval": interval} for interval in self.intervals)
        return subscriptions

    async def stop(self) -> None:
        self._stop.set()

    async def run_forever(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            direct_socket: socket.socket | None = None
            try:
                connect_kwargs: dict[str, Any] = {"open_timeout": 10, "ping_interval": 20, "ping_timeout": 20, "max_size": 8_000_000}
                resolved_ip = os.getenv("HYPERLIQUID_RESOLVED_IP")
                if resolved_ip:
                    direct_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    direct_socket.setblocking(False)
                    await asyncio.wait_for(
                        asyncio.get_running_loop().sock_connect(direct_socket, (resolved_ip, 443)),
                        timeout=10,
                    )
                    connect_kwargs.update(sock=direct_socket, server_hostname="api.hyperliquid.xyz")
                async with websockets.connect(self.url, **connect_kwargs) as ws:
                    for sub in self.subscriptions():
                        await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))
                    self.state.connected = True
                    attempt = 0
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        if self._stop.is_set():
                            break
                        try:
                            received_at_ms = int(time.time() * 1000)
                            channel, data = parse_ws_message(json.loads(raw), received_at_ms=received_at_ms)
                            if channel == "subscriptionResponse":
                                continue
                            applied = self.state.apply(channel, data, received_at_ms=received_at_ms)
                            if self.on_event is not None:
                                await self.on_event(channel, applied)
                        except (json.JSONDecodeError, SchemaError, TypeError, ValueError):
                            continue
            except asyncio.CancelledError:
                raise
            except Exception:
                self.state.connected = False
                delay = min(30.0, 0.5 * 2**attempt) + random.random() * 0.5
                attempt = min(attempt + 1, 8)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
            finally:
                self.state.connected = False
                if direct_socket is not None:
                    direct_socket.close()
