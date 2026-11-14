from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from src.hyperliquid.schemas import BookSnapshot, Trade


class RawParquetRotator:
    """Buffers raw WS events and rotates compact Parquet files; SQLite stores only aggregates."""

    def __init__(self, root: str | Path, rotation_minutes: int = 60, retention_days: int = 14):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.rotation_ms = rotation_minutes * 60_000
        self.retention_seconds = max(1, retention_days) * 86_400
        self._started = int(time.time() * 1000)
        self._trades: list[dict[str, Any]] = []
        self._books: list[dict[str, Any]] = []

    def add_trades(self, trades: Iterable[Trade]) -> None:
        self._trades.extend(vars(t) for t in trades)

    def add_book(self, book: BookSnapshot) -> None:
        self._books.append({
            "coin": book.coin, "timestamp": book.timestamp,
            "exchange_timestamp_ms": book.exchange_timestamp_ms,
            "received_at_ms": book.received_at_ms,
            "bids": [[x.price, x.size, x.orders] for x in book.bids],
            "asks": [[x.price, x.size, x.orders] for x in book.asks],
        })

    def flush_if_due(self, force: bool = False) -> list[Path]:
        now = int(time.time() * 1000)
        if not force and now - self._started < self.rotation_ms:
            return []
        stamp = pd.Timestamp(self._started, unit="ms", tz="UTC").strftime("%Y%m%dT%H%M%SZ")
        written: list[Path] = []
        for kind, records in (("trades", self._trades), ("books", self._books)):
            if records:
                directory = self.root / kind
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / f"{kind}_{stamp}.parquet"
                pd.DataFrame(records).to_parquet(path, index=False, compression="zstd")
                records.clear()
                written.append(path)
        self._started = now
        cutoff = time.time() - self.retention_seconds
        for path in self.root.glob("*/*.parquet"):
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        return written


def aggregate_trades(trades: Iterable[Trade], window_ms: int = 300_000, now_ms: int | None = None) -> dict[str, Any]:
    now_ms = now_ms or int(time.time() * 1000)
    selected = [t for t in trades if t.timestamp >= now_ms - window_ms]
    if not selected:
        return {"available": False, "buy_volume": 0.0, "sell_volume": 0.0, "delta": 0.0, "trade_count": 0, "hvn": [], "lvn": []}
    sizes = np.array([t.size for t in selected], dtype=float)
    prices = np.array([t.price for t in selected], dtype=float)
    buy = np.array([t.side == "B" for t in selected])
    buy_volume, sell_volume = float(sizes[buy].sum()), float(sizes[~buy].sum())
    threshold = float(np.quantile(sizes, 0.95)) if len(sizes) >= 20 else float(sizes.max())
    large = sizes >= threshold
    bins = min(30, max(5, int(np.sqrt(len(prices)))))
    hist, edges = np.histogram(prices, bins=bins, weights=sizes)
    centers = (edges[:-1] + edges[1:]) / 2
    order = np.argsort(hist)
    poc_idx = int(np.argmax(hist))
    return {
        "available": True, "buy_volume": buy_volume, "sell_volume": sell_volume,
        "delta": buy_volume - sell_volume, "trade_count": len(selected),
        "aggressive_imbalance": (buy_volume - sell_volume) / (buy_volume + sell_volume + 1e-12),
        "large_buy_volume": float(sizes[large & buy].sum()), "large_sell_volume": float(sizes[large & ~buy].sum()),
        "poc": float(centers[poc_idx]), "hvn": [float(centers[i]) for i in order[-3:][::-1]],
        "lvn": [float(centers[i]) for i in order[:3]],
    }
