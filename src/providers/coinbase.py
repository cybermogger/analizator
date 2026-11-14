from __future__ import annotations

import asyncio
from typing import Any, Mapping, Sequence

from .base import MarketDataProvider
from .common import (
    as_float, as_int, decimal_places, discovered_instrument, iso_to_ms, metadata, now_ms,
    require_mapping, require_sequence,
)
from .http import PublicHTTPClient
from .instruments import InstrumentRegistry
from .runtime import ProviderRuntime
from .types import (
    Instrument,
    MarketCatalogEntry,
    MarketCatalogScope,
    MarketType,
    NormalizedBBO,
    NormalizedBookLevel,
    NormalizedCandle,
    NormalizedL2,
    NormalizedTrade,
    ProviderHealth,
    Side,
)


_GRANULARITY_SECONDS = {
    "1m": ("ONE_MINUTE", 60),
    "5m": ("FIVE_MINUTE", 300),
    "15m": ("FIFTEEN_MINUTE", 900),
    "30m": ("THIRTY_MINUTE", 1800),
    "1h": ("ONE_HOUR", 3600),
    "4h": ("FOUR_HOUR", 14400),
    "1d": ("ONE_DAY", 86400),
}


class CoinbaseProvider(ProviderRuntime, MarketDataProvider):
    provider_id = "coinbase"

    def __init__(self, config: Mapping[str, Any], instruments: InstrumentRegistry):
        super().__init__(self.provider_id)
        self.config = dict(config)
        self.instruments = instruments
        self.http = PublicHTTPClient(
            str(config["rest_url"]), timeout_seconds=float(config.get("request_timeout_seconds", 8))
        )

    async def list_instruments(self) -> Sequence[Instrument]:
        return self.instruments.for_provider(self.provider_id)

    async def discover_catalog(self) -> Sequence[MarketCatalogScope]:
        scope, source = "spot", f"{self.http.base_url}/api/v3/brokerage/market/products"
        max_pages = max(1, min(40, int(self.config.get("catalog_max_pages", 20))))
        page_size = max(1, min(250, int(self.config.get("catalog_page_size", 250))))
        cursor: str | None = None
        received = now_ms()
        page_count = 0
        entries: list[MarketCatalogEntry] = []
        seen: set[str] = set()
        seen_cursors: set[str] = set()
        try:
            for page in range(max_pages):
                params: dict[str, Any] = {"limit": page_size, "product_type": "SPOT"}
                if cursor:
                    params["cursor"] = cursor
                payload, received = await self.http.get_json("/api/v3/brokerage/market/products", params)
                self._success(received)
                root = require_mapping(payload, "Coinbase products")
                products = require_sequence(root.get("products"), "Coinbase product page")
                page_count += 1
                if not products:
                    raise ValueError("Coinbase returned an empty product page")
                for raw in products:
                    product = require_mapping(raw, "Coinbase product")
                    symbol = str(product.get("product_id", ""))
                    base = str(product.get("base_currency_id", ""))
                    quote = str(product.get("quote_currency_id", ""))
                    if not symbol or not base or not quote:
                        raise ValueError("Coinbase product page contains a row without stable identity fields")
                    if symbol in seen:
                        raise ValueError("Coinbase product pagination contains duplicates")
                    seen.add(symbol)
                    is_spot = str(product.get("product_type", "SPOT")).upper() == "SPOT"
                    status_ok = is_spot and not any(bool(product.get(key, False)) for key in (
                        "is_disabled", "trading_disabled", "is_trading_disabled", "view_only"
                    )) and str(product.get("status", "online")).lower() in {"online", "trading", "active"}
                    instrument = discovered_instrument(
                        self.instruments, provider=self.provider_id, venue="coinbase-advanced",
                        market_type=MarketType.SPOT, base=base, quote=quote, venue_symbol=symbol,
                        price_decimals=decimal_places(product.get("quote_increment", "1")),
                        size_decimals=decimal_places(product.get("base_increment", "1")),
                        scope=scope, source=source, received_at_ms=received, active=status_ok,
                    )
                    reasons = [] if status_ok else ["market_not_trading_or_not_spot"]
                    price = turnover = None
                    if status_ok:
                        try:
                            price = as_float(product.get("price"), "price")
                        except ValueError:
                            reasons.append("last_price_unavailable_or_invalid")
                        try:
                            base_volume = as_float(product.get("volume_24h"), "volume_24h", allow_zero=True)
                        except ValueError:
                            base_volume = None
                            reasons.append("base_volume_unavailable_or_invalid")
                        if price is not None and base_volume is not None:
                            turnover = base_volume * price
                    if turnover is None:
                        reasons.append("quote_turnover_unavailable")
                    entries.append(MarketCatalogEntry(
                        instrument, scope, source, received, None, turnover, price,
                        active=status_ok, admitted=status_ok and turnover is not None, reasons=tuple(reasons),
                    ))
                pagination = require_mapping(root.get("pagination"), "Coinbase pagination")
                has_next = pagination.get("has_next")
                if not isinstance(has_next, bool):
                    raise ValueError("Coinbase pagination.has_next must be a boolean")
                next_cursor = pagination.get("next_cursor")
                if not has_next:
                    break
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                    raise ValueError("Coinbase pagination did not advance its cursor")
                seen_cursors.add(next_cursor)
                cursor = str(next_cursor)
                await asyncio.sleep(max(0.05, min(1.0, float(self.config.get("catalog_page_delay_seconds", 0.15)))))
            else:
                raise ValueError(f"Coinbase catalog exceeded {max_pages} pages; refresh marked incomplete")
            if not entries:
                raise ValueError("Coinbase catalog contained no parseable products")
            return (MarketCatalogScope(self.provider_id, scope, source, received, tuple(entries), True, page_count),)
        except Exception as exc:
            self._failure(exc)
            return (MarketCatalogScope(
                self.provider_id, scope, source, now_ms(), (), False, max(1, page_count),
                f"{type(exc).__name__}: {exc}"[:300],
            ),)

    async def refresh_bbo_batch(self, instruments: Sequence[Instrument]) -> Sequence[NormalizedBBO]:
        limit = max(1, min(30, int(self.config.get("bbo_enrichment_limit", 10))))
        requested = [item for item in instruments if item.provider == self.provider_id][:limit]
        results = await asyncio.gather(*(self.fetch_bbo(item) for item in requested), return_exceptions=True)
        return tuple(item for item in results if isinstance(item, NormalizedBBO))

    def _check(self, instrument: Instrument) -> None:
        if instrument.provider != self.provider_id:
            raise ValueError(f"instrument {instrument.instrument_id} does not belong to Coinbase")

    async def _get(self, instrument: Instrument, path: str, params: Mapping[str, Any] | None = None) -> tuple[Any, int, str]:
        self._check(instrument)
        try:
            payload, received = await self.http.get_json(path, params)
            self._success(received)
            return payload, received, f"{self.http.base_url}/{path.lstrip('/')}"
        except Exception as exc:
            self._failure(exc)
            raise

    def _parse_book(
        self, instrument: Instrument, payload: Any, received: int, source: str
    ) -> tuple[NormalizedBBO, NormalizedL2]:
        root = require_mapping(payload, "Coinbase product book")
        book = require_mapping(root.get("pricebook", root), "Coinbase pricebook")
        has_event_time = bool(book.get("time"))
        event_time = iso_to_ms(str(book["time"])) if has_event_time else received

        def levels(raw: Any, *, reverse: bool) -> tuple[NormalizedBookLevel, ...]:
            parsed = tuple(
                NormalizedBookLevel(
                    as_float(require_mapping(item, "Coinbase book level")["price"], "price"),
                    as_float(require_mapping(item, "Coinbase book level")["size"], "size", allow_zero=True),
                )
                for item in require_sequence(raw, "Coinbase book side")
            )
            return tuple(sorted(parsed, key=lambda item: item.price, reverse=reverse))

        bids = levels(book.get("bids", []), reverse=True)
        asks = levels(book.get("asks", []), reverse=False)
        if not bids or not asks:
            raise ValueError("Coinbase product book has no BBO")
        meta = metadata(
            instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
            exchange_time_kind="EXCHANGE_EVENT" if has_event_time else "RECEIVE_TIME_FALLBACK",
            event_id=f"book:{event_time}:{bids[0].price}:{asks[0].price}:{received}",
        )
        return (
            NormalizedBBO(meta, bids[0].price, asks[0].price, bids[0].size, asks[0].size),
            NormalizedL2(meta, bids, asks),
        )

    async def fetch_bbo(self, instrument: Instrument) -> NormalizedBBO:
        payload, received, source = await self._get(
            instrument, "/api/v3/brokerage/market/product_book",
            {"product_id": instrument.venue_symbol, "limit": 1},
        )
        bbo, _ = self._parse_book(instrument, payload, received, source)
        self._success(received, bbo.metadata.exchange_timestamp_ms)
        return bbo

    async def fetch_l2(self, instrument: Instrument, depth: int = 20) -> NormalizedL2:
        payload, received, source = await self._get(
            instrument, "/api/v3/brokerage/market/product_book",
            {"product_id": instrument.venue_symbol, "limit": max(1, min(int(depth), 1000))},
        )
        _, book = self._parse_book(instrument, payload, received, source)
        self._success(received, book.metadata.exchange_timestamp_ms)
        return book

    async def fetch_trades(self, instrument: Instrument, limit: int = 100) -> list[NormalizedTrade]:
        payload, received, source = await self._get(
            instrument, f"/api/v3/brokerage/market/products/{instrument.venue_symbol}/ticker",
            {"limit": max(1, min(int(limit), 1000))},
        )
        root = require_mapping(payload, "Coinbase market trades")
        result: list[NormalizedTrade] = []
        for raw in require_sequence(root.get("trades", []), "Coinbase trades"):
            item = require_mapping(raw, "Coinbase trade")
            event_time = iso_to_ms(str(item["time"]))
            trade_id = str(item["trade_id"])
            # Coinbase public market trade side is the maker side; aggressor side is the inverse.
            maker = str(item["side"]).upper()
            if maker not in {"BUY", "SELL"}:
                raise ValueError(f"unexpected Coinbase maker side: {maker}")
            result.append(NormalizedTrade(
                metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                         event_id=f"trade:{trade_id}"),
                side=Side.SELL if maker == "BUY" else Side.BUY,
                price=as_float(item["price"], "price"),
                size=as_float(item["size"], "size", allow_zero=True),
                trade_id=trade_id,
            ))
        result.sort(key=lambda item: (item.metadata.exchange_timestamp_ms, item.trade_id))
        if result:
            self._success(received, result[-1].metadata.exchange_timestamp_ms)
        return result

    async def fetch_candles(
        self, instrument: Instrument, interval: str, start_ms: int | None = None, limit: int = 300
    ) -> list[NormalizedCandle]:
        if interval not in _GRANULARITY_SECONDS:
            raise ValueError(f"unsupported Coinbase candle interval: {interval}")
        granularity, seconds = _GRANULARITY_SECONDS[interval]
        end_s = now_ms() // 1000
        start_s = int(start_ms // 1000) if start_ms is not None else end_s - seconds * max(1, min(int(limit), 350))
        payload, received, source = await self._get(
            instrument, f"/api/v3/brokerage/market/products/{instrument.venue_symbol}/candles",
            {"start": start_s, "end": end_s, "granularity": granularity, "limit": max(1, min(int(limit), 350))},
        )
        root = require_mapping(payload, "Coinbase candles")
        result: list[NormalizedCandle] = []
        current = now_ms()
        for raw in require_sequence(root.get("candles", []), "Coinbase candles"):
            item = require_mapping(raw, "Coinbase candle")
            open_time = as_int(item["start"], "start") * 1000
            close_time = open_time + seconds * 1000 - 1
            result.append(NormalizedCandle(
                metadata(instrument, exchange_timestamp_ms=close_time, received_at_ms=received, source=source,
                         exchange_time_kind="DERIVED_INTERVAL_CLOSE",
                         event_id=f"candle:{interval}:{open_time}"),
                interval=interval, open_time_ms=open_time, close_time_ms=close_time,
                open=as_float(item["open"], "open"), high=as_float(item["high"], "high"),
                low=as_float(item["low"], "low"), close=as_float(item["close"], "close"),
                volume=as_float(item["volume"], "volume", allow_zero=True),
                closed=close_time < current,
            ))
        result.sort(key=lambda item: item.open_time_ms)
        if result:
            self._success(received, result[-1].metadata.exchange_timestamp_ms)
        return result

    def parse_ticker_message(self, payload: Mapping[str, Any], received_at_ms: int) -> list[NormalizedBBO]:
        has_event_time = bool(payload.get("timestamp"))
        event_time = iso_to_ms(str(payload["timestamp"])) if has_event_time else received_at_ms
        result: list[NormalizedBBO] = []
        for event in require_sequence(payload.get("events", []), "Coinbase ticker events"):
            event_obj = require_mapping(event, "Coinbase ticker event")
            for raw in require_sequence(event_obj.get("tickers", []), "Coinbase tickers"):
                ticker = require_mapping(raw, "Coinbase ticker")
                symbol = str(ticker["product_id"])
                candidates = [i for i in self.instruments.for_provider(self.provider_id) if i.venue_symbol == symbol]
                if len(candidates) != 1:
                    continue
                instrument = candidates[0]
                bid = as_float(ticker["best_bid"], "best_bid")
                ask = as_float(ticker["best_ask"], "best_ask")
                result.append(NormalizedBBO(
                    metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received_at_ms,
                             exchange_time_kind="EXCHANGE_EVENT" if has_event_time else "RECEIVE_TIME_FALLBACK",
                             source=str(self.config["websocket_url"]),
                             event_id=f"ticker:{event_time}:{bid}:{ask}"),
                    bid=bid, ask=ask,
                    bid_size=as_float(ticker.get("best_bid_quantity", 0), "best_bid_quantity", allow_zero=True),
                    ask_size=as_float(ticker.get("best_ask_quantity", 0), "best_ask_quantity", allow_zero=True),
                    quote_volume_24h=as_float(ticker["volume_24_h"], "volume_24_h", allow_zero=True) *
                    as_float(ticker.get("price", (bid + ask) / 2), "price"),
                ))
        if result:
            self._success(received_at_ms, event_time)
        return result

    def health(self) -> ProviderHealth:
        return super().health(int(float(self.config.get("stale_after_seconds", 30)) * 1000))

    async def close(self) -> None:
        await self.http.close()
