from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from aiohttp import web


ROOT = Path(__file__).resolve().parents[2] / "dashboard"
SERVICE = web.AppKey("dashboard_service", object)


def snapshot(service: Any) -> dict[str, Any]:
    now = int(time.time() * 1000)
    cards = []
    for card in service.market_analysis.markets(30):
        candles = card.get("candles") or {}
        quality = card.get("candle_quality") or {}
        bbo = card.get("bbo") or {}
        cards.append({
            "instrument_id": card.get("instrument_id"), "base": card.get("base"),
            "venue": card.get("venue"), "quote": card.get("quote"),
            "market_type": card.get("market_type"), "observed_at_ms": card.get("observed_at_ms"),
            "observation_status": card.get("observation_status"),
            "observation_score": card.get("observation_score"),
            "price": bbo.get("mid"), "spread_bps": bbo.get("spread_bps"),
            "trends": {tf: candles.get(tf, {}).get("trend") for tf in ("15m", "1h", "4h")},
            "quality": {tf: quality.get(tf, {}).get("status") for tf in ("15m", "1h", "4h")},
        })
    events = []
    for event in service.events.latest(15):
        events.append({
            "event_id": event.get("event_id"), "event_type": event.get("event_type"),
            "summary": str(event.get("summary") or "")[:500],
            "published_at_ms": event.get("first_published_at_ms"),
            "received_at_ms": event.get("latest_received_at_ms"),
            "contradiction_state": event.get("contradiction_state"),
            "source_ids": event.get("source_ids", [])[:3],
            "evidence_urls": event.get("evidence_urls", [])[:3],
            "assets": [item.get("canonical_asset_id") for item in event.get("impacted_assets", [])[:8]],
        })
    candidates = []
    for candidate in service.onchain.latest(10):
        candidates.append({
            "chain_id": candidate.get("chain_id"),
            "contract_address": candidate.get("contract_address"),
            "symbol": candidate.get("symbol"), "status": candidate.get("status"),
            "as_of_ms": candidate.get("as_of_ms"),
            "liquidity_usd": candidate.get("liquidity_usd"),
            "proven_sellable_capacity_usd": candidate.get("proven_sellable_capacity_usd"),
            "official_identity_confirmed": candidate.get("official_identity_confirmed"),
            "risk_flags": candidate.get("risk_flags", [])[:5],
        })
    return {"as_of_ms": now, "markets": cards, "events": events,
        "candidates": candidates, "market_count": len(cards), "event_count": len(events),
        "semantics": "Наблюдение и экспериментальные гипотезы; рейтинг покрытия не является вероятностью прибыли."}


def create_dashboard_app(service: Any) -> web.Application:
    app = web.Application(client_max_size=1024)
    app[SERVICE] = service

    async def serve(request: web.Request, name: str) -> web.Response:
        path = ROOT / name
        response = web.FileResponse(path)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; base-uri 'none'; form-action 'none'"
        return response

    async def index(request: web.Request) -> web.Response:
        return await serve(request, "index.html")

    async def asset(request: web.Request) -> web.Response:
        return await serve(request, request.match_info["name"])

    async def api(request: web.Request) -> web.Response:
        response = web.json_response(snapshot(request.app[SERVICE]))
        response.headers["Cache-Control"] = "no-store"
        return response

    app.router.add_get("/", index)
    app.router.add_get("/{name:app\\.css|app\\.js}", asset)
    app.router.add_get("/api/snapshot", api)
    return app


async def start_dashboard(service: Any, settings: dict[str, Any]) -> web.AppRunner:
    host = str(settings.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("dashboard must bind to loopback only until authentication is configured")
    runner = web.AppRunner(create_dashboard_app(service), access_log=None, shutdown_timeout=5)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, int(settings.get("port", 8081))).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
