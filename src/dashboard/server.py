from __future__ import annotations

from pathlib import Path
import asyncio
import time
from typing import Any

from aiohttp import web


ROOT = Path(__file__).resolve().parents[2] / "dashboard"
SERVICE = web.AppKey("dashboard_service", object)


def snapshot(service: Any) -> dict[str, Any]:
    now = int(time.time() * 1000)
    cards = []
    signals = []
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
        for record in service.strategy_shadow.latest_plans(str(card.get("instrument_id") or "")):
            plan = record.get("plan") or {}
            if (plan.get("variant") != "disciplined" or plan.get("status") != "PROPOSED"
                    or plan.get("direction") != "LONG" or int(plan.get("expires_at_ms") or 0) <= now
                    or record.get("events")):
                continue
            signals.append({
                "instrument_id": plan.get("instrument_id"), "venue": plan.get("venue"),
                "quote": plan.get("quote"), "direction": "LONG",
                "observed_at_ms": plan.get("observed_at_ms"),
                "expires_at_ms": plan.get("expires_at_ms"),
                "entry_reference": plan.get("entry_reference"),
                "stop": plan.get("stop"), "target": plan.get("target"),
                "notional": plan.get("notional"),
            })
    events = []
    for event in service.events.latest(15):
        if (event.get("event_type") == "RUMOR" or event.get("authenticity_risk")
                or float(event.get("credibility") or 0) < .7):
            continue
        events.append({
            **(service.events.translator.view(str(event.get('summary') or '')) if hasattr(service.events,'translator') else {'summary_ru':None,'translation_status':'UNAVAILABLE'}),
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
    paper_journal = []
    for record in service.strategy_shadow.recent_disciplined_plans(20):
        plan = record["plan"]
        events_by_type = {event.get("type"): event for event in record["events"]
            if event.get("type") in {"FILLED", "CLOSED", "EXPIRED", "REJECTED"}}
        fill = events_by_type.get("FILLED") or {}
        close = events_by_type.get("CLOSED") or {}
        paper_journal.append({
            "plan_id": plan.get("plan_id"), "instrument_id": plan.get("instrument_id"),
            "venue": plan.get("venue"), "quote": plan.get("quote"),
            "status": record["state"], "observed_at_ms": plan.get("observed_at_ms"),
            "expires_at_ms": plan.get("expires_at_ms"),
            "entry_reference": plan.get("entry_reference"), "stop": plan.get("stop"),
            "target": plan.get("target"), "notional": plan.get("notional"),
            "filled_at_ms": fill.get("at"), "filled_price": fill.get("price"),
            "closed_at_ms": close.get("at"), "close_reason": close.get("reason"),
            "realized_net": close.get("net_pnl"),
        })
    forecasts = getattr(service, "market_forecasts", None)
    if forecasts and getattr(forecasts,'profit_policy',False):signals=[]
    estimates = forecasts.forecasts() if forecasts is not None else []
    return {"as_of_ms": now, "markets": cards, "signals": signals[:10], "events": events,
        "forecasts": estimates, "forecast_error": forecasts.last_error if forecasts else None,
        "forecast_performance": forecasts.performance() if forecasts else None,
        "forecast_horizons": ["5m", "15m", "1h", "2h"],
        "candidates": candidates, "paper_journal": paper_journal,
        "paper_status": "NOT_EVALUABLE", "market_count": len(cards), "event_count": len(events),
        "semantics": "Наблюдение и экспериментальные гипотезы; рейтинг покрытия не является вероятностью прибыли."}


def create_dashboard_app(service: Any) -> web.Application:
    app = web.Application(client_max_size=1024)
    app[SERVICE] = service

    async def serve(request: web.Request, name: str) -> web.Response:
        path = ROOT / name
        # Tiny static assets use a normal response. This avoids platform-specific
        # sendfile behaviour and makes desktop restarts/cache invalidation reliable.
        mime = {'.html':'text/html','.css':'text/css','.js':'application/javascript'}[path.suffix]
        response = web.Response(body=await asyncio.to_thread(path.read_bytes),content_type=mime,charset='utf-8')
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

    async def forecasts(request: web.Request) -> web.Response:
        engine = getattr(service, "market_forecasts", None)
        horizon = request.query.get("horizon", "15m")
        if horizon not in ("5m", "15m", "1h", "2h"):
            return web.json_response({"error": "Выберите горизонт 5m, 15m, 1h или 2h"}, status=400)
        rows = engine.forecasts(horizon) if engine else []
        identity = request.query.get("instrument")
        if identity:
            rows = [row for row in rows if row["instrument_id"] == identity]
        response = web.json_response({"forecasts": rows, "horizon": horizon})
        response.headers["Cache-Control"] = "no-store"
        return response

    async def best(request: web.Request) -> web.Response:
        from .ranking import best_signal
        engine=getattr(service,'market_forecasts',None)
        horizons=('1h','2h') if engine and engine.profit_policy else ('5m','15m','1h')
        rows=[row for horizon in horizons for row in (engine.forecasts(horizon) if engine else [])]
        response=web.json_response(best_signal(rows,int(time.time()*1000)))
        response.headers['Cache-Control']='no-store'
        return response

    async def x_status(request: web.Request) -> web.Response:
        import json
        watchlist=json.loads((ROOT.parent/'config'/'x_watchlist.json').read_text(encoding='utf-8'))
        events=getattr(service,'events',None)
        health=events.health() if events and hasattr(events,'health') else {}
        source=health.get('official_x',{})
        return web.json_response({'status':source.get('status','disabled'),'accounts':watchlist['accounts'],
            'note':'Список для мониторинга, не количественный рейтинг. Идентичность аккаунтов ещё требует проверки по числовым ID. Без X API поток не подключён.'})

    async def signal_audit(request: web.Request) -> web.Response:
        import json
        path=ROOT.parent/'reports'/'signal-audit.json'
        if not path.is_file():return web.json_response({'status':'PENDING','note':'Историческая проверка ещё выполняется.'})
        report=json.loads(await asyncio.to_thread(path.read_text,encoding='utf-8'))
        return web.json_response(report,headers={'Cache-Control':'no-store'})

    async def health(request: web.Request) -> web.Response:
        from collections import Counter
        from src.util.redaction import redact_sensitive_text
        market=getattr(service,'market_data',None)
        analysis=getattr(service,'market_analysis',None)
        providers=market.health() if market else {}
        return web.json_response({'providers':{name:{'status':value.get('status'),
            'error':redact_sensitive_text(str(value.get('last_error') or ''))[:200]} for name,value in providers.items()},
            'universe':len(market.membership) if market else 0,
            'selection_reasons':dict(Counter(reason for row in getattr(market,'universe_snapshot',{}).get('candidates',[]) for reason in row.get('reasons',[]))),
            'catalog':{name:{'status':state.get('status'),'count':state.get('entry_count',0),
                'error':redact_sensitive_text(str(state.get('last_error') or ''))[:200]}
                for name,state in getattr(market,'_catalog_state',{}).items()},
            'analysis_cycles':getattr(analysis,'_cycles',0),
            'analysis_error':redact_sensitive_text(str(getattr(analysis,'_last_cycle_error',None) or ''))[:200]})

    app.router.add_get("/", index)
    app.router.add_get("/{name:app\\.css|app\\.js}", asset)
    app.router.add_get("/api/snapshot", api)
    app.router.add_get("/api/forecasts", forecasts)
    app.router.add_get('/api/best',best)
    app.router.add_get('/api/x-status',x_status)
    app.router.add_get('/api/health',health)
    app.router.add_get('/api/signal-audit',signal_audit)
    return app


async def start_dashboard(service: Any, settings: dict[str, Any]) -> web.AppRunner:
    host = str(settings.get("host", "127.0.0.1"))
    if host not in ("127.0.0.1", "localhost", "::1") and not (
        host == "0.0.0.0" and settings.get("container_loopback_proxy") is True
    ):
        raise ValueError("dashboard must bind to loopback only until authentication is configured")
    runner = web.AppRunner(create_dashboard_app(service), access_log=None, shutdown_timeout=5)
    await runner.setup()
    try:
        await web.TCPSite(runner, host, int(settings.get("port", 8081))).start()
    except BaseException:
        await runner.cleanup()
        raise
    return runner
