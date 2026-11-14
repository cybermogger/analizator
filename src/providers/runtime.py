from __future__ import annotations

import time

from .types import ProviderHealth, ProviderStatus


class ProviderRuntime:
    def __init__(self, provider_id: str):
        self.provider_id = provider_id
        self._last_received_at_ms: int | None = None
        self._last_exchange_timestamp_ms: int | None = None
        self._failures = 0
        self._last_error: str | None = None
        self._disabled = False

    def _success(self, received_at_ms: int, exchange_timestamp_ms: int | None = None) -> None:
        self._last_received_at_ms = int(received_at_ms)
        self._last_exchange_timestamp_ms = int(exchange_timestamp_ms or received_at_ms)
        self._failures = 0
        self._last_error = None

    def _failure(self, error: BaseException) -> None:
        self._failures += 1
        self._last_error = f"{type(error).__name__}: {error}"[:1000]

    def health(self, stale_after_ms: int = 30_000) -> ProviderHealth:
        checked = int(time.time() * 1000)
        lag = None
        if self._last_received_at_ms is not None:
            lag = max(0, checked - self._last_received_at_ms)
        if self._disabled:
            status = ProviderStatus.DISABLED
        elif self._failures > 0 and self._last_received_at_ms is None:
            status = ProviderStatus.DISCONNECTED
        elif self._failures >= 3:
            status = ProviderStatus.DISCONNECTED
        elif self._failures > 0:
            status = ProviderStatus.DEGRADED
        elif lag is not None and lag > stale_after_ms:
            status = ProviderStatus.STALE
        elif self._last_received_at_ms is not None:
            status = ProviderStatus.HEALTHY
        else:
            status = ProviderStatus.DEGRADED
        return ProviderHealth(
            provider=self.provider_id,
            status=status,
            checked_at_ms=checked,
            last_received_at_ms=self._last_received_at_ms,
            lag_ms=lag,
            consecutive_failures=self._failures,
            last_error=self._last_error,
        )
