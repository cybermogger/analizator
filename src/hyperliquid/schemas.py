from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import time
from typing import Any, Mapping, Sequence


class SchemaError(ValueError):
    """Raised when an official Hyperliquid response has an unexpected shape."""


def _float(value: Any, field: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    try:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("non-finite")
        return number
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"Invalid numeric field {field}: {value!r}") from exc


def _int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"Invalid integer field {field}: {value!r}") from exc


@dataclass(frozen=True)
class Candle:
    coin: str
    interval: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int
    received_at_ms: int = 0

    @property
    def exchange_timestamp_ms(self) -> int:
        """Exchange event time; candle open/close times remain authoritative for ordering."""
        return self.close_time

    @classmethod
    def from_api(cls, obj: Mapping[str, Any], received_at_ms: int | None = None) -> "Candle":
        required = {"s", "i", "t", "T", "o", "h", "l", "c", "v", "n"}
        missing = required.difference(obj)
        if missing:
            raise SchemaError(f"Candle missing fields: {sorted(missing)}")
        candle = cls(
            coin=str(obj["s"]), interval=str(obj["i"]),
            open_time=_int(obj["t"], "t"), close_time=_int(obj["T"], "T"),
            open=float(_float(obj["o"], "o")), high=float(_float(obj["h"], "h")),
            low=float(_float(obj["l"], "l")), close=float(_float(obj["c"], "c")),
            volume=float(_float(obj["v"], "v")), trades=_int(obj["n"], "n"),
            received_at_ms=int(received_at_ms or 0),
        )
        if not candle.coin or not candle.interval:
            raise SchemaError("Candle coin/interval must be non-empty")
        if candle.open_time < 0 or candle.close_time < candle.open_time:
            raise SchemaError("Invalid candle millisecond timestamps")
        if min(candle.open, candle.high, candle.low, candle.close) <= 0 or candle.volume < 0 or candle.trades < 0:
            raise SchemaError("Candle prices must be positive and volume/count non-negative")
        if candle.low > candle.high or min(candle.open, candle.close) < candle.low or max(candle.open, candle.close) > candle.high:
            raise SchemaError(f"Invalid OHLC relationship: {obj!r}")
        return candle

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AssetContext:
    coin: str
    timestamp: int
    mark: float
    oracle: float
    mid: float | None
    funding: float
    open_interest: float
    day_notional_volume: float
    premium: float | None
    previous_day_price: float
    received_at_ms: int = 0
    exchange_timestamp_ms: int | None = None

    @classmethod
    def from_api(
        cls,
        coin: str,
        timestamp: int,
        obj: Mapping[str, Any],
        *,
        received_at_ms: int | None = None,
        exchange_timestamp_ms: int | None = None,
    ) -> "AssetContext":
        required = {"markPx", "oraclePx", "funding", "openInterest", "dayNtlVlm", "prevDayPx"}
        missing = required.difference(obj)
        if missing:
            raise SchemaError(f"Asset context for {coin} missing fields: {sorted(missing)}")
        mark = float(_float(obj["markPx"], "markPx"))
        oracle = float(_float(obj["oraclePx"], "oraclePx"))
        supplied_premium = _float(obj.get("premium"), "premium", optional=True)
        premium = float(supplied_premium) if supplied_premium is not None else ((mark / oracle - 1.0) if oracle else None)
        context = cls(
            coin=coin, timestamp=timestamp, mark=mark, oracle=oracle,
            mid=_float(obj.get("midPx"), "midPx", optional=True),
            funding=float(_float(obj["funding"], "funding")),
            open_interest=float(_float(obj["openInterest"], "openInterest")),
            day_notional_volume=float(_float(obj["dayNtlVlm"], "dayNtlVlm")),
            premium=premium, previous_day_price=float(_float(obj["prevDayPx"], "prevDayPx")),
            received_at_ms=int(received_at_ms or timestamp),
            exchange_timestamp_ms=exchange_timestamp_ms,
        )
        if (not context.coin or context.timestamp < 0 or context.mark <= 0 or context.oracle <= 0 or
                (context.mid is not None and context.mid <= 0) or context.open_interest < 0 or
                context.day_notional_volume < 0 or context.previous_day_price <= 0):
            raise SchemaError(f"Invalid asset context values for {coin}")
        return context


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float
    orders: int

    @classmethod
    def from_api(cls, obj: Mapping[str, Any]) -> "BookLevel":
        level = cls(float(_float(obj.get("px"), "px")), float(_float(obj.get("sz"), "sz")), _int(obj.get("n"), "n"))
        if level.price <= 0 or level.size < 0 or level.orders < 0:
            raise SchemaError("Book level price must be positive and size/order count non-negative")
        return level


