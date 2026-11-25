from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
import sys
import time

import structlog
from src.util.redaction import safe_exception

from .analyzer import AnalyzerService
from .health import heartbeat_path, write_heartbeat


def _start_market_analysis_worker(service: AnalyzerService) -> asyncio.Task[None] | None:
    if not service.config.get("market_analysis", {}).get("enabled", True):
        return None
    return asyncio.create_task(
        service.market_analysis.run_forever(), name="multi-asset-descriptive-analysis"
    )


async def _run_auto_retrain(service: AnalyzerService) -> None:
    log = structlog.get_logger("auto_retrain")
    settings = service.config["models"].get("auto_retrain", {})
    timeout = float(settings.get("timeout_seconds", 900))
    root = Path(__file__).resolve().parents[2]
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(root / "quant_audit.py"), "--config", str(root / "config.yaml"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(root),
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        log.error("auto_retrain_timeout", limit_seconds=timeout)
        return
    except asyncio.CancelledError:
        process.kill()
        await process.wait()
        raise
    text = output.decode("utf-8", errors="replace")
    if process.returncode != 0:
        log.error("auto_retrain_failed", returncode=process.returncode, output=text[-4000:])
        return
    for coin in service.config["market"]["assets"]:
        for horizon in service.config["models"]["horizons"]:
            service.models.load(coin, horizon)
    log.info("auto_retrain_completed", loaded_models=len(service.models.artifacts), output=text[-2000:])


async def run_daemon(service: AnalyzerService) -> None:
    log = structlog.get_logger("scheduler")
    runtime_heartbeat = heartbeat_path(service.config["storage"]["sqlite_path"])
    write_heartbeat(runtime_heartbeat, "starting", service)
    dashboard_runner = None
    dashboard_settings = service.config.get("dashboard", {})
    if dashboard_settings.get("enabled", False):
        try:
            from src.dashboard.server import start_dashboard
            dashboard_runner = await start_dashboard(service, dashboard_settings)
            log.info("local_dashboard_started", host=dashboard_settings.get("host"), port=dashboard_settings.get("port"))
        except Exception as exc:
            log.error("local_dashboard_unavailable", error=safe_exception(exc, 300))
    ws_task = asyncio.create_task(service.websocket.run_forever(), name="hyperliquid-websocket")
    multi_venue_task = asyncio.create_task(service.market_data.run_forever(), name="multi-venue-market-data")
    event_task = asyncio.create_task(service.events.run_forever(), name="event-intelligence")
    onchain_task = asyncio.create_task(service.onchain.run_forever(), name="onchain-intelligence")
    new_token_task = asyncio.create_task(service.new_token_alerts.run_forever(service.onchain),
        name="new-token-reports") if service.telegram.configured and service.config.get("alerts", {}).get("enabled", False) else None
    analysis_task = _start_market_analysis_worker(service)
    command_task = asyncio.create_task(service.telegram_commands.run_forever(), name="telegram-commands")
    interval = float(service.config["service"]["analysis_interval_seconds"])
    health_every = float(service.config["service"]["health_interval_hours"]) * 3600
    last_health = 0.0
    retrain_settings = service.config["models"].get("auto_retrain", {})
    retrain_every = float(retrain_settings.get("interval_hours", 24)) * 3600
    last_retrain = time.monotonic()
    retrain_task: asyncio.Task[None] | None = None
    try:
        while True:
            started = time.monotonic()
            cycle_error: str | None = None
            try:
                await asyncio.wait_for(service.analysis_cycle(send_telegram=True), timeout=float(service.config["service"]["cycle_timeout_seconds"]))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cycle_error = safe_exception(exc, 1000)
                log.error("analysis_cycle_failed", error=cycle_error)
                expected_feed_error = isinstance(exc, (TimeoutError, OSError)) or type(exc).__name__ in {
                    "HyperliquidAPIError", "ClientConnectorError", "ServerTimeoutError"
                }
                if not expected_feed_error:
                    await service.notify_critical("analysis_cycle", exc)
                await service.notify_feed_status(False, cycle_error)
            try:
                write_heartbeat(runtime_heartbeat, "running", service, cycle_error)
            except OSError as exc:
                log.error("heartbeat_write_failed", path=str(runtime_heartbeat), error=safe_exception(exc))
            now = time.monotonic()
            forecasts = getattr(service, "market_forecasts", None)
            if forecasts is not None:
                try:
                    await asyncio.wait_for(forecasts.digest(int(time.time()*1000)), timeout=15)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.error("forecast_digest_failed", error=safe_exception(exc))
            if now - last_health >= health_every:
                log.info("health", **service.health())
                last_health = now
            if retrain_settings.get("enabled", False) and retrain_task is None and now - last_retrain >= retrain_every:
                retrain_task = asyncio.create_task(_run_auto_retrain(service), name="quant-auto-retrain")
                last_retrain = now
                log.info("auto_retrain_started", timeout_seconds=retrain_settings.get("timeout_seconds", 900))
            if retrain_task is not None and retrain_task.done():
                with contextlib.suppress(Exception):
                    retrain_task.result()
                retrain_task = None
            await asyncio.sleep(max(1.0, interval - (time.monotonic() - started)))
    finally:
        if dashboard_runner is not None:
            await dashboard_runner.cleanup()
        with contextlib.suppress(OSError):
            write_heartbeat(runtime_heartbeat, "stopping", service)
        if retrain_task is not None:
            retrain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await retrain_task
        await service.websocket.stop()
        await service.market_data.stop()
        await service.events.stop()
        await service.onchain.stop()
        service.new_token_alerts.stop()
        await service.market_analysis.stop()
        await service.telegram_commands.stop()
        ws_task.cancel()
        multi_venue_task.cancel()
        event_task.cancel()
        onchain_task.cancel()
        if new_token_task is not None:
            new_token_task.cancel()
        command_task.cancel()
        if analysis_task is not None:
            analysis_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ws_task
        with contextlib.suppress(asyncio.CancelledError):
            await multi_venue_task
        with contextlib.suppress(asyncio.CancelledError):
            await event_task
        with contextlib.suppress(asyncio.CancelledError):
            await onchain_task
        if new_token_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await new_token_task
        if analysis_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await analysis_task
        with contextlib.suppress(asyncio.CancelledError):
            await command_task
        await service.close()
