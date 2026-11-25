from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Any


def health_snapshot(
    storage: Any,
    state: Any,
    model_registry: Any,
    assets: list[str],
    last_alert_ms: int | None = None,
    *,
    provider_health: dict[str, Any] | None = None,
    market_truth: dict[str, Any] | None = None,
    event_health: dict[str, Any] | None = None,
    latest_events: list[dict[str, Any]] | None = None,
    onchain_health: dict[str, Any] | None = None,
    latest_onchain_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    snapshot = {
        "assets": {coin: "configured" for coin in assets}, "ws_connected": bool(state.connected),
        "last_market_update": state.last_update_ms, "market_age_seconds": (int(time.time() * 1000) - state.last_update_ms) / 1000 if state.last_update_ms else None,
        "last_telegram_alert": last_alert_ms, "db": storage.health(deep=False),
        "models": {f"{coin}/{horizon}": "MODEL_READY" for coin, horizon in model_registry.artifacts},
    }
    snapshot["providers"] = provider_health or {}
    snapshot["market_truth"] = market_truth or {}
    snapshot["event_sources"] = event_health or {}
    snapshot["latest_events"] = latest_events or []
    snapshot["onchain_providers"] = onchain_health or {}
    snapshot["latest_onchain_candidates"] = latest_onchain_candidates or []
    return snapshot


def heartbeat_path(sqlite_path: str | Path) -> Path:
    override = os.getenv("ANALYZER_HEARTBEAT_PATH")
    return Path(override) if override else Path(sqlite_path).parent / "heartbeat.json"


def write_heartbeat(path: str | Path, status: str, service: Any, error: str | None = None) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at_ms": int(time.time() * 1000),
        "pid": os.getpid(),
        "watchdog_run_id": os.getenv("ANALYZER_WATCHDOG_RUN_ID"),
        "status": status,
        "ws_connected": bool(service.state.connected),
        "last_market_update_ms": int(service.state.last_update_ms or 0),
        "last_telegram_alert_ms": service.last_alert_ms,
        "error": error,
    }
    metrics = getattr(service, "operational_metrics", None)
    if callable(metrics):
        payload["operations"] = metrics()
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    temporary.replace(destination)
