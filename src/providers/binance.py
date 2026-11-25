from __future__ import annotations

import time
import asyncio
import json
from typing import Any, Mapping, Sequence

from .base import DerivativesDataProvider, ProviderCapabilityError
from .common import (
    as_float, as_int, as_signed_float, decimal_places, discovered_instrument, metadata, now_ms,
    require_mapping, require_sequence,
)
from .http import PublicHTTPClient
from .instruments import InstrumentRegistry
from .runtime import ProviderRuntime
from .types import (
    FundingSnapshot,
    Instrument,
    MarketCatalogEntry,
    MarketCatalogScope,
    Liquidation,
    MarkIndexSnapshot,
    MarketType,
    NormalizedBBO,
    NormalizedBookLevel,
    NormalizedCandle,
    NormalizedL2,
    NormalizedTrade,
    OpenInterestSnapshot,
    ProviderHealth,
    Side,
)


class BinanceProvider(ProviderRuntime, DerivativesDataProvider):
    provider_id = "binance"
    bbo_batch_mode = "bulk"

    def __init__(self, config: Mapping[str, Any], instruments: InstrumentRegistry):
        super().__init__(self.provider_id)
        timeout = float(config.get("request_timeout_seconds", 8))
        self.config = dict(config)
        self.instruments = instruments
        self.spot = PublicHTTPClient(str(config["spot_rest_url"]), timeout_seconds=timeout)
        self.futures = PublicHTTPClient(str(config["futures_rest_url"]), timeout_seconds=timeout)

    async def list_instruments(self) -> Sequence[Instrument]:
        return self.instruments.for_provider(self.provider_id)

    async def discover_catalog(self) -> Sequence[MarketCatalogScope]:
        scopes: list[MarketCatalogScope] = []
        groups = (
            (MarketType.SPOT, self.spot, "binance-spot", "/api/v3", "exchangeInfo", "ticker/24hr"),
            (MarketType.PERPETUAL, self.futures, "binance-usdm", "/fapi/v1", "exchangeInfo", "ticker/24hr"),
        )
        async def discover(market_type, client, venue, prefix, info_path, ticker_path):
            scope = market_type.value
            try:
                info, received = await client.get_json(f"{prefix}/{info_path}", {"showPermissionSets": "false"} if market_type == MarketType.SPOT else None)
                self._success(received)
                ticker_rows, ticker_received = await client.get_json(f"{prefix}/{ticker_path}")
                received = max(received, ticker_received)
                rows = require_sequence(require_mapping(info, "Binance exchangeInfo").get("symbols"), "Binance symbols")
                tickers: dict[str, Mapping[str, Any]] = {}
                for raw_ticker in require_sequence(ticker_rows, "Binance ticker/24hr"):
                    if not isinstance(raw_ticker, Mapping):
                        continue
                    ticker_symbol = str(raw_ticker.get("symbol", ""))
                    if ticker_symbol:
                        tickers[ticker_symbol] = raw_ticker
                if not rows:
                    raise ValueError("Binance exchangeInfo returned an empty catalog")
                entries: list[MarketCatalogEntry] = []
                for raw in rows:
                    row = require_mapping(raw, "Binance symbol")
                    if market_type == MarketType.PERPETUAL and row.get("contractType") != "PERPETUAL":
                        continue
                    symbol = str(row.get("symbol", ""))
                    base, quote = str(row.get("baseAsset", "")), str(row.get("quoteAsset", ""))
                    if not symbol or not base or not quote:
                        raise ValueError("Binance exchangeInfo contains a symbol without stable identity fields")
                    filters = {
                        str(item.get("filterType")): item
                        for item in row.get("filters", []) if isinstance(item, Mapping)
                    }
                    price_decimals = decimal_places(filters.get("PRICE_FILTER", {}).get("tickSize", "1"))
                    size_decimals = decimal_places(filters.get("LOT_SIZE", {}).get("stepSize", "1"))
                    status_ok = row.get("status") == "TRADING" and (
                        market_type != MarketType.SPOT or bool(row.get("isSpotTradingAllowed", True))
                    )
                    instrument = discovered_instrument(
                        self.instruments, provider=self.provider_id, venue=venue, market_type=market_type,
                        base=base, quote=quote, venue_symbol=symbol, price_decimals=price_decimals,
                        size_decimals=size_decimals, scope=scope,
                        source=f"{client.base_url}{prefix}/{info_path}", received_at_ms=received,
                        active=status_ok,
                    )
                    ticker = tickers.get(symbol)
                    reasons = [] if status_ok else ["market_not_trading"]
                    turnover = last = bid = ask = bid_size = ask_size = change_magnitude = None
                    exchange_time = None
                    if ticker is None:
                        reasons.append("ticker_not_returned")
                    else:
                        try:
                            turnover = as_float(ticker.get("quoteVolume"), "quoteVolume", allow_zero=True)
                        except ValueError:
                            reasons.append("quote_turnover_invalid")
                        try:
                            last = as_float(ticker.get("lastPrice"), "lastPrice")
                        except ValueError:
                            reasons.append("last_price_invalid")
                        try:
                            change_magnitude = abs(as_signed_float(ticker.get("priceChangePercent"), "priceChangePercent"))
                        except ValueError:
                            reasons.append("price_change_24h_unavailable")
                        try:
                            exchange_time = as_int(ticker.get("closeTime"), "closeTime")
                            max_age_ms = int(float(self.config.get("max_exchange_timestamp_age_seconds", 180)) * 1000)
                            future_tolerance_ms = int(float(self.config.get("exchange_timestamp_future_tolerance_seconds", 5)) * 1000)
                            if exchange_time > received + future_tolerance_ms:
                                reasons.append("ticker_timestamp_future")
                                turnover = last = change_magnitude = None
                            elif received - exchange_time > max_age_ms:
                                reasons.append("ticker_timestamp_stale")
                                turnover = last = change_magnitude = None
                        except (TypeError, ValueError):
                            exchange_time = None
                            reasons.append("ticker_timestamp_unavailable")
                            turnover = last = change_magnitude = None
                        if ticker.get("bidPrice") is not None or ticker.get("askPrice") is not None:
                            try:
                                bid = as_float(ticker.get("bidPrice"), "bidPrice")
                                ask = as_float(ticker.get("askPrice"), "askPrice")
                                if bid >= ask:
                                    raise ValueError("crossed bid/ask")
                                bid_size = as_float(ticker.get("bidQty", 0), "bidQty", allow_zero=True)
                                ask_size = as_float(ticker.get("askQty", 0), "askQty", allow_zero=True)
                            except ValueError:
                                bid = ask = bid_size = ask_size = None
                                reasons.append("ticker_bbo_invalid")
                    entries.append(MarketCatalogEntry(
                        instrument, scope, f"{client.base_url}{prefix}/{ticker_path}", received,
                        exchange_time, turnover, last, bid, ask, bid_size, ask_size, change_magnitude,
                        active=status_ok, admitted=status_ok and not reasons, reasons=tuple(reasons),
                    ))
                scopes.append(MarketCatalogScope(
                    self.provider_id, scope, f"{client.base_url}{prefix}/{info_path}+{ticker_path}",
                    received, tuple(entries), True, 1,
                ))
            except Exception as exc:
                self._failure(exc)
                scopes.append(MarketCatalogScope(
                    self.provider_id, scope, f"{client.base_url}{prefix}/{info_path}+{ticker_path}",
                    now_ms(), (), False, 1, f"{type(exc).__name__}: {exc}"[:300],
                ))
        await asyncio.gather(*(discover(*group) for group in groups))
        return tuple(scopes)

    async def refresh_bbo_batch(self, instruments: Sequence[Instrument]) -> Sequence[NormalizedBBO]:
        result: list[NormalizedBBO] = []
        groups = (
            (MarketType.SPOT, self.spot, "/api/v3", "binance-spot"),
            (MarketType.PERPETUAL, self.futures, "/fapi/v1", "binance-usdm"),
        )
        async def fetch_group(market_type, client, prefix, venue):
            requested = [item for item in instruments if item.market_type == market_type and item.provider == self.provider_id]
            if not requested:
                return
            try:
                payload, received = await client.get_json(f"{prefix}/ticker/bookTicker", {"symbols": json.dumps([item.venue_symbol for item in requested], ensure_ascii=False, separators=(",", ":"))} if market_type == MarketType.SPOT else None)
                rows = {str(item["symbol"]): item for item in require_sequence(payload, "Binance book tickers")
                        if isinstance(item, Mapping) and item.get("symbol")}
                for instrument in requested:
                    row = rows.get(instrument.venue_symbol)
                    if row is None:
                        continue
                    try:
                        exchange = as_int(row.get("time", row.get("E", received)), "exchange timestamp")
                        bid = as_float(row.get("bidPrice"), "bidPrice")
                        ask = as_float(row.get("askPrice"), "askPrice")
                        if bid >= ask:
                            continue
                        result.append(NormalizedBBO(
                            metadata(instrument, exchange_timestamp_ms=exchange, received_at_ms=received,
                                     source=f"{client.base_url}{prefix}/ticker/bookTicker",
                                     sequence=as_int(row.get("u", row.get("updateId", 0)), "update id") or None,
                                     event_id=f"book-ticker:{instrument.venue_symbol}:{exchange}:{received}"),
                            bid, ask,
                            as_float(row.get("bidQty", 0), "bidQty", allow_zero=True),
                            as_float(row.get("askQty", 0), "askQty", allow_zero=True), None,
                        ))
                    except (TypeError, ValueError):
                        # An invalid quote is isolated to its instrument, not the full batch.
                        continue
                self._success(received)
            except Exception as exc:
                self._failure(exc)
        await asyncio.gather(*(fetch_group(*group) for group in groups))
        return tuple(result)

    def _client(self, instrument: Instrument) -> tuple[PublicHTTPClient, str]:
        if instrument.provider != self.provider_id:
            raise ValueError(f"instrument {instrument.instrument_id} does not belong to Binance")
        return (self.spot, "/api/v3") if instrument.market_type == MarketType.SPOT else (self.futures, "/fapi/v1")

    async def _get(self, instrument: Instrument, endpoint: str, params: Mapping[str, Any]) -> tuple[Any, int, str]:
        client, prefix = self._client(instrument)
        path = f"{prefix}/{endpoint}"
        try:
            payload, received = await client.get_json(path, params)
            self._success(received)
            return payload, received, f"{client.base_url}{path}"
        except Exception as exc:
            self._failure(exc)
            raise

    async def fetch_bbo(self, instrument: Instrument) -> NormalizedBBO:
        payload, received, source = await self._get(instrument, "ticker/bookTicker", {"symbol": instrument.venue_symbol})
        obj = require_mapping(payload, "Binance bookTicker")
        has_event_time = obj.get("time") is not None or obj.get("E") is not None
        event_time = as_int(obj.get("time", obj.get("E", received)), "time")
        bbo = NormalizedBBO(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                     exchange_time_kind="EXCHANGE_EVENT" if has_event_time else "RECEIVE_TIME_FALLBACK",
                     sequence=as_int(obj["u"], "u") if obj.get("u") is not None else None,
                     event_id=f"rest-book:{instrument.venue_symbol}:{event_time}:{received}"),
            bid=as_float(obj.get("bidPrice", obj.get("b")), "bidPrice"),
            ask=as_float(obj.get("askPrice", obj.get("a")), "askPrice"),
            bid_size=as_float(obj.get("bidQty", obj.get("B", 0)), "bidQty", allow_zero=True),
            ask_size=as_float(obj.get("askQty", obj.get("A", 0)), "askQty", allow_zero=True),
        )
        self._success(received, event_time)
        return bbo

    async def fetch_trades(self, instrument: Instrument, limit: int = 100) -> list[NormalizedTrade]:
        payload, received, source = await self._get(
            instrument, "trades", {"symbol": instrument.venue_symbol, "limit": max(1, min(int(limit), 1000))}
        )
        items = require_sequence(payload, "Binance trades")
        result: list[NormalizedTrade] = []
        for item in items:
            obj = require_mapping(item, "Binance trade")
            event_time = as_int(obj.get("time", obj.get("T", received)), "time")
            trade_id = str(obj.get("id", obj.get("a", "")))
            result.append(NormalizedTrade(
                metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                         event_id=f"trade:{trade_id}"),
                side=Side.SELL if bool(obj.get("isBuyerMaker", obj.get("m", False))) else Side.BUY,
                price=as_float(obj.get("price", obj.get("p")), "price"),
                size=as_float(obj.get("qty", obj.get("q")), "qty", allow_zero=True),
                trade_id=trade_id,
            ))
        if result:
            self._success(received, result[-1].metadata.exchange_timestamp_ms)
        return result

    async def fetch_l2(self, instrument: Instrument, depth: int = 20) -> NormalizedL2:
        allowed = min((5, 10, 20, 50, 100, 500, 1000), key=lambda value: abs(value - max(5, int(depth))))
        payload, received, source = await self._get(
            instrument, "depth", {"symbol": instrument.venue_symbol, "limit": allowed}
        )
        obj = require_mapping(payload, "Binance depth")
        has_event_time = obj.get("E") is not None or obj.get("T") is not None
        event_time = as_int(obj.get("E", obj.get("T", received)), "event time")
        sequence = as_int(obj.get("lastUpdateId", obj.get("u", 0)), "lastUpdateId")
        levels = lambda raw: tuple(
            NormalizedBookLevel(as_float(item[0], "price"), as_float(item[1], "size", allow_zero=True))
            for item in require_sequence(raw, "book side")
        )
        book = NormalizedL2(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                     sequence=sequence, exchange_time_kind="EXCHANGE_EVENT" if has_event_time else "RECEIVE_TIME_FALLBACK"),
            bids=levels(obj.get("bids", obj.get("b", []))),
            asks=levels(obj.get("asks", obj.get("a", []))),
        )
        self._success(received, event_time)
        return book

    async def fetch_candles(
        self, instrument: Instrument, interval: str, start_ms: int | None = None, limit: int = 300
    ) -> list[NormalizedCandle]:
        params: dict[str, Any] = {
            "symbol": instrument.venue_symbol,
            "interval": interval,
            "limit": max(1, min(int(limit), 1000)),
        }
        if start_ms is not None:
            params["startTime"] = int(start_ms)
        payload, received, source = await self._get(instrument, "klines", params)
        result: list[NormalizedCandle] = []
        current = now_ms()
        for raw in require_sequence(payload, "Binance klines"):
            item = require_sequence(raw, "Binance kline")
            open_time = as_int(item[0], "open time")
            close_time = as_int(item[6], "close time")
            result.append(NormalizedCandle(
                metadata(instrument, exchange_timestamp_ms=close_time, received_at_ms=received, source=source,
                         event_id=f"candle:{interval}:{open_time}"),
                interval=interval, open_time_ms=open_time, close_time_ms=close_time,
                open=as_float(item[1], "open"), high=as_float(item[2], "high"),
                low=as_float(item[3], "low"), close=as_float(item[4], "close"),
                volume=as_float(item[5], "volume", allow_zero=True),
                trade_count=as_int(item[8], "trade count") if len(item) > 8 else None,
                closed=close_time < current,
            ))
        if result:
            self._success(received, result[-1].metadata.exchange_timestamp_ms)
        return result

    def _require_perpetual(self, instrument: Instrument) -> None:
        if instrument.market_type != MarketType.PERPETUAL:
            raise ProviderCapabilityError(f"{instrument.instrument_id} is not a derivatives instrument")

    async def _premium_index(self, instrument: Instrument) -> tuple[Mapping[str, Any], int, str]:
        self._require_perpetual(instrument)
        payload, received, source = await self._get(instrument, "premiumIndex", {"symbol": instrument.venue_symbol})
        return require_mapping(payload, "Binance premiumIndex"), received, source

    async def fetch_funding(self, instrument: Instrument) -> FundingSnapshot:
        obj, received, source = await self._premium_index(instrument)
        has_event_time = obj.get("time") is not None
        event_time = as_int(obj.get("time", received), "time")
        return FundingSnapshot(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                     exchange_time_kind="EXCHANGE_EVENT" if has_event_time else "RECEIVE_TIME_FALLBACK"),
            rate=float(obj["lastFundingRate"]),
            next_funding_time_ms=as_int(obj["nextFundingTime"], "nextFundingTime"),
        )

    async def fetch_open_interest(self, instrument: Instrument) -> OpenInterestSnapshot:
        self._require_perpetual(instrument)
        payload, received, source = await self._get(instrument, "openInterest", {"symbol": instrument.venue_symbol})
        obj = require_mapping(payload, "Binance openInterest")
        has_event_time = obj.get("time") is not None
        event_time = as_int(obj.get("time", received), "time")
        value = as_float(obj["openInterest"], "openInterest", allow_zero=True)
        return OpenInterestSnapshot(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                     exchange_time_kind="EXCHANGE_EVENT" if has_event_time else "RECEIVE_TIME_FALLBACK"),
            open_interest_base=value,
        )

    async def fetch_mark_index(self, instrument: Instrument) -> MarkIndexSnapshot:
        obj, received, source = await self._premium_index(instrument)
        has_event_time = obj.get("time") is not None
        event_time = as_int(obj.get("time", received), "time")
        return MarkIndexSnapshot(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                     exchange_time_kind="EXCHANGE_EVENT" if has_event_time else "RECEIVE_TIME_FALLBACK"),
            mark_price=as_float(obj["markPrice"], "markPrice"),
            index_price=as_float(obj["indexPrice"], "indexPrice"),
        )

    async def fetch_liquidations(self, instrument: Instrument, limit: int = 100) -> list[Liquidation]:
        self._require_perpetual(instrument)
        raise ProviderCapabilityError("Binance public liquidations are a WebSocket stream, not a REST snapshot")

    def parse_liquidation(self, instrument: Instrument, payload: Mapping[str, Any], received_at_ms: int) -> Liquidation:
        obj = require_mapping(payload.get("o"), "Binance forceOrder.o")
        event_time = as_int(payload.get("E", obj.get("T", received_at_ms)), "event time")
        price = as_float(obj.get("ap", obj.get("p")), "average price")
        size = as_float(obj.get("z", obj.get("q")), "filled quantity", allow_zero=True)
        order_id = str(obj.get("i", f"{event_time}:{obj.get('S')}:{price}:{size}"))
        return Liquidation(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received_at_ms,
                     source=str(self.config["futures_websocket_url"]), event_id=f"liquidation:{order_id}"),
            side=Side(str(obj["S"]).lower()), price=price, size=size, notional=price * size,
            liquidation_id=order_id,
        )

    def health(self) -> ProviderHealth:
        return super().health(int(float(self.config.get("stale_after_seconds", 30)) * 1000))

    async def close(self) -> None:
        await self.spot.close()
        await self.futures.close()
