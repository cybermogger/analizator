import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer
import pytest

from src.dashboard.server import create_dashboard_app, snapshot, start_dashboard


def fake_service():
    market = SimpleNamespace(markets=lambda limit: [{
        "instrument_id": "binance:spot:BONK-USDT", "base": "BONK", "venue": "binance-spot",
        "quote": "USDT", "market_type": "spot", "observed_at_ms": 1_800_000_000_000,
        "observation_status": "OBSERVED", "observation_score": 75.0,
        "bbo": {"mid": .00001, "spread_bps": 12},
        "candles": {"15m": {"trend": "UP"}},
        "candle_quality": {"15m": {"status": "FRESH"}},
        "internal_secret": "must-not-leak",
    }])
    events = SimpleNamespace(latest=lambda limit: [{
        "event_id": "evt-1", "event_type": "LISTING", "summary": "Official listing",
        "source_ids": ["official"], "evidence_urls": ["https://example.invalid/news"],
        "impacted_assets": [{"canonical_asset_id": "solana:bonk-contract"}],
        "payload_json": "must-not-leak",
    }])
    onchain = SimpleNamespace(latest=lambda limit: [{"chain_id": "solana",
        "contract_address": "verified-mint", "symbol": "NEW", "status": "OBSERVE",
        "risk_flags": ["liquidity_unverified"], "private_payload": "must-not-leak"}])
    return SimpleNamespace(market_analysis=market, events=events, onchain=onchain)


def test_snapshot_whitelists_public_fields():
    result = snapshot(fake_service())
    assert result["markets"][0]["base"] == "BONK"
    assert result["events"][0]["assets"] == ["solana:bonk-contract"]
    assert result["candidates"][0]["contract_address"] == "verified-mint"
    assert "must-not-leak" not in str(result)
    assert "не является вероятностью прибыли" in result["semantics"]


@pytest.mark.asyncio
async def test_local_dashboard_serves_static_and_snapshot():
    async with TestServer(create_dashboard_app(fake_service())) as server:
        async with TestClient(server) as client:
            api = await client.get("/api/snapshot")
            assert api.status == 200
            assert (await api.json())["markets"][0]["base"] == "BONK"
            assert api.headers["Cache-Control"] == "no-store"
            index = await client.get("/")
            assert index.status == 200
            assert "Карта рынков" in await index.text()
            assert "default-src 'self'" in index.headers["Content-Security-Policy"]
            assert (await client.get("/app.js")).status == 200
            assert (await client.get("/../config.yaml")).status == 404


def test_dashboard_rejects_public_bind():
    with pytest.raises(ValueError, match="loopback"):
        asyncio.run(start_dashboard(fake_service(), {"host": "0.0.0.0", "port": 8081}))
