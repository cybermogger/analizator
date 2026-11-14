from __future__ import annotations

import asyncio
import math
import os
import random
import socket
import time
from typing import Any

import aiohttp

from .schemas import AssetContext, BookSnapshot, Candle, SchemaError, parse_meta_and_contexts


class HyperliquidAPIError(RuntimeError):
    pass


class _StaticResolver(aiohttp.abc.AbstractResolver):
    """Optional operator-supplied DNS fallback; TLS still validates the official hostname."""

    def __init__(self, address: str):
        self.address = address

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[dict[str, Any]]:
        return [{"hostname": host, "host": self.address, "port": port, "family": socket.AF_INET, "proto": 0, "flags": 0}]

    async def close(self) -> None:
        return None


class HyperliquidREST:
    """Read-only adapter for the official Hyperliquid Info endpoint."""

    def __init__(self, url: str, timeout: float = 10, max_retries: int = 4, session: aiohttp.ClientSession | None = None):
        if not 0 < timeout <= 10:
            raise ValueError("Hyperliquid HTTP timeout must be > 0 and <= 10 seconds")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self.url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self._session = session
        self._owns_session = session is None
        self.received_at_ms_by_type: dict[str, int] = {}

    async def __aenter__(self) -> "HyperliquidREST":
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout, connect=min(self.timeout, 5))
            resolved_ip = os.getenv("HYPERLIQUID_RESOLVED_IP")
            connector = aiohttp.TCPConnector(resolver=_StaticResolver(resolved_ip)) if resolved_ip else None
            self._session = aiohttp.ClientSession(timeout=timeout, connector=connector, headers={"User-Agent": "hl-readonly-analyzer/1.0"})
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _post(self, body: dict[str, Any]) -> Any:
        if self._session is None:
            raise RuntimeError("HyperliquidREST must be used as an async context manager")
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self._session.post(self.url, json=body) as response:
                    if response.status == 429 or response.status >= 500:
                        text = await response.text()
                        raise HyperliquidAPIError(f"HTTP {response.status}: {text[:300]}")
                    if response.status != 200:
                        text = await response.text()
                        raise HyperliquidAPIError(f"HTTP {response.status}: {text[:500]}")
                    payload = await response.json(content_type=None)
                    self.received_at_ms_by_type[str(body.get("type", "unknown"))] = int(time.time() * 1000)
                    return payload
            except (aiohttp.ClientError, asyncio.TimeoutError, HyperliquidAPIError) as exc:
                last_error = exc
                if attempt == self.max_retries:
                    break
                await asyncio.sleep(min(8.0, 0.4 * (2**attempt)) + random.random() * 0.2)
        raise HyperliquidAPIError(f"Info request failed after retries: {body.get('type')}: {last_error}") from last_error

    async def all_mids(self) -> dict[str, float]:
        payload = await self._post({"type": "allMids"})
        self.received_at_ms_by_type["allMids"] = int(time.time() * 1000)
        if not isinstance(payload, dict):
            raise SchemaError("allMids response must be an object")
        mids = {str(k): float(v) for k, v in payload.items()}
        if any(not coin or not math.isfinite(price) or price <= 0 for coin, price in mids.items()):
            raise SchemaError("allMids contains an invalid coin or price")
        return mids

    async def meta_and_asset_contexts(self) -> tuple[dict[str, Any], dict[str, AssetContext]]:
        payload = await self._post({"type": "metaAndAssetCtxs"})
        received_at_ms = int(time.time() * 1000)
        self.received_at_ms_by_type["metaAndAssetCtxs"] = received_at_ms
        return parse_meta_and_contexts(payload, received_at_ms)

    async def candles(self, coin: str, interval: str, start_time: int, end_time: int | None = None) -> list[Candle]:
        request: dict[str, Any] = {"coin": coin, "interval": interval, "startTime": int(start_time)}
        if end_time is not None:
            request["endTime"] = int(end_time)
        payload = await self._post({"type": "candleSnapshot", "req": request})
        received_at_ms = int(time.time() * 1000)
        self.received_at_ms_by_type[f"candleSnapshot:{coin}:{interval}"] = received_at_ms
        if not isinstance(payload, list):
            raise SchemaError("candleSnapshot response must be a list")
        return [Candle.from_api(item, received_at_ms) for item in payload]

    async def l2_book(self, coin: str) -> BookSnapshot:
        payload = await self._post({"type": "l2Book", "coin": coin})
        received_at_ms = int(time.time() * 1000)
        self.received_at_ms_by_type[f"l2Book:{coin}"] = received_at_ms
        if not isinstance(payload, dict):
            raise SchemaError("l2Book response must be an object")
        return BookSnapshot.from_api(payload, received_at_ms)