@dataclass(frozen=True)
class BookSnapshot:
    coin: str
    timestamp: int
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    received_at_ms: int = 0

    @property
    def exchange_timestamp_ms(self) -> int:
        return self.timestamp

    @classmethod
    def from_api(cls, obj: Mapping[str, Any], received_at_ms: int | None = None) -> "BookSnapshot":
        levels = obj.get("levels")
        if not isinstance(levels, Sequence) or isinstance(levels, (str, bytes)) or len(levels) != 2:
            raise SchemaError("L2 book levels must contain [bids, asks]")
        if any(not isinstance(side, Sequence) or isinstance(side, (str, bytes)) for side in levels):
            raise SchemaError("L2 book sides must be arrays")
        if any(not isinstance(item, Mapping) for side in levels for item in side):
            raise SchemaError("L2 book levels must be objects")
        bids = tuple(BookLevel.from_api(x) for x in levels[0])
        asks = tuple(BookLevel.from_api(x) for x in levels[1])
        coin = str(obj.get("coin", ""))
        if not coin:
            raise SchemaError("L2 book missing coin")
        if any(left.price < right.price for left, right in zip(bids, bids[1:])):
            raise SchemaError("L2 bids are not descending")
        if any(left.price > right.price for left, right in zip(asks, asks[1:])):
            raise SchemaError("L2 asks are not ascending")
        if bids and asks and bids[0].price >= asks[0].price:
            raise SchemaError("Crossed L2 book")
        timestamp = _int(obj.get("time"), "time")
        if timestamp < 0:
            raise SchemaError("Invalid L2 millisecond timestamp")
        return cls(coin, timestamp, bids, asks, int(received_at_ms or 0))


@dataclass(frozen=True)
class BboSnapshot:
    coin: str
    timestamp: int
    bid: BookLevel | None
    ask: BookLevel | None
    received_at_ms: int = 0

    @property
    def exchange_timestamp_ms(self) -> int:
        return self.timestamp

    @classmethod
    def from_api(cls, obj: Mapping[str, Any], received_at_ms: int | None = None) -> "BboSnapshot":
        raw = obj.get("bbo")
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or len(raw) != 2:
            raise SchemaError("BBO must contain [bid, ask]")
        if raw[0] is not None and not isinstance(raw[0], Mapping):
            raise SchemaError("BBO bid must be a level or null")
        if raw[1] is not None and not isinstance(raw[1], Mapping):
            raise SchemaError("BBO ask must be a level or null")
        bid = BookLevel.from_api(raw[0]) if isinstance(raw[0], Mapping) else None
        ask = BookLevel.from_api(raw[1]) if isinstance(raw[1], Mapping) else None
        if bid is not None and ask is not None and bid.price >= ask.price:
            raise SchemaError("Crossed BBO")
        coin = str(obj.get("coin", ""))
        if not coin:
            raise SchemaError("BBO missing coin")
        timestamp = _int(obj.get("time"), "time")
        if timestamp < 0:
            raise SchemaError("Invalid BBO millisecond timestamp")
        return cls(coin, timestamp, bid, ask, int(received_at_ms or 0))


@dataclass(frozen=True)
class Trade:
    coin: str
    side: str
    price: float
    size: float
    timestamp: int
    trade_id: int
    tx_hash: str
    received_at_ms: int = 0

    @property
    def exchange_timestamp_ms(self) -> int:
        return self.timestamp

    @property
    def is_aggressing_buy(self) -> bool:
        # Official notation: B=Bid/Buy, A=Ask/Sell; for trades side is aggressing side.
        return self.side == "B"

    @classmethod
    def from_api(cls, obj: Mapping[str, Any], received_at_ms: int | None = None) -> "Trade":
        side = str(obj.get("side", "")).upper()
        if side not in {"A", "B"}:
            raise SchemaError(f"Unexpected trade side {side!r}")
        trade = cls(
            coin=str(obj.get("coin", "")), side=side,
            price=float(_float(obj.get("px"), "px")), size=float(_float(obj.get("sz"), "sz")),
            timestamp=_int(obj.get("time"), "time"), trade_id=_int(obj.get("tid"), "tid"),
            tx_hash=str(obj.get("hash", "")), received_at_ms=int(received_at_ms or 0),
        )
        if not trade.coin or trade.price <= 0 or trade.size < 0 or trade.timestamp < 0:
            raise SchemaError("Invalid trade coin/price/size/timestamp")
        return trade


