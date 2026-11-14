from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any

from src.data.repository import MarketRepository
from src.data.storage import SCHEMA_VERSION, SQLiteStorage
from src.events.repository import EventRepository
from src.onchain.repository import OnChainRepository
from src.paper.repository import PaperRepository
from src.service.config import load_config
from src.service.health import heartbeat_path


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show analyzer runtime and latest SOL/HYPE probabilities")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--max-age-seconds", type=float, default=180)
    return parser.parse_args()


def utc_timestamp(value: int | None) -> str:
    if not value:
        return "never"
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def load_heartbeat(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def heartbeat_age_seconds(value: dict[str, Any] | None) -> float | None:
    if not value:
        return None


def paper_ledger_reconciles(storage: SQLiteStorage, tolerance: float = 1e-8) -> bool:
    rows = storage.query("""SELECT
      COALESCE(SUM(gross_pnl_usd),0) AS gross,
      COALESCE(SUM(net_pnl_usd),0) AS net,
      COALESCE(SUM(fees_usd),0) AS fees,
      COALESCE(SUM(funding_usd),0) AS funding,
      COALESCE(SUM(slippage_usd),0) AS slippage FROM paper_ledger""")
    row = rows[0]
    expected = float(row["gross"]) - float(row["fees"]) - float(row["funding"]) - float(row["slippage"])
    return abs(expected - float(row["net"])) <= tolerance
    try:
        return max(0.0, (int(time.time() * 1000) - int(value["updated_at_ms"])) / 1000)
    except (KeyError, TypeError, ValueError):
        return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = arguments()
    config, _ = load_config(Path(args.config))
    database_path = Path(config["storage"]["sqlite_path"])
    runtime_path = heartbeat_path(database_path)
    heartbeat = load_heartbeat(runtime_path)
    age = heartbeat_age_seconds(heartbeat)
    operations = (heartbeat or {}).get("operations", {})

    if not database_path.exists():
        print(f"DB status: MISSING ({database_path})")
        return 1

    storage: SQLiteStorage | None = None
    try:
        storage = SQLiteStorage(database_path)
        repository = MarketRepository(storage)
        event_repository = EventRepository(storage)
        onchain_repository = OnChainRepository(storage)
        database_health = storage.health()
        if args.healthcheck:
            healthy = (
                database_health["integrity"] == "ok"
                and database_health["schema_version"] == SCHEMA_VERSION
                and paper_ledger_reconciles(storage)
                and age is not None
                and age <= args.max_age_seconds
                and heartbeat is not None
                and heartbeat.get("status") in {"starting", "running"}
            )
            print(
                f"{'healthy' if healthy else 'unhealthy'}: "
                f"heartbeat_age={age if age is not None else 'missing'} "
                f"db={database_health['integrity']}"
            )
            return 0 if healthy else 1

        for coin in config["market"]["assets"]:
            row = repository.latest_signal(coin)
            print(f"{coin}:")
            if row is None:
                print("  current price: unavailable")
                print("  LONG: unavailable\n  SHORT: unavailable\n  WAIT: unavailable")
                print("  model status: no signal")
                print("  last analysis: never")
                continue
            payload = json.loads(row["payload_json"])
            primary = payload["horizons"][payload["primary_horizon"]]
            print(f"  current price: {float(payload['mark']):.8g}")
            print(f"  LONG: {float(primary['LONG']):.2f}%")
            print(f"  SHORT: {float(primary['SHORT']):.2f}%")
            print(f"  WAIT: {float(primary['WAIT']):.2f}%")
            print(f"  confidence: {float(payload.get('confidence', 0)):.1f}/100")
            print(f"  signal strength: {float(payload.get('signal_strength', 0)):.1f}/100")
            print(f"  regime: {payload.get('regime', 'UNKNOWN')}")
            print(f"  data freshness: {payload.get('data_freshness', {}).get('status', 'UNKNOWN')}")
            print(f"  model status: {row['model_status']}")
            print(f"  model version: {payload.get('production_model_version', 'unavailable')}")
            calibration = operations.get("calibration", {}).get(coin, {})
            print(
                "  calibration drift: "
                f"samples={calibration.get('samples', 'unavailable')}; "
                f"Brier={calibration.get('brier', 'unavailable')}; "
                f"ECE={calibration.get('ece', 'unavailable')}"
            )
            print(f"  last analysis: {utc_timestamp(int(row['timestamp']))}")

        runtime_status = heartbeat.get("status") if heartbeat else None
        if runtime_status == "stopping":
            ws_status = "STOPPED"
        elif age is None or age > args.max_age_seconds:
            ws_status = "STALE"
        else:
            ws_status = "CONNECTED" if heartbeat and heartbeat.get("ws_connected") else "DISCONNECTED"
        market_update = utc_timestamp(int(heartbeat.get("last_market_update_ms", 0))) if heartbeat else "never"
        heartbeat_age = f"{age:.1f}s" if age is not None else "missing"
        print(f"WS status: {ws_status}; last update: {market_update}; heartbeat age: {heartbeat_age}")
        print(
            f"DB status: {database_health['integrity']}; "
            f"schema: {database_health['schema_version']}; bytes: {database_health['bytes']}"
        )
        provider_rows = repository.provider_health_rows()
        enabled_provider_ids = {
            key for key, value in config["providers"].items() if value.get("enabled", False)
        }
        now_ms = int(time.time() * 1000)
        healthy_provider_ids = set()
        for item in provider_rows:
            provider_id = str(item["provider_id"])
            stale_after_ms = int(float(config["providers"].get(provider_id, {}).get("stale_after_seconds", 30)) * 1000)
            last_received = item["last_received_at_ms"]
            if (
                str(item["status"]).lower() == "healthy" and last_received is not None
                and now_ms - int(last_received) <= stale_after_ms
            ):
                healthy_provider_ids.add(provider_id)
        print(
            "Market source coverage: "
            f"{len(healthy_provider_ids & enabled_provider_ids)}/{len(enabled_provider_ids)} enabled providers healthy"
        )
        if provider_rows:
            print("Provider health:")
            for provider in provider_rows:
                provider_config = config["providers"].get(str(provider["provider_id"]), {})
                stale_after_ms = int(float(provider_config.get("stale_after_seconds", 30)) * 1000)
                current_lag = (
                    max(0, int(time.time() * 1000) - int(provider["last_received_at_ms"]))
                    if provider["last_received_at_ms"] is not None else None
                )
                effective_status = str(provider["status"]).upper()
                if current_lag is not None and current_lag > stale_after_ms and effective_status == "HEALTHY":
                    effective_status = "STALE"
                lag = f"{current_lag}ms" if current_lag is not None else "unknown"
                print(
                    f"  {provider['provider_id']}: {effective_status}; "
                    f"lag={lag}; failures={int(provider['consecutive_failures'])}"
                )
        else:
            print("Provider health: no multi-venue samples yet")
        event_health = event_repository.event_source_health()
        event_sources = storage.query("SELECT source_id,enabled,config_json FROM event_sources ORDER BY source_id")
        event_source_config = {
            str(item["source_id"]): json.loads(item["config_json"])
            for item in event_sources
        }
        enabled_event_sources = {str(item["source_id"]) for item in event_sources if item["enabled"]}
        healthy_event_sources = {
            str(item["source_id"]) for item in event_health
            if str(item["status"]).lower() == "healthy"
        }
        print(
            "Event source coverage: "
            f"{len(healthy_event_sources & enabled_event_sources)}/{len(enabled_event_sources)} enabled sources healthy"
        )
        event_lags = [int(item["lag_ms"]) for item in event_health if item["lag_ms"] is not None]
        print(f"Event lag: {max(event_lags) if event_lags else 'unavailable'} ms")
        event_health_by_id = {str(item["source_id"]): item for item in event_health}
        if event_sources:
            print("Event Intelligence sources:")
            for source_id, settings in event_source_config.items():
                source = event_health_by_id.get(source_id, {})
                source_status = source.get("status", "unknown" if settings.get("enabled") else "disabled")
                lag_value = source.get("lag_ms")
                lag = f"{int(lag_value)}ms" if lag_value is not None else "unknown"
                detail = settings.get("disabled_reason") if not settings.get("enabled") else source.get("last_error")
                suffix = f"; reason={str(detail)[:180]}" if detail else ""
                print(
                    f"  {source_id}: {str(source_status).upper()}; "
                    f"lag={lag}; failures={int(source.get('consecutive_failures', 0))}{suffix}"
                )
        else:
            print("Event Intelligence sources: no samples yet")
        latest_events = event_repository.latest_events(1)
        if latest_events:
            event = latest_events[0]
            print(
                f"latest event: {event['event_type']}; confidence={float(event['confidence']) * 100:.0f}%; "
                f"confirmations={int(event['confirmation_count'])}; {str(event['summary'])[:120]}"
            )
        else:
            print("latest event: none")
        onchain_health = onchain_repository.provider_health()
        if onchain_health:
            print("On-chain providers:")
            for provider in onchain_health:
                lag = f"{int(provider['lag_ms'])}ms" if provider["lag_ms"] is not None else "unknown"
                print(
                    f"  {provider['provider_id']}: {str(provider['status']).upper()}; "
                    f"chain={provider['chain_id']}; lag={lag}; "
                    f"failures={int(provider['consecutive_failures'])}"
                )
        else:
            print("On-chain providers: no samples yet")
        candidates = onchain_repository.latest_assessments(1)
        if candidates:
            candidate = candidates[0]
            print(
                f"latest on-chain candidate: {candidate.get('symbol') or 'UNKNOWN'} "
                f"{candidate['chain_id']}:{candidate['contract_address']} "
                f"status={candidate['status']}"
            )
        else:
            print("latest on-chain candidate: none")
        telegram = repository.latest_telegram_event()
        if telegram:
            summary = " ".join(str(telegram["message"]).split())[:160]
            print(
                f"last Telegram alert: {utc_timestamp(int(telegram['timestamp']))}; "
                f"{telegram['kind']}/{telegram['scope']}; {summary}"
            )
        else:
            print("last Telegram alert: never")

        current_universe = repository.current_universe()
        deep_size = sum(bool(item["deep_watchlist"]) for item in current_universe)
        print(f"Universe: scan={len(current_universe)}; deep_watch={deep_size}")
        request_metrics = operations.get("market_requests", {})
        market_queue = sum(int(value.get("queue_depth", 0)) for value in request_metrics.values())
        market_in_flight = sum(int(value.get("in_flight", 0)) for value in request_metrics.values())
        event_metrics = operations.get("event_ingestion", {})
        print(
            "Backpressure: "
            f"market queue={market_queue}, in_flight={market_in_flight}; "
            f"event queue={event_metrics.get('queue_depth', 'unavailable')}, "
            f"backlogged={event_metrics.get('backlogged_events', 'unavailable')}, "
            f"dropped={event_metrics.get('dropped_events', 'unavailable')}"
        )
        paper_rows = storage.query(
            """SELECT COALESCE(SUM(cash_delta_usd),0) AS cash,
            COALESCE(SUM(gross_pnl_usd),0) AS gross,
            COALESCE(SUM(net_pnl_usd),0) AS net,
            COALESCE(SUM(fees_usd),0) AS fees,
            COALESCE(SUM(funding_usd),0) AS funding,
            COALESCE(SUM(slippage_usd),0) AS slippage FROM paper_ledger"""
        )
        open_rows = storage.query(
            """SELECT COUNT(*) AS count FROM (
            SELECT decision_id FROM paper_ledger WHERE decision_id IS NOT NULL
            GROUP BY decision_id HAVING ABS(SUM(quantity_delta))>1e-12)"""
        )
        paper = paper_rows[0]
        reconciles = paper_ledger_reconciles(storage)
        print(
            "Paper shadow: "
            f"net_realized=${paper['net']:.2f}; open={open_rows[0]['count']}; "
            f"fees=${paper['fees']:.2f}; funding=${paper['funding']:.2f}; "
            f"slippage=${paper['slippage']:.2f}; "
            f"{'RECONCILED' if reconciles else 'RECONCILIATION_FAILURE'}"
        )
        paper_repository = PaperRepository(storage)
        breakers = sorted({
            str(reason) for item in paper_repository.decisions(30)
            for reason in item["recommendation"].get("risk_reasons", [])
        })
        print(f"Circuit breakers: {', '.join(breakers) if breakers else 'none active in recent decisions'}")
        degraded_reasons = operations.get("degraded_reasons", [])
        print(f"Degraded reasons: {', '.join(str(item) for item in degraded_reasons) if degraded_reasons else 'none reported'}")
        delivery = storage.telegram_delivery_status()
        print(
            "Telegram delivery: "
            f"sent={delivery['sent']}; failed={delivery['failed']}; "
            f"unknown_after_crash={delivery['unknown']}"
        )
        return 0
    except (OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError) as error:
        print(f"status error: {type(error).__name__}: {error}")
        return 1
    finally:
        if storage is not None:
            storage.close()


if __name__ == "__main__":
    raise SystemExit(main())
