from __future__ import annotations

import json

import pytest

from src.hyperliquid.schemas import BboSnapshot, Candle, SchemaError, parse_meta_and_contexts, parse_ws_message
from src.hyperliquid.websocket import LiveMarketState


def test_rest_candle_and_meta_parsers():
    candle = Candle.from_api({"T": 1681924499999, "c": "29258.0", "h": "29309.0", "i": "15m", "l": "29250.0", "n": 189, "o": "29295.0", "s": "BTC", "t": 1681923600000, "v": "0.98639"})
    assert candle.coin == "BTC" and candle.close == 29258
    payload = [{"universe": [{"name": "SOL", "szDecimals": 2}]}, [{"dayNtlVlm": "100", "funding": "0.0001", "markPx": "100", "midPx": "99.9", "openInterest": "5", "oraclePx": "100.1", "premium": "-0.001", "prevDayPx": "98"}]]
    _, contexts = parse_meta_and_contexts(payload, 123)
    assert contexts["SOL"].open_interest == 5


def test_websocket_parsers():
    channel, trades = parse_ws_message({"channel": "trades", "data": [{"coin": "SOL", "side": "B", "px": "100", "sz": "2", "hash": "0x1", "time": 123, "tid": 7, "users": ["a", "b"]}]})
    assert channel == "trades" and trades[0].price == 100 and trades[0].is_aggressing_buy
    _, sells = parse_ws_message({"channel": "trades", "data": [{"coin": "SOL", "side": "A", "px": "100", "sz": "1", "hash": "0x2", "time": 124, "tid": 8, "users": ["a", "b"]}]})
    assert sells[0].is_aggressing_buy is False
    channel, candles = parse_ws_message({"channel": "candle", "data": {"T": 199, "c": "101", "h": "102", "i": "1m", "l": "99", "n": 2, "o": "100", "s": "SOL", "t": 100, "v": "4"}})
    assert channel == "candle" and candles[0].interval == "1m"
    channel, bbo = parse_ws_message({"channel": "bbo", "data": {"coin": "SOL", "time": 123, "bbo": [{"px": "99", "sz": "2", "n": 1}, {"px": "101", "sz": "3", "n": 2}]}})
    assert channel == "bbo" and isinstance(bbo, BboSnapshot) and bbo.bid.price == 99 and bbo.ask.price == 101
    channel, mids = parse_ws_message({"channel": "allMids", "data": {"mids": {"SOL": "100.5"}}})
    assert channel == "allMids" and mids == {"SOL": 100.5}
    channel, context = parse_ws_message({"channel": "activeAssetCtx", "data": {"coin": "SOL", "ctx": {"dayNtlVlm": "100", "funding": "0.0001", "markPx": "100", "midPx": "99.9", "openInterest": "5", "oraclePx": "100.1", "premium": "-0.001", "prevDayPx": "98"}}}, received_at_ms=456)
    assert channel == "activeAssetCtx" and context.timestamp == 456 and context.received_at_ms == 456
    assert context.mark == 100 and context.mid == 99.9 and context.oracle == 100.1
    assert context.funding == 0.0001 and context.open_interest == 5


def test_parser_rejects_missing_fields():
    with pytest.raises(SchemaError): Candle.from_api({"s": "SOL"})


def test_replayed_trade_is_deduplicated():
    _, trades = parse_ws_message({"channel": "trades", "data": [{"coin": "SOL", "side": "B", "px": "100", "sz": "2", "hash": "0x1", "time": 123, "tid": 7, "users": ["a", "b"]}]})
    state = LiveMarketState()
    assert len(state.apply("trades", trades)) == 1
    assert state.apply("trades", trades) == []
    assert len(state.trades["SOL"]) == 1
