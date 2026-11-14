from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

from src.util.redaction import redact_sensitive_text

from .auth import InitDataError, TelegramInitDataAuthenticator
from .urls import valid_web_app_url


WEB_ROOT = Path(__file__).resolve().parents[2] / "webapp"
MAX_ITEMS = 30
SERVICE_KEY = web.AppKey("analyzer_service", object)
AUTH_KEY = web.AppKey("mini_app_auth", TelegramInitDataAuthenticator)


def _timestamp(value: Any) -> int | None:
    try:
        number = int(value)
        return number if number > 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _signal(row: dict[str, Any], coin: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(row["payload_json"])
        horizon = str(payload["primary_horizon"])
        probabilities = payload["horizons"][horizon]
        mark = float(payload["mark"])
        direction_probability = _number(payload.get("probability"))
        confidence = _number(payload.get("confidence", 0))
        normalized_probabilities = {key: _number(probabilities.get(key)) for key in ("LONG", "SHORT", "WAIT")}
        if (
            not mark > 0 or direction_probability is None or not 0 <= direction_probability <= 100
            or confidence is None or not 0 <= confidence <= 100
            or any(value is None or not 0 <= value <= 100 for value in normalized_probabilities.values())
            or not math.isclose(sum(normalized_probabilities.values()), 100, abs_tol=0.1)
        ):
            return None
        return {
            "asset": coin,
            "price": mark,
            "direction": str(payload.get("direction", "WAIT")),
            "probability_percent": direction_probability,
            "probabilities": normalized_probabilities,
            "horizon": horizon,
            "confidence_percent": confidence,
            "freshness": dict(payload.get("data_freshness", {})),
            "regime": str(payload.get("regime", "UNKNOWN")),
            "model_status": str(row.get("model_status", "UNKNOWN")),
            "model_version": str(payload.get("production_model_version", "UNKNOWN")),
            "analysis_at_ms": _timestamp(row.get("timestamp")),
            "explanation": "Модельная оценка вероятности; это не гарантия результата.",
        }
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _event(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row.get("event_id", "")),
        "event_type": str(row.get("event_type", "UNKNOWN")),
        "summary": str(row.get("summary", ""))[:700],
        "novelty": _number(row.get("novelty")),
        "credibility": _number(row.get("credibility")),
        "confirmation_count": int(row.get("confirmation_count", 0)),
        "expected_direction": str(row.get("expected_direction", "unknown")),
        "contradiction_state": str(row.get("contradiction_state", "unknown")),
        "authenticity_risk": bool(row.get("authenticity_risk", False)),
        "confidence": _number(row.get("confidence")),
        "source_ids": [str(value) for value in row.get("source_ids", [])[:12]],
        "evidence_urls": [str(value)[:1000] for value in row.get("evidence_urls", [])[:12]],
        "published_at_ms": _timestamp(row.get("first_published_at_ms")),
        "received_at_ms": _timestamp(row.get("latest_received_at_ms")),
        "impacted_assets": [
            {
                "canonical_asset_id": str(item.get("canonical_asset_id", "")),
                "chain_id": item.get("chain_id"),
                "contract_address": item.get("contract_address"),
            }
            for item in row.get("impacted_assets", [])[:12]
        ],
    }


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    quote = row.get("paper_exit_quote") or {}
    return {
        "chain_id": row.get("chain_id"),
        "contract_address": row.get("contract_address"),
        "symbol": row.get("symbol"),
        "name": row.get("name"),
        "status": str(row.get("status", "UNKNOWN")),
        "as_of_ms": _timestamp(row.get("as_of_ms")),
        "pool_provider": row.get("pool_provider"),
        "pool_id": row.get("pool_id"),
        "liquidity_usd": _number(row.get("liquidity_usd")),
        "sellable_capacity_usd": _number(row.get("proven_sellable_capacity_usd")),
        "top10_holder_percent": _number(row.get("top10_holder_percent")),
        "risk_flags": [str(value) for value in row.get("risk_flags", [])[:20]],
        "evidence_event_ids": [str(value) for value in row.get("evidence_event_ids", [])[:20]],
        "invalidation": [str(value)[:240] for value in row.get("invalidation", [])[:12]],
        "paper_exit_quote": {
            key: _timestamp(quote.get(key)) if key == "quoted_at_ms" else _number(quote.get(key))
            for key in ("notional_usd", "net_output_usd", "effective_slippage_bps", "quoted_at_ms")
        } if quote else None,
        "auto_purchase": False,
    }


