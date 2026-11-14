from __future__ import annotations

from typing import Any, Mapping, Sequence

from src.hyperliquid.rest import HyperliquidREST

from .base import DerivativesDataProvider, ProviderCapabilityError
from .common import metadata, now_ms
from .instruments import InstrumentRegistry
from .runtime import ProviderRuntime
from .types import (
    FundingSnapshot,
    Instrument,
    Liquidation,
    MarkIndexSnapshot,
    NormalizedBBO,
    NormalizedBookLevel,
    NormalizedCandle,
    NormalizedL2,
    NormalizedTrade,
    OpenInterestSnapshot,
    ProviderHealth,
)


class HyperliquidProvider(ProviderRuntime, DerivativesDataProvider):
    """Normalization facade; existing strict Hyperliquid REST schemas remain authoritative."""

    provider_id = "hyperliquid"

    def __init__(self, config: Mapping[str, Any], instruments: InstrumentRegistry):
        super().__init__(self.provider_id)
        self.config = dict(config)
        self.instruments = instruments

    def _check(self, instrument: Instrument) -> None:
        if instrument.provider != self.provider_id:
            raise ValueError(f"instrument {instrument.instrument_id} does not belong to Hyperliquid")

    def _rest(self) -> HyperliquidREST:
        return HyperliquidREST(
            str(self.config["rest_url"]),
            timeout=float(self.config.get("request_timeout_seconds", 8)),
            max_retries=int(self.config.get("max_retries", 2)),
        )

    async def list_instruments(self) -> Sequence[Instrument]:
        return self.instruments.for_provider(self.provider_id)

    async def fetch_bbo(self, instrument: Instrument) -> NormalizedBBO:
        book = await self.fetch_l2(instrument, 20)
        if not book.bids or not book.asks:
            raise ValueError("Hyperliquid L2 has no BBO")
        return NormalizedBBO(
            book.metadata, book.bids[0].price, book.asks[0].price, book.bids[0].size, book.asks[0].size
        )

    async def fetch_l2(self, instrument: Instrument, depth: int = 20) -> NormalizedL2:
        self._check(instrument)
        try:
            async with self._rest() as rest:
                book = await rest.l2_book(instrument.venue_symbol)
            received = int(book.received_at_ms or now_ms())
            event_time = int(book.exchange_timestamp_ms)
            result = NormalizedL2(
                metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received,
                         source=str(self.config["rest_url"]), event_id=f"book:{event_time}:{received}"),
                bids=tuple(NormalizedBookLevel(level.price, level.size, level.orders) for level in book.bids[:depth]),
                asks=tuple(NormalizedBookLevel(level.price, level.size, level.orders) for level in book.asks[:depth]),
            )
            self._success(received, event_time)
            return result
        except Exception as exc:
            self._failure(exc)
            raise

    async def fetch_trades(self, instrument: Instrument, limit: int = 100) -> list[NormalizedTrade]:
        self._check(instrument)
        raise ProviderCapabilityError("Hyperliquid public trade history is consumed from the existing strict WebSocket adapter")

    async def fetch_candles(
        self, instrument: Instrument, interval: str, start_ms: int | None = None, limit: int = 300
    ) -> list[NormalizedCandle]:
        self._check(instrument)
        end = now_ms()
        start = int(start_ms if start_ms is not None else end - max(1, int(limit)) * 60_000)
        try:
            async with self._rest() as rest:
                candles = await rest.candles(instrument.venue_symbol, interval, start, end)
            result = [
                NormalizedCandle(
                    metadata(instrument, exchange_timestamp_ms=candle.exchange_timestamp_ms,
                             received_at_ms=int(candle.received_at_ms or end), source=str(self.config["rest_url"]),
                             event_id=f"candle:{interval}:{candle.open_time}"),
                    interval=interval, open_time_ms=candle.open_time, close_time_ms=candle.close_time,
                    open=candle.open, high=candle.high, low=candle.low, close=candle.close,
                    volume=candle.volume, trade_count=candle.trades, closed=candle.close_time < end,
                )
                for candle in candles[-max(1, int(limit)):]
            ]
            if result:
                self._success(result[-1].metadata.received_at_ms, result[-1].metadata.exchange_timestamp_ms)
            return result
        except Exception as exc:
            self._failure(exc)
            raise

    async def _context(self, instrument: Instrument) -> tuple[Any, int]:
        self._check(instrument)
        try:
            async with self._rest() as rest:
                _, contexts = await rest.meta_and_asset_contexts()
            context = contexts[instrument.venue_symbol]
            received = int(context.received_at_ms or now_ms())
            self._success(received, context.exchange_timestamp_ms or context.timestamp)
            return context, received
        except Exception as exc:
            self._failure(exc)
            raise

    async def fetch_funding(self, instrument: Instrument) -> FundingSnapshot:
        context, received = await self._context(instrument)
        event_time = int(context.exchange_timestamp_ms or context.timestamp)
        return FundingSnapshot(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received,
                     source=str(self.config["rest_url"])),
            rate=context.funding,
        )

    async def fetch_open_interest(self, instrument: Instrument) -> OpenInterestSnapshot:
        context, received = await self._context(instrument)
        event_time = int(context.exchange_timestamp_ms or context.timestamp)
        return OpenInterestSnapshot(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received,
                     source=str(self.config["rest_url"])),
            open_interest_base=context.open_interest,
            open_interest_quote=context.open_interest * context.mark,
        )

    async def fetch_mark_index(self, instrument: Instrument) -> MarkIndexSnapshot:
        context, received = await self._context(instrument)
        event_time = int(context.exchange_timestamp_ms or context.timestamp)
        return MarkIndexSnapshot(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received,
                     source=str(self.config["rest_url"])),
            mark_price=context.mark, index_price=context.oracle,
        )

    async def fetch_liquidations(self, instrument: Instrument, limit: int = 100) -> list[Liquidation]:
        self._check(instrument)
        raise ProviderCapabilityError("Hyperliquid liquidations require a separate public event stream")

    def health(self) -> ProviderHealth:
        return super().health(int(float(self.config.get("stale_after_seconds", 30)) * 1000))

    async def close(self) -> None:
        return None
