from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping

import aiohttp

from .base import ProviderError


class PublicHTTPClient:
    """Bounded, keyless HTTP client for official public market-data endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 8,
        session: aiohttp.ClientSession | None = None,
        user_agent: str = "crypto-intelligence-readonly/1.0",
    ):
        if not 0 < float(timeout_seconds) <= 10:
            raise ValueError("public HTTP timeout must be > 0 and <= 10 seconds")
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)
        self._session = session
        self._owns_session = session is None
        self._user_agent = user_agent

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds, connect=min(5.0, self.timeout_seconds))
            self._session = aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": self._user_agent})
        return self._session

    async def get_json(self, path: str, params: Mapping[str, Any] | None = None) -> tuple[Any, int]:
        session = await self._ensure_session()
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            async with session.get(url, params=dict(params or {})) as response:
                if response.status != 200:
                    text = await response.text()
                    raise ProviderError(f"GET {url} returned HTTP {response.status}: {text[:300]}")
                payload = await response.json(content_type=None)
                return payload, int(time.time() * 1000)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            if isinstance(exc, ProviderError):
                raise
            raise ProviderError(f"GET {url} failed: {type(exc).__name__}: {exc}") from exc

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
