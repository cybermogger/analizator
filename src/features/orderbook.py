from __future__ import annotations

from typing import Any

import numpy as np

from src.hyperliquid.schemas import BookSnapshot


def orderbook_features(book: BookSnapshot) -> dict[str, Any]:
    if not book.bids or not book.asks:
        return {"available": False}
    best_bid, best_ask = book.bids[0].price, book.asks[0].price
    mid = (best_bid + best_ask) / 2
    bid_sizes = np.array([x.size for x in book.bids], dtype=float)
    ask_sizes = np.array([x.size for x in book.asks], dtype=float)
    bid_prices = np.array([x.price for x in book.bids], dtype=float)
    ask_prices = np.array([x.price for x in book.asks], dtype=float)
    bid_depth, ask_depth = float(bid_sizes.sum()), float(ask_sizes.sum())
    weights_bid = 1 / (1 + np.abs(bid_prices / mid - 1) * 10_000)
    weights_ask = 1 / (1 + np.abs(ask_prices / mid - 1) * 10_000)
    weighted_bid, weighted_ask = float((bid_sizes * weights_bid).sum()), float((ask_sizes * weights_ask).sum())
    total_depth = bid_depth + ask_depth
    weighted_total = weighted_bid + weighted_ask
    top_total = bid_sizes[0] + ask_sizes[0]
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth else 0.0
    weighted_imbalance = (weighted_bid - weighted_ask) / weighted_total if weighted_total else 0.0
    microprice = (best_ask * bid_sizes[0] + best_bid * ask_sizes[0]) / top_total if top_total else mid
    distances = (25, 50, 100)
    depth_bands: dict[str, float] = {}
    for bps in distances:
        b = float(bid_sizes[bid_prices >= mid * (1 - bps / 10_000)].sum())
        a = float(ask_sizes[ask_prices <= mid * (1 + bps / 10_000)].sum())
        depth_bands[f"imbalance_{bps}bps"] = (b - a) / (b + a + 1e-12)
    bid_slope = np.polyfit((mid - bid_prices) / mid * 10_000, np.cumsum(bid_sizes), 1)[0] if len(bid_sizes) > 2 else 0
    ask_slope = np.polyfit((ask_prices - mid) / mid * 10_000, np.cumsum(ask_sizes), 1)[0] if len(ask_sizes) > 2 else 0
    all_sizes = np.r_[bid_sizes, ask_sizes]
    wall_threshold = float(np.quantile(all_sizes, 0.9)) if len(all_sizes) >= 5 else float(all_sizes.max())
    walls = ([{"side": "bid", "price": float(p), "size": float(s)} for p, s in zip(bid_prices, bid_sizes) if s >= wall_threshold] +
             [{"side": "ask", "price": float(p), "size": float(s)} for p, s in zip(ask_prices, ask_sizes) if s >= wall_threshold])
    return {
        "available": True,
        # ``timestamp`` is kept for backward-compatible event-time sorting.
        "timestamp": book.timestamp,
        "exchange_timestamp_ms": book.exchange_timestamp_ms,
        "received_at_ms": book.received_at_ms,
        "best_bid": best_bid, "best_ask": best_ask,
        "spread": best_ask - best_bid, "relative_spread": (best_ask - best_bid) / mid,
        "bid_depth": bid_depth, "ask_depth": ask_depth, "imbalance": imbalance,
        "weighted_imbalance": weighted_imbalance, "microprice": float(microprice),
        "book_pressure": float((microprice - mid) / mid * 10_000),
        "depth_slope": float((bid_slope - ask_slope) / (abs(bid_slope) + abs(ask_slope) + 1e-12)),
        "walls": walls, **depth_bands,
    }


def book_momentum(current: dict[str, Any], previous: dict[str, Any] | None) -> float:
    if not current.get("available") or not previous or not previous.get("available"):
        return 0.0
    return float(current.get("weighted_imbalance", 0) - previous.get("weighted_imbalance", 0))