def parse_meta_and_contexts(payload: Any, timestamp: int) -> tuple[dict[str, Any], dict[str, AssetContext]]:
    if not isinstance(payload, Sequence) or len(payload) != 2:
        raise SchemaError("metaAndAssetCtxs must return [meta, contexts]")
    meta, raw_contexts = payload
    if not isinstance(meta, Mapping) or not isinstance(raw_contexts, Sequence):
        raise SchemaError("Invalid metaAndAssetCtxs types")
    universe = meta.get("universe")
    if not isinstance(universe, Sequence) or len(universe) != len(raw_contexts):
        raise SchemaError("Universe and asset context lengths differ")
    contexts: dict[str, AssetContext] = {}
    for asset, ctx in zip(universe, raw_contexts, strict=True):
        if not isinstance(asset, Mapping) or not isinstance(ctx, Mapping) or "name" not in asset:
            raise SchemaError("Invalid universe entry")
        coin = str(asset["name"])
        # metaAndAssetCtxs has no exchange event timestamp. ``timestamp`` is the
        # local successful-response time and is retained as the causal record key.
        contexts[coin] = AssetContext.from_api(coin, timestamp, ctx, received_at_ms=timestamp)
    return dict(meta), contexts


def parse_ws_message(payload: Any, received_at_ms: int | None = None) -> tuple[str, Any]:
    if not isinstance(payload, Mapping):
        raise SchemaError("WebSocket message must be an object")
    channel = payload.get("channel")
    if not isinstance(channel, str) or "data" not in payload:
        raise SchemaError("WebSocket message missing channel/data")
    data = payload["data"]
    if channel == "trades":
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)) or any(not isinstance(item, Mapping) for item in data):
            raise SchemaError("trades data must be a list")
        return channel, [Trade.from_api(item, received_at_ms) for item in data]
    if channel == "l2Book":
        return channel, BookSnapshot.from_api(data, received_at_ms)
    if channel == "bbo":
        if not isinstance(data, Mapping):
            raise SchemaError("bbo data must be an object")
        return channel, BboSnapshot.from_api(data, received_at_ms)
    if channel == "candle":
        if isinstance(data, Mapping):
            raw = [data]
        elif isinstance(data, Sequence) and not isinstance(data, (str, bytes)) and all(isinstance(item, Mapping) for item in data):
            raw = data
        else:
            raise SchemaError("candle data must be an object or list of objects")
        return channel, [Candle.from_api(item, received_at_ms) for item in raw]
    if channel == "allMids":
        if not isinstance(data, Mapping) or not isinstance(data.get("mids"), Mapping):
            raise SchemaError("allMids data must contain a mids object")
        mids = {str(coin): float(_float(value, f"mids.{coin}")) for coin, value in data["mids"].items()}
        if any(not coin or value <= 0 for coin, value in mids.items()):
            raise SchemaError("allMids contains an invalid coin or price")
        return channel, mids
    if channel == "activeAssetCtx":
        if not isinstance(data, Mapping) or not isinstance(data.get("ctx"), Mapping):
            raise SchemaError("activeAssetCtx data must contain coin and ctx")
        coin = str(data.get("coin", ""))
        if not coin:
            raise SchemaError("activeAssetCtx missing coin")
        received = received_at_ms or int(time.time() * 1000)
        exchange_timestamp = int(data["time"]) if data.get("time") is not None else None
        return channel, AssetContext.from_api(
            coin,
            exchange_timestamp or received,
            data["ctx"],
            received_at_ms=received,
            exchange_timestamp_ms=exchange_timestamp,
        )
    return channel, data
