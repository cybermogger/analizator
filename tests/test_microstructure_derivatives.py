from __future__ import annotations

import pandas as pd

import os
import time

from src.data.aggregation import RawParquetRotator
from src.features.derivatives import derivatives_features
from src.features.orderbook import orderbook_features
from src.hyperliquid.schemas import AssetContext, BookSnapshot, Trade


def test_orderbook_imbalance():
    book = BookSnapshot.from_api({
        "coin": "SOL", "time": 123,
        "levels": [
            [{"px": "99.9", "sz": "20", "n": 2}, {"px": "99.8", "sz": "10", "n": 1}],
            [{"px": "100.1", "sz": "5", "n": 1}, {"px": "100.2", "sz": "5", "n": 1}],
        ],
    })
    values = orderbook_features(book)
    assert values["best_bid"] == 99.9
    assert values["best_ask"] == 100.1
    assert values["imbalance"] == 0.5
    assert values["weighted_imbalance"] > 0
    assert 99.9 < values["microprice"] < 100.1


def test_oi_change_and_price_oi_combination():
    now = 10_000_000
    history = pd.DataFrame([
        {"timestamp": now - 3_600_001, "open_interest": 100, "funding": 0.0001, "premium": 0.001},
        {"timestamp": now - 300_001, "open_interest": 105, "funding": 0.0001, "premium": 0.001},
    ])
    context = AssetContext("SOL", now, 98, 98.1, 98, 0.00012, 110, 1_000_000, 0.0012, 101)
    values = derivatives_features(context, history, {"1h": -0.02, "5m": -0.01})
    assert abs(values["oi_change_1h"] - 0.10) < 1e-9
    assert abs(values["funding_change_1h"] - 0.00002) < 1e-12
    assert abs(values["premium_change_1h"] - 0.0002) < 1e-12
    assert values["price_oi_combinations"]["1h"] == "NEW_SHORT_POSITIONING_PROXY"


def test_raw_retention_and_utc_filename(tmp_path):
    old = tmp_path / "trades" / "old.parquet"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"expired")
    expired = time.time() - 2 * 86_400
    os.utime(old, (expired, expired))

    writer = RawParquetRotator(tmp_path, retention_days=1)
    writer._started = 1_700_000_000_000
    writer.add_trades([Trade("SOL", "B", 100.0, 1.0, 1_700_000_000_001, 7, "0x1")])
    written = writer.flush_if_due(force=True)

    assert not old.exists()
    assert len(written) == 1
    assert written[0].name == "trades_20231114T221320Z.parquet"
