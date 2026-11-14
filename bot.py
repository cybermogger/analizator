from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import signal as system_signal
import sys
from pathlib import Path

from src.alerts.formatter import format_signal
from src.service.analyzer import AnalyzerService
from src.service.config import load_config
from src.service.logging import configure_logging
from src.service.scheduler import run_daemon


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only SOL/HYPE Hyperliquid analyzer")
    parser.add_argument("--config", default="config.yaml")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="Run one full SOL/HYPE analysis cycle")
    mode.add_argument("--smoke-test", action="store_true", help="Validate live official Hyperliquid REST schemas")
    mode.add_argument("--cycles", type=int, help="Run N bounded live analysis cycles without Telegram (audit/soak)")
    parser.add_argument("--send-telegram", action="store_true", help="Allow Telegram delivery during --once")
    parser.add_argument("--print-messages", action="store_true", help="Print Russian Telegram formatter output")
    parser.add_argument("--full-json", action="store_true", help="Print every feature and component weight during --once")
    parser.add_argument("--cycle-delay", type=float, default=2.0, help="Delay between --cycles runs, in seconds")
    return parser.parse_args()


async def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = arguments()
    config, _ = load_config(Path(args.config))
    configure_logging(
        config["storage"]["logs_path"],
        config["service"]["log_level"],
        int(config["service"].get("log_max_bytes", 10_000_000)),
        int(config["service"].get("log_backup_count", 5)),
    )
    service = AnalyzerService(config)
    if args.smoke_test:
        try:
            try:
                result = await asyncio.wait_for(service.live_smoke_test(), timeout=60)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 0
            except TimeoutError:
                print(json.dumps({"status": "LIVE_SMOKE_TIMEOUT", "limit_seconds": 60, "reason": "environment/network limitation"}, ensure_ascii=False, indent=2))
                return 3
        finally:
            await service.close()
    if args.once:
        try:
            signals = await asyncio.wait_for(
                service.analysis_cycle(send_telegram=args.send_telegram),
                timeout=float(config["service"]["cycle_timeout_seconds"]),
            )
            output = signals if args.full_json else [{
                "coin": signal["coin"], "mark": signal["mark"], "primary_horizon": signal["primary_horizon"],
                "direction": signal["direction"], "probability": signal["probability"],
                "horizons": signal["horizons"], "confidence": signal.get("confidence"),
                "signal_strength": signal.get("signal_strength"), "regime": signal["regime"],
                "expected_move": signal.get("expected_move"), "expected_range": signal.get("expected_range"),
                "target_range": signal.get("target_range"), "invalidation": signal.get("invalidation"),
                "support": signal.get("support"), "resistance": signal.get("resistance"),
                "volatility": signal.get("volatility"), "data_freshness": signal.get("data_freshness"),
                "reasons": signal.get("reasons"), "alert_decision": signal.get("alert_decision"),
                "model_status": signal["model_status"],
            } for signal in signals]
            print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
            if args.print_messages:
                for signal in signals:
                    print("\n" + format_signal(signal) + "\n")
            return 0 if len(signals) == len(config["market"]["assets"]) else 2
        finally:
            await service.close()
    if args.cycles is not None:
        if args.cycles < 1:
            raise SystemExit("--cycles must be positive")
        ws_task = asyncio.create_task(service.websocket.run_forever(), name="audit-websocket")
        results: list[dict[str, object]] = []
        try:
            deadline = asyncio.get_running_loop().time() + 10
            while not service.state.connected and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.1)
            for cycle in range(1, args.cycles + 1):
                signals = await asyncio.wait_for(
                    service.analysis_cycle(send_telegram=False),
                    timeout=float(config["service"]["cycle_timeout_seconds"]),
                )
                summary = {
                    "cycle": cycle,
                    "assets": [{
                        "coin": signal["coin"], "mark": signal["mark"],
                        "primary_horizon": signal["primary_horizon"], "direction": signal["direction"],
                        "probability": round(signal["probability"], 4),
                        "confidence": round(signal.get("confidence", 0), 2),
                        "signal_strength": round(signal.get("signal_strength", 0), 2),
                        "event": signal.get("event"), "model_status": signal["model_status"],
                    } for signal in signals],
                }
                results.append(summary)
                print(json.dumps(summary, ensure_ascii=False), flush=True)
                if len(signals) != len(config["market"]["assets"]):
                    return 2
                if cycle < args.cycles:
                    await asyncio.sleep(max(0.0, args.cycle_delay))
            print(json.dumps({"status": "SOAK_OK", "cycles": len(results)}, ensure_ascii=False))
            return 0
        finally:
            await service.websocket.stop()
            ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ws_task
            await service.close()
    daemon_task = asyncio.create_task(run_daemon(service), name="analyzer-daemon")
    loop = asyncio.get_running_loop()
    installed: list[system_signal.Signals] = []
    for shutdown_signal in (system_signal.SIGINT, system_signal.SIGTERM):
        try:
            loop.add_signal_handler(shutdown_signal, daemon_task.cancel)
            installed.append(shutdown_signal)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await daemon_task
    finally:
        for shutdown_signal in installed:
            loop.remove_signal_handler(shutdown_signal)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
