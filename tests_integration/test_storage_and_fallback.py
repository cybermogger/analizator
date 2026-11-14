from __future__ import annotations

import copy
import time

import numpy as np
import pytest

from src.hyperliquid.schemas import AssetContext, Candle
from src.service.analyzer import AnalyzerService, INTERVAL_MS
from src.service.config import load_config


class OfflineREST:
    async def __aenter__(self): return self
    async def __aexit__(self, *_args): return None
    async def candles(self, *_args, **_kwargs): raise OSError("network disabled")
    async def meta_and_asset_contexts(self): raise OSError("network disabled")
    async def l2_book(self, *_args, **_kwargs): raise OSError("network disabled")


def seed_cache(service: AnalyzerService) -> None:
    now = int(time.time() * 1000)
    for coin_index, coin in enumerate(("SOL", "HYPE", "BTC")):
        base = 100.0 + coin_index * 10
        for interval in service.config["market"]["timeframes"]:
            step = INTERVAL_MS[interval]
            candles = []
            for index in range(230):
                close = base + index * 0.01 + np.sin(index / 10) * 0.1
                open_price = close - 0.02
                candles.append(Candle(coin, interval, now - (230 - index) * step, now - (229 - index) * step - 1, open_price, close + 0.1, open_price - 0.1, close, 100 + index, 10 + index))
            service.repository.upsert_candles(candles)
        service.repository.upsert_contexts([AssetContext(coin, now, base + 2, base + 2.1, base + 2.05, 0.0001, 1000, 1_000_000, -0.001, base)])


@pytest.mark.asyncio
async def test_network_outage_uses_cache_and_keeps_both_assets_running(tmp_path, monkeypatch):
    original, _ = load_config("config.yaml")
    config = copy.deepcopy(original)
    config["storage"]["sqlite_path"] = str(tmp_path / "analyzer.db")
    config["storage"]["raw_path"] = str(tmp_path / "raw")
    config["storage"]["models_path"] = str(tmp_path / "models")
    config["storage"]["logs_path"] = str(tmp_path / "logs")
    service = AnalyzerService(config)
    seed_cache(service)
    monkeypatch.setattr(service, "_rest", lambda: OfflineREST())
    try:
        signals = await service.analysis_cycle(send_telegram=False)
        assert [signal["coin"] for signal in signals] == ["SOL", "HYPE"]
        assert all(signal["model_status"] == "MODEL_WARMING_UP" for signal in signals)
        assert all(abs(sum(signal["horizons"][h].values()) - 100) < 1e-8 for signal in signals for h in ("15m", "1h", "4h"))
        assert all(signal["details"][h]["weights"]["ml"] == 0 for signal in signals for h in ("15m", "1h", "4h"))
    finally:
        await service.close()


def test_database_init_and_migration_are_idempotent(tmp_path):
    from src.data.storage import SCHEMA_VERSION, SQLiteStorage
    path = tmp_path / "db.sqlite"
    first = SQLiteStorage(path)
    assert first.health()["schema_version"] == SCHEMA_VERSION
    first.close()
    second = SQLiteStorage(path)
    assert second.health()["integrity"] == "ok"
    assert second.query("SELECT COUNT(*) AS n FROM schema_migrations")[0]["n"] == 1
    second.close()
