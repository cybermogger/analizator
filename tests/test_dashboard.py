import asyncio
import time
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
        "credibility": .9, "authenticity_risk": False,
        "source_ids": ["official"], "evidence_urls": ["https://example.invalid/news"],
        "impacted_assets": [{"canonical_asset_id": "solana:bonk-contract"}],
        "payload_json": "must-not-leak",
    }])
    onchain = SimpleNamespace(latest=lambda limit: [{"chain_id": "solana",
        "contract_address": "verified-mint", "symbol": "NEW", "status": "OBSERVE",
        "risk_flags": ["liquidity_unverified"], "private_payload": "must-not-leak"}])
    strategy = SimpleNamespace(latest_plans=lambda iid: [], recent_disciplined_plans=lambda limit: [])
    return SimpleNamespace(market_analysis=market, events=events, onchain=onchain,
        strategy_shadow=strategy)


def test_snapshot_whitelists_public_fields():
    result = snapshot(fake_service())
    assert result["markets"][0]["base"] == "BONK"
    assert result["events"][0]["assets"] == ["solana:bonk-contract"]
    assert result["candidates"][0]["contract_address"] == "verified-mint"
    assert "must-not-leak" not in str(result)
    assert "не является вероятностью прибыли" in result["semantics"]


def test_snapshot_exposes_only_fresh_unfilled_disciplined_plan():
    service = fake_service()
    now = int(time.time() * 1000)
    plan = {"variant": "disciplined", "status": "PROPOSED", "direction": "LONG",
        "instrument_id": "binance:spot:BONK-USDT", "venue": "binance-spot", "quote": "USDT",
        "observed_at_ms": now - 1000, "expires_at_ms": now + 30_000,
        "entry_reference": .00001, "stop": .000009, "target": .000012,
        "notional": 100, "internal_secret": "must-not-leak"}
    service.strategy_shadow.latest_plans = lambda iid: [{"plan": plan, "events": []}]
    result = snapshot(service)
    assert len(result["signals"]) == 1
    assert "must-not-leak" not in str(result)
    service.strategy_shadow.latest_plans = lambda iid: [{"plan": plan, "events": [{"type": "FILLED"}]}]
    assert snapshot(service)["signals"] == []


def test_snapshot_paper_journal_whitelists_closed_virtual_outcome():
    service = fake_service()
    service.strategy_shadow.recent_disciplined_plans = lambda limit: [{
        "plan": {"plan_id": "plan-1", "instrument_id": "binance:spot:BONK-USDT",
            "venue": "binance-spot", "quote": "USDT", "observed_at_ms": 1000,
            "entry_reference": .00001, "stop": .000009, "target": .000012,
            "notional": 100, "private_payload": "must-not-leak"},
        "state": "CLOSED", "events": [
            {"type": "FILLED", "at": 2000, "price": .0000101, "secret": "must-not-leak"},
            {"type": "CLOSED", "at": 3000, "net_pnl": -1.25, "reason": "STOP"}],
    }]
    result = snapshot(service)
    assert result["paper_status"] == "NOT_EVALUABLE"
    assert result["paper_journal"][0]["realized_net"] == -1.25
    assert result["paper_journal"][0]["status"] == "CLOSED"
    assert "must-not-leak" not in str(result)


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