def _recommendation(row: dict[str, Any]) -> dict[str, Any]:
    item = row.get("recommendation", {})
    probability = _number(item.get("calibrated_probability"))
    evidence = lambda values: [
        {"category": str(value.get("category", "")), "summary": str(value.get("summary", ""))[:300],
         "confirmed": bool(value.get("confirmed", False)), "freshness_seconds": value.get("freshness_seconds")}
        for value in values[:8]
    ]
    return {
        "decision_id": str(row.get("decision_id", "")),
        "asset_id": str(item.get("canonical_asset_id", row.get("canonical_asset_id", ""))),
        "instrument_id": str(item.get("instrument_id", row.get("instrument_id", ""))),
        "direction": str(item.get("direction", "NO_TRADE")),
        "status": str(item.get("status", row.get("status", "UNKNOWN"))),
        "probability_percent": probability * 100 if probability is not None and 0 <= probability <= 1 else None,
        "horizon": str(item.get("horizon", "UNKNOWN")),
        "model": str(item.get("model_name", "UNKNOWN")),
        "model_version": str(item.get("model_version", "UNKNOWN")),
        "paper_notional_usd": _number(item.get("paper_notional_usd")),
        "expected_net_move": _number(item.get("expected_net_move")),
        "updated_at_ms": _timestamp(item.get("decision_at_ms", row.get("decision_at_ms"))),
        "freshness_seconds": _number(item.get("data_freshness_seconds")),
        "risk_reasons": [str(value) for value in item.get("risk_reasons", [])[:12]],
        "supporting_evidence": evidence(item.get("supporting_evidence", [])),
        "contradicting_evidence": evidence(item.get("contradicting_evidence", [])),
    }


def build_snapshot(service: Any, now_ms: int | None = None) -> dict[str, Any]:
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    signals = []
    for coin in service.config["market"]["assets"]:
        row = service.repository.latest_signal(coin)
        if row is not None:
            serialized = _signal(row, coin)
            if serialized is not None:
                signals.append(serialized)
    truth = {}
    for key, value in service.market_data.truth.items():
        truth[key] = {
            "status": value.status.value,
            "price": value.canonical_price,
            "calculated_at_ms": int(value.calculated_at_ms),
            "quorum": int(value.quorum),
            "required_quorum": int(value.required_quorum),
            "venue_divergence_bps": value.venue_divergence_bps,
            "cross_exchange_basis_bps": value.cross_exchange_basis_bps,
            "stale_sources": list(value.stale_sources),
            "rejected_outliers": list(value.rejected_outliers),
            "reason": value.reason,
        }
    providers = {
        key: {
            "status": str(value.get("status", "unknown")),
            "lag_ms": value.get("lag_ms"),
            "last_received_at_ms": _timestamp(value.get("last_received_at_ms")),
            "consecutive_failures": int(value.get("consecutive_failures", 0)),
            "reason": redact_sensitive_text(value.get("last_error", ""), 180) or None,
        }
        for key, value in service.market_data.health().items()
    }
    event_sources = {
        key: {
            "status": str(value.get("status", "unknown")),
            "enabled": bool(value.get("enabled", False)),
            "official": bool(value.get("official", False)),
            "lag_ms": value.get("lag_ms"),
            "last_successful_poll_at_ms": _timestamp(value.get("last_successful_poll_at_ms")),
            "reason": redact_sensitive_text(
                value.get("disabled_reason") or value.get("last_error", ""), 180
            ) or None,
        }
        for key, value in service.events.health().items()
    }
    onchain_sources = {
        key: {
            "status": str(value.get("status", "unknown")), "enabled": bool(value.get("enabled", False)),
            "chain_id": value.get("chain_id"), "lag_ms": value.get("lag_ms"),
            "capabilities": {
                str(name): {
                    "status": str(capability.get("status", "unknown")),
                    "reason": redact_sensitive_text(capability.get("reason", ""), 180),
                    "condition": redact_sensitive_text(capability.get("condition", ""), 120) or None,
                }
                for name, capability in value.get("capabilities", {}).items()
                if isinstance(capability, dict)
            },
            "not_available": list(value.get("not_available", [])),
            "reason": redact_sensitive_text(value.get("last_error", ""), 180) or None,
        }
        for key, value in service.onchain.health().items()
    }
    paper_report = service.paper.performance()
    database_health = service.storage.health()
    decisions = service.paper.latest(MAX_ITEMS)
    position_map = service.paper.portfolio.positions()
    open_positions = []
    for decision_id, position in position_map.items():
        decision = next((row for row in decisions if row.get("decision_id") == decision_id), None)
        rec = decision.get("recommendation", {}) if decision else {}
        open_positions.append({
            "decision_id": decision_id,
            "asset_id": str(rec.get("canonical_asset_id", "unknown")),
            "instrument_id": str(rec.get("instrument_id", "unknown")),
            "direction": "LONG" if position.direction > 0 else "SHORT",
            "entry_price": float(position.entry_price),
            "notional_usd": float(position.notional_usd),
            "quantity": float(position.quantity),
            "opened_at_ms": _timestamp(rec.get("decision_at_ms")),
        })
    return {
        "updated_at_ms": now,
        "service": {
            "state": "RUNNING",
            "websocket_connected": bool(service.state.connected),
            "last_market_update_ms": _timestamp(service.state.last_update_ms),
            "database": {
                key: database_health.get(key)
                for key in ("integrity", "schema_version", "bytes")
            },
        },
        "assets": signals,
        "market_truth": truth,
        "providers": providers,
        "events": [_event(item) for item in service.events.latest(MAX_ITEMS)],
        "onchain_candidates": [_candidate(item) for item in service.onchain.latest(MAX_ITEMS)],
        "paper": {
            "mode": "SHADOW",
            "updated_at_ms": now,
            "initial_equity_usd": paper_report["initial_equity_usd"],
            "cash_usd": paper_report["cash_usd"],
            "gross_realized_pnl_usd": paper_report["gross_realized_pnl_usd"],
            "net_realized_pnl_usd": paper_report["net_realized_pnl_usd"],
            "net_unrealized_pnl_usd": paper_report["net_unrealized_pnl_usd"],
            "fees_usd": paper_report["fees_usd"], "funding_usd": paper_report["funding_usd"],
            "slippage_usd": paper_report["slippage_usd"],
            "open_position_count": int(paper_report.get("open_positions", len(open_positions))),
            "reconciles": bool(paper_report["reconciles"]),
            "positions": open_positions,
            "decisions": [_recommendation(item) for item in decisions],
            "disclaimer": "Учёт виртуальный (shadow), не реальные средства и не обещание доходности.",
        },
        "event_sources": event_sources,
        "onchain_sources": onchain_sources,
        "operations": service.operational_metrics(),
    }


