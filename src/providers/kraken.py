from __future__ import annotations

from dataclasses import replace
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


_INTERVAL_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}


class KrakenProvider(ProviderRuntime, MarketDataProvider):
    provider_id = "kraken"
    bbo_batch_mode = "bulk"

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
        scope = "spot"
        source = f"{self.http.base_url}/0/public/AssetPairs+Ticker"
        try:
            pairs, received = await self.http.get_json("/0/public/AssetPairs")
            pair_root = require_mapping(pairs, "Kraken AssetPairs")
            if require_sequence(pair_root.get("error", []), "Kraken errors"):
                raise ValueError("Kraken AssetPairs returned an error")
            pair_rows = require_mapping(pair_root.get("result"), "Kraken AssetPairs result")
            if not pair_rows:
                raise ValueError("Kraken AssetPairs returned an empty catalog")
            tickers, ticker_received = await self.http.get_json("/0/public/Ticker")
            received = max(received, ticker_received)
            ticker_root = require_mapping(tickers, "Kraken Ticker")
            if require_sequence(ticker_root.get("error", []), "Kraken errors"):
                raise ValueError("Kraken Ticker returned an error")
            ticker_rows = require_mapping(ticker_root.get("result"), "Kraken Ticker result")
            entries: list[MarketCatalogEntry] = []
            for pair_key, raw in pair_rows.items():
                row = require_mapping(raw, "Kraken pair")
                wsname = str(row.get("wsname", ""))
                if "/" not in wsname:
                    raise ValueError(f"Kraken pair {pair_key} lacks a parseable wsname; catalog is incomplete")
                base, quote = wsname.split("/", 1)
                symbol = wsname
                tick = ticker_rows.get(pair_key) or ticker_rows.get(str(row.get("altname", ""))) or ticker_rows.get(wsname)
                status_ok = str(row.get("status", "online")).lower() in {"online", "trading"}
                instrument = discovered_instrument(
                    self.instruments, provider=self.provider_id, venue="kraken-spot",
                    market_type=MarketType.SPOT, base=base, quote=quote, venue_symbol=symbol,
                    price_decimals=int(row.get("pair_decimals", 0)),
                    size_decimals=int(row.get("lot_decimals", 0)), scope=scope, source=source,
                    received_at_ms=received, active=status_ok,
                )
                instrument = replace(instrument, metadata={
                    **instrument.metadata,
                    "kraken_pair_key": pair_key,
                    "kraken_altname": str(row.get("altname", "")),
                    "kraken_wsname": wsname,
                })
                reasons = [] if status_ok else ["market_not_trading"]
                turnover = last = bid = ask = bid_size = ask_size = None
                if not status_ok:
                    pass
                elif tick is None:
                    reasons.append("ticker_not_returned")
                else:
                    try:
                        ticker = require_mapping(tick, "Kraken ticker pair")
                        bid_values = require_sequence(ticker.get("b"), "Kraken bid")
                        ask_values = require_sequence(ticker.get("a"), "Kraken ask")
                        if not bid_values or not ask_values:
                            raise ValueError("ticker has no bid or ask")
                        bid = as_float(bid_values[0], "bid")
                        ask = as_float(ask_values[0], "ask")
                        if bid >= ask:
                            raise ValueError("ticker has a crossed bid/ask")
                        bid_size = as_float(bid_values[2] if len(bid_values) > 2 else 0, "bid size", allow_zero=True)
                        ask_size = as_float(ask_values[2] if len(ask_values) > 2 else 0, "ask size", allow_zero=True)
                    except (TypeError, ValueError, KeyError):
                        bid = ask = bid_size = ask_size = None
                        reasons.append("ticker_bbo_unavailable_or_invalid")
                    try:
                        ticker = require_mapping(tick, "Kraken ticker pair")
                        last_values = require_sequence(ticker.get("c"), "Kraken last")
                        volume_values = require_sequence(ticker.get("v"), "Kraken volume")
                        if not last_values or len(volume_values) < 2:
                            raise ValueError("ticker lacks last price or 24h volume")
                        last = as_float(last_values[0], "last")
                        turnover = as_float(volume_values[1], "24h volume", allow_zero=True) * last
                    except (TypeError, ValueError, KeyError):
                        last = turnover = None
                        reasons.append("ticker_turnover_unavailable_or_invalid")
                entries.append(MarketCatalogEntry(
                    instrument, scope, source, received, None, turnover, last,
                    bid, ask, bid_size, ask_size, None,
                    active=status_ok, admitted=status_ok and turnover is not None and not reasons,
                    reasons=tuple(reasons),
                ))
            if not entries:
                raise ValueError("Kraken catalog contained no parseable pairs")
            self._success(received)
            return (MarketCatalogScope(self.provider_id, scope, source, received, tuple(entries), True),)
        except Exception as exc:
            self._failure(exc)
            return (MarketCatalogScope(
                self.provider_id, scope, source, now_ms(), (), False, 1,
                f"{type(exc).__name__}: {exc}"[:300],
            ),)

    async def refresh_bbo_batch(self, instruments: Sequence[Instrument]):
        requested = [item for item in instruments if item.provider == self.provider_id]
        if not requested:
            return ()
        try:
            payload, received = await self.http.get_json("/0/public/Ticker")
            root = require_mapping(payload, "Kraken Ticker")
            if require_sequence(root.get("error", []), "Kraken errors"):
                raise ValueError("Kraken Ticker returned an error")
            rows = require_mapping(root.get("result"), "Kraken Ticker result")
            result = []
            for instrument in requested:
                pair_keys = (
                    str(instrument.metadata.get("kraken_pair_key", "")),
                    str(instrument.metadata.get("kraken_altname", "")),
                    str(instrument.metadata.get("kraken_wsname", "")),
                    instrument.venue_symbol,
                )
                item = next((rows[key] for key in pair_keys if key and key in rows), None)
                if item is None:
                    continue
                try:
                    ticker = require_mapping(item, "Kraken ticker pair")
                    bid_values = require_sequence(ticker.get("b"), "Kraken bid")
                    ask_values = require_sequence(ticker.get("a"), "Kraken ask")
                    volume_values = require_sequence(ticker.get("v"), "Kraken volume")
                    if not bid_values or not ask_values:
                        continue
                    bid, ask = as_float(bid_values[0], "bid"), as_float(ask_values[0], "ask")
                    if bid >= ask:
                        continue
                    last_values = require_sequence(ticker.get("c", []), "Kraken last")
                    turnover = as_float(volume_values[1], "24h volume", allow_zero=True) * as_float(last_values[0], "last") if len(volume_values) > 1 and last_values else None
                    result.append(NormalizedBBO(
                        metadata(instrument, exchange_timestamp_ms=received, received_at_ms=received,
                                 exchange_time_kind="RECEIVE_TIME_FALLBACK",
                                 source=f"{self.http.base_url}/0/public/Ticker",
                                 event_id=f"bulk-ticker:{instrument.venue_symbol}:{received}"),
                        bid, ask,
                        as_float(bid_values[2], "bid size", allow_zero=True) if len(bid_values) > 2 else 0,
                        as_float(ask_values[2], "ask size", allow_zero=True) if len(ask_values) > 2 else 0,
                        turnover,
                    ))
                except (KeyError, TypeError, ValueError):
                    # Preserve good instruments from a bulk response with one damaged row.
                    continue
            self._success(received)
            return tuple(result)
        except Exception as exc:
            self._failure(exc)
            return ()

    async def _get(self, instrument: Instrument, endpoint: str, params: Mapping[str, Any]) -> tuple[Mapping[str, Any], int, str]:
        if instrument.provider != self.provider_id:
            raise ValueError(f"instrument {instrument.instrument_id} does not belong to Kraken")
        path = f"/0/public/{endpoint}"
        try:
            payload, received = await self.http.get_json(path, params)
            root = require_mapping(payload, f"Kraken {endpoint}")
            errors = require_sequence(root.get("error", []), "Kraken errors")
            if errors:
                raise ValueError(f"Kraken {endpoint} error: {errors}")
            result = require_mapping(root.get("result"), "Kraken result")
            self._success(received)
            return result, received, f"{self.http.base_url}{path}"
        except Exception as exc:
            self._failure(exc)
            raise

    @staticmethod
    def _pair_payload(result: Mapping[str, Any], *, exclude: set[str] | None = None) -> Any:
        keys = [key for key in result if key not in (exclude or set())]
        if len(keys) != 1:
            raise ValueError(f"Kraken response must contain exactly one requested pair, got {keys}")
        return result[keys[0]]

    async def fetch_bbo(self, instrument: Instrument) -> NormalizedBBO:
        result, received, source = await self._get(instrument, "Ticker", {"pair": instrument.venue_symbol})
        item = require_mapping(self._pair_payload(result), "Kraken ticker pair")
        bid_values = require_sequence(item["b"], "Kraken bid")
        ask_values = require_sequence(item["a"], "Kraken ask")
        bbo = NormalizedBBO(
            metadata(instrument, exchange_timestamp_ms=received, received_at_ms=received, source=source,
                     exchange_time_kind="RECEIVE_TIME_FALLBACK",
                     event_id=f"ticker:{received}:{bid_values[0]}:{ask_values[0]}"),
            bid=as_float(bid_values[0], "bid"), ask=as_float(ask_values[0], "ask"),
            bid_size=as_float(bid_values[2] if len(bid_values) > 2 else 0, "bid size", allow_zero=True),
            ask_size=as_float(ask_values[2] if len(ask_values) > 2 else 0, "ask size", allow_zero=True),
            quote_volume_24h=(
                as_float(require_sequence(item["v"], "volume")[1], "volume", allow_zero=True)
                * as_float(require_sequence(item["c"], "last")[0], "last")
            ),
        )
        self._success(received, received)
        return bbo

    async def fetch_l2(self, instrument: Instrument, depth: int = 20) -> NormalizedL2:
        result, received, source = await self._get(
            instrument, "Depth", {"pair": instrument.venue_symbol, "count": max(1, min(int(depth), 500))}
        )
        item = require_mapping(self._pair_payload(result), "Kraken depth pair")

        def parse_levels(raw: Any, reverse: bool) -> tuple[NormalizedBookLevel, ...]:
            parsed = tuple(
                NormalizedBookLevel(as_float(level[0], "price"), as_float(level[1], "size", allow_zero=True))
                for level in require_sequence(raw, "Kraken book side")
            )
            return tuple(sorted(parsed, key=lambda level: level.price, reverse=reverse))

        bids = parse_levels(item.get("bids", []), True)
        asks = parse_levels(item.get("asks", []), False)
        level_times = [float(level[2]) for level in item.get("bids", []) + item.get("asks", []) if len(level) > 2]
        event_seconds = max(level_times or [received / 1000])
        event_time = int(event_seconds * 1000)
        book = NormalizedL2(
            metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                     exchange_time_kind="EXCHANGE_EVENT" if level_times else "RECEIVE_TIME_FALLBACK",
                     event_id=f"book:{event_time}:{bids[0].price if bids else ''}:{asks[0].price if asks else ''}"),
            bids=bids, asks=asks,
        )
        self._success(received, event_time)
        return book

    async def fetch_trades(self, instrument: Instrument, limit: int = 100) -> list[NormalizedTrade]:
        result, received, source = await self._get(instrument, "Trades", {"pair": instrument.venue_symbol})
        rows = require_sequence(self._pair_payload(result, exclude={"last"}), "Kraken trades")
        result_rows: list[NormalizedTrade] = []
        for index, raw in enumerate(rows[-max(1, min(int(limit), 1000)):]):
            item = require_sequence(raw, "Kraken trade")
            event_time = int(float(item[2]) * 1000)
            side = Side.BUY if str(item[3]).lower() == "b" else Side.SELL
            trade_id = str(item[6]) if len(item) > 6 and item[6] else f"{event_time}:{index}:{item[0]}:{item[1]}"
            result_rows.append(NormalizedTrade(
                metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received, source=source,
                         event_id=f"trade:{trade_id}"),
                side=side, price=as_float(item[0], "price"),
                size=as_float(item[1], "size", allow_zero=True), trade_id=trade_id,
            ))
        if result_rows:
            self._success(received, result_rows[-1].metadata.exchange_timestamp_ms)
        return result_rows

    async def fetch_candles(
        self, instrument: Instrument, interval: str, start_ms: int | None = None, limit: int = 300
    ) -> list[NormalizedCandle]:
        if interval not in _INTERVAL_MINUTES:
            raise ValueError(f"unsupported Kraken candle interval: {interval}")
        params: dict[str, Any] = {"pair": instrument.venue_symbol, "interval": _INTERVAL_MINUTES[interval]}
        if start_ms is not None:
            params["since"] = int(start_ms // 1000)
        result, received, source = await self._get(instrument, "OHLC", params)
        rows = require_sequence(self._pair_payload(result, exclude={"last"}), "Kraken OHLC")
        interval_ms = _INTERVAL_MINUTES[interval] * 60_000
        current = now_ms()
        candles: list[NormalizedCandle] = []
        for raw in rows[-max(1, min(int(limit), 720)):]:
            item = require_sequence(raw, "Kraken candle")
            open_time = as_int(item[0], "open time") * 1000
            close_time = open_time + interval_ms - 1
            candles.append(NormalizedCandle(
                metadata(instrument, exchange_timestamp_ms=close_time, received_at_ms=received, source=source,
                         event_id=f"candle:{interval}:{open_time}"),
                interval=interval, open_time_ms=open_time, close_time_ms=close_time,
                open=as_float(item[1], "open"), high=as_float(item[2], "high"),
                low=as_float(item[3], "low"), close=as_float(item[4], "close"),
                volume=as_float(item[6], "volume", allow_zero=True),
                trade_count=as_int(item[7], "trade count"), closed=close_time < current,
            ))
        if candles:
            self._success(received, candles[-1].metadata.exchange_timestamp_ms)
        return candles

    def parse_ticker_message(self, payload: Mapping[str, Any], received_at_ms: int) -> list[NormalizedBBO]:
        if payload.get("channel") != "ticker":
            return []
        result: list[NormalizedBBO] = []
        for raw in require_sequence(payload.get("data", []), "Kraken ticker data"):
            item = require_mapping(raw, "Kraken ticker")
            symbol = str(item["symbol"])
            candidates = [i for i in self.instruments.for_provider(self.provider_id) if i.venue_symbol == symbol]
            if len(candidates) != 1:
                continue
            instrument = candidates[0]
            event_time = iso_to_ms(str(item["timestamp"]))
            bid = as_float(item["bid"], "bid")
            ask = as_float(item["ask"], "ask")
            result.append(NormalizedBBO(
                metadata(instrument, exchange_timestamp_ms=event_time, received_at_ms=received_at_ms,
                         exchange_time_kind="EXCHANGE_EVENT" if payload.get("time") else "RECEIVE_TIME_FALLBACK",
                         source=str(self.config["websocket_url"]), event_id=f"ticker:{event_time}:{bid}:{ask}"),
                bid=bid, ask=ask,
                bid_size=as_float(item["bid_qty"], "bid_qty", allow_zero=True),
                ask_size=as_float(item["ask_qty"], "ask_qty", allow_zero=True),
                quote_volume_24h=as_float(item.get("volume", 0), "volume", allow_zero=True) *
                as_float(item.get("last", (bid + ask) / 2), "last"),
            ))
        if result:
            self._success(received_at_ms, result[-1].metadata.exchange_timestamp_ms)
        return result

    def health(self) -> ProviderHealth:
        return super().health(int(float(self.config.get("stale_after_seconds", 30)) * 1000))

    async def close(self) -> None:
        await self.http.close()
