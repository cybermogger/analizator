from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from src.data.repository import MarketRepository
from src.data.storage import SQLiteStorage
from src.hyperliquid.rest import HyperliquidREST
from src.models.quant_audit import run_quant_audit
from src.service.analyzer import INTERVAL_MS
from src.service.config import load_config


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Official-data backfill followed by safe champion/challenger training")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--coin", choices=["SOL", "HYPE"], action="append")
    parser.add_argument("--horizon", choices=["15m", "1h", "4h"], action="append")
    parser.add_argument("--history", type=int, default=5000, help="Official candleSnapshot history, max 5000")
    parser.add_argument("--offline", action="store_true", help="Use only already cached official Hyperliquid candles")
    parser.add_argument("--no-promote", action="store_true", help="Save challengers but never replace a champion")
    return parser.parse_args()


async def backfill(config: dict, config_path: Path, tasks: list[tuple[str, str]], history: int) -> None:
    storage = SQLiteStorage(config["storage"]["sqlite_path"])
    repository = MarketRepository(storage)
    requests = {
        (coin, config["models"]["horizons"][horizon]["source_interval"])
        for coin, horizon in tasks
    }
    requests.update(
        (config["market"]["context_asset"], config["models"]["horizons"][horizon]["source_interval"])
        for _, horizon in tasks
    )
    now = int(time.time() * 1000)
    try:
        async with HyperliquidREST(
            config["market"]["rest_url"], config["service"]["request_timeout_seconds"],
            config["service"]["max_retries"],
        ) as rest:
            for coin, interval in sorted(requests):
                candles = await rest.candles(coin, interval, now - history * INTERVAL_MS[interval], now)
                repository.upsert_candles(candles)
                print(f"BACKFILL {coin}/{interval}: {len(candles)}", flush=True)
    finally:
        storage.close()


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = arguments()
    config_path = Path(args.config).resolve()
    config, _ = load_config(config_path)
    coins = args.coin or config["market"]["assets"]
    horizons = args.horizon or list(config["models"]["horizons"])
    tasks = [(coin, horizon) for coin in coins for horizon in horizons]
    history = max(500, min(int(args.history), 5000))
    if not args.offline:
        await backfill(config, config_path, tasks, history)
    report, path = await asyncio.to_thread(
        run_quant_audit, config_path, tasks, not args.no_promote,
        lambda message: print(message, flush=True),
    )
    summary = {
        key: {
            "status": task["model_status"], "barrier": task["selected_barrier"],
            "variant": task["selected_variant"], "test": task["untouched_test_metrics"],
        }
        for key, task in report["tasks"].items()
    }
    print(json.dumps({"report": str(path), "models": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