def create_web_app(service: Any, settings: dict[str, Any]) -> web.Application:
    auth = TelegramInitDataAuthenticator(
        service.telegram.token,
        settings.get("allowed_user_ids", []),
        max_age_seconds=int(settings.get("init_data_max_age_seconds", 300)),
    )

    @web.middleware
    async def security_headers(request: web.Request, handler):
        response = await handler(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors https://web.telegram.org https://*.telegram.org; "
            "base-uri 'none'; form-action 'none'; object-src 'none'"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store, max-age=0"
            response.headers["Pragma"] = "no-cache"
        return response

    app = web.Application(middlewares=[security_headers], client_max_size=16 * 1024)
    app[SERVICE_KEY] = service
    app[AUTH_KEY] = auth

    async def index(_request: web.Request) -> web.StreamResponse:
        return web.FileResponse(WEB_ROOT / "index.html", headers={"Cache-Control": "no-store"})

    async def root(_request: web.Request) -> web.StreamResponse:
        raise web.HTTPFound("/app/")

    async def asset(request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        if filename not in {"app.css", "app.js"}:
            raise web.HTTPNotFound()
        return web.FileResponse(WEB_ROOT / filename, headers={"Cache-Control": "no-cache"})

    async def snapshot(request: web.Request) -> web.Response:
        if request.headers.get("Origin"):
            origin = urlparse(request.headers["Origin"])
            request_host = urlparse("//" + request.headers.get("Host", ""))
            same_host = bool(
                origin.hostname and request_host.hostname
                and origin.hostname.lower() == request_host.hostname.lower()
                and origin.port == request_host.port
            )
            if not same_host:
                return web.json_response({"error": "origin_not_allowed"}, status=403)
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("tma "):
            return web.json_response({"error": "telegram_init_data_required"}, status=401)
        try:
            auth.validate(authorization[4:])
        except InitDataError as exc:
            status = 503 if not auth.configured else 401
            code = "mini_app_auth_not_configured" if status == 503 else "telegram_init_data_invalid"
            # The reason and initData never leave server memory or enter the response/log.
            _ = exc
            return web.json_response({"error": code}, status=status)
        try:
            result = build_snapshot(service)
        except Exception:
            return web.json_response({"error": "snapshot_unavailable"}, status=503)
        return web.json_response(result, dumps=lambda value: json.dumps(value, ensure_ascii=False, allow_nan=False))

    app.router.add_get("/", root)
    app.router.add_get("/app/", index)
    app.router.add_get("/app/{filename}", asset)
    app.router.add_get("/api/v1/snapshot", snapshot)
    return app
