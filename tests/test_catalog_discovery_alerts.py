import asyncio

from src.alerts.catalog_discovery import CatalogDiscoveryAlerts
from src.data.storage import SQLiteStorage
from src.providers.types import Instrument, MarketCatalogEntry, MarketCatalogScope, MarketType


def catalog_entry(base, now, *, turnover=2_000_000, spread=.001):
    instrument = Instrument(f"binance:spot:{base}-USDT", f"unresolved:binance:{base}:USDT",
        "binance", "binance-spot", MarketType.SPOT, base, "USDT", f"{base}USDT", 6, 6)
    return MarketCatalogEntry(instrument, "spot", "official-exchange-info", now,
        None, turnover, 1.0, bid=1.0, ask=1.0 + spread, active=True, admitted=True)


def scope(now, *entries):
    return MarketCatalogScope("binance", "spot", "official-exchange-info", now,
        tuple(entries), True)


def test_catalog_discovery_bootstrap_new_pair_and_restart_dedup(tmp_path):
    db = SQLiteStorage(tmp_path / "catalog.db")
    now = 1_800_000_000_000
    sent = []

    async def fake_send(key, message, at):
        sent.append(message)
        return True, "synthetic-id"

    alerts = CatalogDiscoveryAlerts(db, {"enabled": True}, fake_send)
    old = catalog_entry("BTC", now)
    new = catalog_entry("MEME", now + 1000)
    assert asyncio.run(alerts.observe(scope(now, old), now)) == 0
    assert asyncio.run(alerts.observe(scope(now + 1000, old, new), now + 1000)) == 1
    assert "binance:spot:MEME-USDT" in sent[0]
    assert "не доказательство запуска нового токена" in sent[0]
    restarted = CatalogDiscoveryAlerts(db, {"enabled": True}, fake_send)
    assert asyncio.run(restarted.observe(scope(now + 2000, old, new), now + 2000)) == 0
    assert len(sent) == 1
    db.close()


def test_catalog_discovery_waits_for_liquidity_and_spread(tmp_path):
    db = SQLiteStorage(tmp_path / "catalog.db")
    now = 1_800_000_000_000
    sent = []

    async def fake_send(key, message, at):
        sent.append(message)
        return True, "synthetic-id"

    alerts = CatalogDiscoveryAlerts(db, {"enabled": True}, fake_send)
    old = catalog_entry("BTC", now)
    assert asyncio.run(alerts.observe(scope(now, old), now)) == 0
    weak = catalog_entry("MEME", now + 1000, turnover=10_000, spread=.02)
    assert asyncio.run(alerts.observe(scope(now + 1000, old, weak), now + 1000)) == 0
    stronger = catalog_entry("MEME", now + 2000)
    assert asyncio.run(alerts.observe(scope(now + 2000, old, stronger), now + 2000)) == 1
    assert len(sent) == 1
    db.close()
