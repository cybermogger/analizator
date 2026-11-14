from __future__ import annotations

import time

import pytest

from src.features.orderbook import orderbook_features
from src.hyperliquid.rest import HyperliquidREST
from src.hyperliquid.schemas import BookSnapshot
from src.hyperliquid.websocket import LiveMarketState
from src.signals.quality import INTERVAL_MS, enrich_signal_quality


def quality_snapshot(
    now: int,
    *,
    interval: str = "5m",
    candle_open: int | None = None,
    candle_received: int | None = None,
    book_exchange: int | None = None,
    book_received: int | None = None,
    context_received: int | None = None,
    orderbook: dict | None = None,
) -> dict:
    interval_ms = INTERVAL_MS[interval]
    current_open = now - now % interval_ms
    probabilities = {"LONG": 55.0, "SHORT": 20.0, "WAIT": 25.0}
    components = {
        "technical": {"probabilities": probabilities, "quality": 0.8},
        "orderbook": {"probabilities": probabilities, "quality": 0.8},
    }
    return enrich_signal_quality(
        mark=100.0,
        timestamp=now,
        context_timestamp=now,
        context_received_at_ms=context_received if context_received is not None else now - 100,
        source_interval=interval,
        source_row={
            "open_time": candle_open if candle_open is not None else current_open,
            "received_at_ms": candle_received if candle_received is not None else now - 100,
            "atr_14": 1.0,
            "atr_pct": 0.01,
            "rolling_std_20": 0.005,
        },
        horizons={"15m": probabilities},
        primary_horizon="15m",
        direction="LONG",
        details={"15m": {"components": components, "forward_return_quantiles": {}}},
        plans={
            "LONG": {"expected_zone": (101.0, 102.0), "invalidation": 99.0},
            "SHORT": {"expected_zone": (98.0, 99.0), "invalidation": 101.0},
            "WAIT": {"expected_zone": (), "invalidation": None},
        },
        regime="RANGE",
        orderbook=orderbook or {
            "available": True,
            "timestamp": book_exchange if book_exchange is not None else now - 200_000,
            "exchange_timestamp_ms": book_exchange if book_exchange is not None else now - 200_000,
            "received_at_ms": book_received if book_received is not None else now - 100,
        },
        tradeflow={"available": False},
        btc_context={"available": False},
        settings={"max_data_age_seconds": 180, "horizon_steps": 1},
    )


def test_exchange_clock_skew_does_not_make_fresh_book_stale():
    now = 1_728_002_400_000
    result = quality_snapshot(now, book_exchange=now - 200_000, book_received=now - 200)
    freshness = result["data_freshness"]
    assert freshness["status"] == "FRESH"
    assert freshness["book_receive_age_seconds"] == pytest.approx(0.2)
    assert freshness["exchange_clock_skew_seconds"] == pytest.approx(199.8)
    assert result["confidence"] > 0


def test_fresh_exchange_timestamp_cannot_hide_old_receive_time():
    now = 1_728_002_400_000
    result = quality_snapshot(now, book_exchange=now - 1_000, book_received=now - 300_000)
    assert result["data_freshness"]["status"] == "STALE"
    assert result["data_freshness"]["book_receive_age_seconds"] == pytest.approx(300.0)
    assert result["confidence"] == 0


@pytest.mark.asyncio
async def test_fresh_rest_fallback_is_fresh(monkeypatch):
    now = int(time.time() * 1000)
    interval_ms = INTERVAL_MS["5m"]
    current_open = now - now % interval_ms
    context_payload = {
        "dayNtlVlm": "100", "funding": "0.0001", "markPx": "100",
        "midPx": "100", "openInterest": "5", "oraclePx": "100.1",
        "premium": "-0.001", "prevDayPx": "99",
    }

    async def fake_post(body):
        if body["type"] == "metaAndAssetCtxs":
            return [{"universe": [{"name": "SOL"}]}, [context_payload]]
        if body["type"] == "candleSnapshot":
            return [{
                "s": "SOL", "i": "5m", "t": current_open,
                "T": current_open + interval_ms - 1, "o": "99", "h": "101",
                "l": "98", "c": "100", "v": "10", "n": 5,
            }]
        if body["type"] == "l2Book":
            return {
                "coin": "SOL", "time": now - 200_000,
                "levels": [
                    [{"px": "99", "sz": "2", "n": 1}],
                    [{"px": "101", "sz": "2", "n": 1}],
                ],
            }
        raise AssertionError(body)

    rest = HyperliquidREST("https://api.hyperliquid.xyz/info", timeout=1)
    monkeypatch.setattr(rest, "_post", fake_post)
    _, contexts = await rest.meta_and_asset_contexts()
    candles = await rest.candles("SOL", "5m", current_open, now)
    book = await rest.l2_book("SOL")
    checked_at = int(time.time() * 1000)
    result = quality_snapshot(
        checked_at,
        candle_open=candles[-1].open_time,
        candle_received=candles[-1].received_at_ms,
        context_received=contexts["SOL"].received_at_ms,
        orderbook=orderbook_features(book),
    )
    assert result["data_freshness"]["status"] == "FRESH"
    assert contexts["SOL"].received_at_ms > 0
    assert candles[-1].received_at_ms > 0
    assert book.received_at_ms > 0


def test_stale_ws_book_is_stale_even_when_connected():
    now = 1_728_002_400_000
    book = BookSnapshot.from_api({
        "coin": "SOL", "time": now - 1_000,
        "levels": [
            [{"px": "99", "sz": "2", "n": 1}],
            [{"px": "101", "sz": "2", "n": 1}],
        ],
    })
    state = LiveMarketState(connected=True)
    state.apply("l2Book", book, received_at_ms=now - 300_000)
    result = quality_snapshot(now, orderbook=orderbook_features(state.books["SOL"]))
    assert state.book_received_at_ms["SOL"] == now - 300_000
    assert result["data_freshness"]["status"] == "STALE"


def test_current_four_hour_candle_is_fresh_when_received_recently():
    interval_ms = INTERVAL_MS["4h"]
    interval_open = 1_728_000_000_000 - 1_728_000_000_000 % interval_ms
    now = interval_open + 40 * 60_000
    result = quality_snapshot(
        now,
        interval="4h",
        candle_open=interval_open,
        candle_received=now - 250,
    )
    freshness = result["data_freshness"]
    assert freshness["status"] == "FRESH"
    assert freshness["candle_interval_valid"] is True
    assert freshness["candle_receive_age_seconds"] == pytest.approx(0.25)
