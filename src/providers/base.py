from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
from typing import AsyncIterator, Sequence

from .types import (
    FundingSnapshot,
    Instrument,
    Liquidation,
    MarkIndexSnapshot,
    NormalizedBBO,
    NormalizedCandle,
    NormalizedEvent,
    NormalizedL2,
    NormalizedTrade,
    OpenInterestSnapshot,
    MarketCatalogScope,
    ProviderHealth,
)


class ProviderError(RuntimeError):
    pass


class ProviderCapabilityError(ProviderError):
    pass


class MarketDataProvider(ABC):
    """Read-only normalized interface for direct public venue data."""

    provider_id: str
    # Providers may opt into one-request bulk BBO endpoints. The default strategy
    # deliberately routes each instrument through the service's rate/concurrency gate.
    bbo_batch_mode = "per_instrument"

    @abstractmethod
    async def list_instruments(self) -> Sequence[Instrument]: ...

    async def discover_catalog(self) -> Sequence[MarketCatalogScope]:
        """Refresh provider market metadata; unsupported providers keep configured instruments."""
        raise ProviderCapabilityError(f"{self.provider_id} catalog discovery is not configured")

    async def refresh_bbo_batch(self, instruments: Sequence[Instrument]) -> Sequence[NormalizedBBO]:
        """Fetch a bounded set of lightweight quotes. Implementations may use bulk ticker endpoints."""
        results = await asyncio.gather(*(self.fetch_bbo(item) for item in instruments), return_exceptions=True)
        return tuple(item for item in results if isinstance(item, NormalizedBBO))

    @abstractmethod
    async def fetch_bbo(self, instrument: Instrument) -> NormalizedBBO: ...

    @abstractmethod
    async def fetch_trades(self, instrument: Instrument, limit: int = 100) -> list[NormalizedTrade]: ...

    @abstractmethod
    async def fetch_l2(self, instrument: Instrument, depth: int = 20) -> NormalizedL2: ...

    @abstractmethod
    async def fetch_candles(
        self, instrument: Instrument, interval: str, start_ms: int | None = None, limit: int = 300
    ) -> list[NormalizedCandle]: ...

    async def stream(self, instruments: Sequence[Instrument], *, deep: bool) -> AsyncIterator[NormalizedEvent]:
        raise ProviderCapabilityError(f"{self.provider_id} streaming is not configured")
        yield  # pragma: no cover

    @abstractmethod
    def health(self) -> ProviderHealth: ...

    @abstractmethod
    async def close(self) -> None: ...


class DerivativesDataProvider(MarketDataProvider, ABC):
    @abstractmethod
    async def fetch_funding(self, instrument: Instrument) -> FundingSnapshot: ...

    @abstractmethod
    async def fetch_open_interest(self, instrument: Instrument) -> OpenInterestSnapshot: ...

    @abstractmethod
    async def fetch_liquidations(self, instrument: Instrument, limit: int = 100) -> list[Liquidation]: ...

    @abstractmethod
    async def fetch_mark_index(self, instrument: Instrument) -> MarkIndexSnapshot: ...
