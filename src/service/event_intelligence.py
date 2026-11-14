from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Any

import structlog
from src.util.redaction import safe_exception

from src.events.entities import EntityResolver, EventEntity
from src.events.pipeline import EventIntelligencePipeline
from src.events.repository import EventRepository
from src.events.sources import EventSource, build_sources
from src.events.types import NormalizedEvent, RawEvent


class EventIntelligenceService:
    """Failure-isolated event ingestion; never participates in signal probabilities."""

    def __init__(self, config: dict[str, Any], storage: Any):
        self.config = config.get("events", {})
        self.enabled = bool(self.config.get("enabled", False))
        self.log = structlog.get_logger("event_intelligence")
        self.repository = EventRepository(storage)
        entities = tuple(self._entity(item) for item in self.config.get("entities", []))
        self.resolver = EntityResolver(entities)
        source_settings = dict(self.config.get("sources", {}))
        self.repository.sync_sources(source_settings)
        self.repository.sync_entities(entities)
        self.sources: dict[str, EventSource] = build_sources(source_settings, self.resolver)
        self.pipeline = EventIntelligencePipeline(
            self.resolver,
            self.repository,
            dedup_similarity=float(self.config.get("dedup_similarity", 0.72)),
            dedup_window_hours=float(self.config.get("dedup_window_hours", 72)),
        )
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []
        self._ingest_lock = asyncio.Lock()
        self._ingest_waiting = 0
        self._ingest_in_flight = 0
        self._maximum_ingest_backlog = 0
        self._ingested = 0
        self._duplicates = 0
        self._ingest_failures = 0

    @staticmethod
    def _entity(value: dict[str, Any]) -> EventEntity:
        return EventEntity(
            project_id=str(value["project_id"]),
            canonical_asset_id=str(value["canonical_asset_id"]),
            name=str(value["name"]),
            tickers=tuple(str(item) for item in value.get("tickers", [])),
            contracts=tuple((str(item["chain_id"]), str(item["address"])) for item in value.get("contracts", [])),
            official_account_ids=tuple(str(item) for item in value.get("official_account_ids", [])),
            official_domains=tuple(str(item) for item in value.get("official_domains", [])),
        )

    async def ingest(self, raw: RawEvent) -> NormalizedEvent:
        self._ingest_waiting += 1
        waiting = True
        try:
            self._maximum_ingest_backlog = max(
                self._maximum_ingest_backlog, self._ingest_waiting - 1
            )
            async with self._ingest_lock:
                self._ingest_waiting -= 1
                waiting = False
                self._ingest_in_flight += 1
                duplicate = raw.raw_event_id in self.pipeline._raw_to_cluster
                try:
                    event = await self.pipeline.ingest_async(raw)
                    self._duplicates += int(duplicate)
                    self._ingested += int(not duplicate)
                except Exception:
                    self._ingest_failures += 1
                    raise
                finally:
                    self._ingest_in_flight -= 1
        finally:
            if waiting:
                self._ingest_waiting -= 1
        self.log.info(
            "event_normalized",
            event_id=event.event_id,
            event_type=event.event_type.value,
            credibility=event.credibility,
            confirmations=event.confirmation_count,
            assets=[item.canonical_asset_id for item in event.impacted_assets],
        )
        on_event = getattr(self, "on_event", None)
        if on_event is not None and not duplicate:
            try:
                await on_event(event, int(time.time() * 1000))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.warning("event_notification_failed", error=safe_exception(exc))
        return event

    async def poll_once(self) -> list[NormalizedEvent]:
        """Bounded one-shot helper used by smoke tests; streaming X is excluded."""
        events: list[NormalizedEvent] = []
        for source in self.sources.values():
            if not source.enabled or type(source).__name__ == "XFilteredStreamSource":
                continue
            try:
                for raw in await source.poll():
                    events.append(await self.ingest(raw))
                    source.runtime.last_received_at_ms = max(
                        source.runtime.last_received_at_ms or 0, raw.received_at_ms
                    )
                source.runtime.status = "healthy"
                source.runtime.last_successful_poll_at_ms = int(time.time() * 1000)
                source.runtime.consecutive_failures = 0
                source.runtime.last_error = None
            except Exception as exc:
                source.runtime.status = "degraded"
                source.runtime.consecutive_failures += 1
                source.runtime.last_error = safe_exception(exc)
                self.log.warning("event_source_one_shot_failed", source=source.source_id, error=safe_exception(exc))
            self._persist_health(source)
        return events

    def _persist_health(self, source: EventSource) -> None:
        now = int(time.time() * 1000)
        self.repository.save_source_health(
            source.source_id,
            source.runtime.status,
            now,
            source.runtime.last_received_at_ms,
            source.runtime.consecutive_failures,
            source.runtime.last_error,
            source.runtime.last_successful_poll_at_ms,
        )

    async def run_forever(self) -> None:
        if not self.enabled:
            return
        self._tasks = [
            asyncio.create_task(source.run(self.ingest, self._stop), name=f"event-source:{source_id}")
            for source_id, source in self.sources.items() if source.enabled
        ]
        try:
            while not self._stop.is_set():
                for source in self.sources.values():
                    self._persist_health(source)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=10.0)
                except TimeoutError:
                    pass
        finally:
            for task in self._tasks:
                task.cancel()
            for task in self._tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            self._tasks.clear()

    async def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict[str, Any]:
        now = int(time.time() * 1000)
        stale_after_ms = int(float(self.config.get("stale_after_seconds", 900)) * 1000)
        return {
            source_id: {
                "source_tier": source.source_tier.value,
                "enabled": source.enabled,
                "official": bool(source.settings.get("official", False)),
                "status": (
                    "stale" if source.runtime.status == "healthy"
                    and source.runtime.last_successful_poll_at_ms is not None
                    and now - source.runtime.last_successful_poll_at_ms > stale_after_ms
                    else source.runtime.status
                ),
                "last_received_at_ms": source.runtime.last_received_at_ms,
                "last_successful_poll_at_ms": source.runtime.last_successful_poll_at_ms,
                "lag_ms": (
                    max(0, now - source.runtime.last_received_at_ms)
                    if source.runtime.last_received_at_ms is not None else None
                ),
                "consecutive_failures": source.runtime.consecutive_failures,
                "last_error": source.runtime.last_error,
                "disabled_reason": (
                    str(source.settings.get("disabled_reason")) if not source.enabled else None
                ),
            }
            for source_id, source in self.sources.items()
        }

    def runtime_metrics(self) -> dict[str, Any]:
        sources = self.health()
        enabled = [value for value in sources.values() if value["enabled"]]
        healthy = [value for value in enabled if value["status"] == "healthy"]
        lags = [int(item["lag_ms"]) for item in enabled if item["lag_ms"] is not None]
        return {
            "configured_sources": len(sources),
            "enabled_sources": len(enabled),
            "healthy_sources": len(healthy),
            "source_coverage": len(healthy) / len(enabled) if enabled else None,
            "event_lag_ms": max(lags) if lags else None,
            "queue_depth": self._ingest_waiting,
            "in_flight": self._ingest_in_flight,
            "maximum_backlog": self._maximum_ingest_backlog,
            "backlogged_events": self._ingest_waiting,
            "dropped_events": 0,
            "ingested_events": self._ingested,
            "duplicate_events": self._duplicates,
            "ingest_failures": self._ingest_failures,
            "backpressure": "awaited_serial_ingest",
        }

    def latest(self, limit: int = 5) -> list[dict[str, Any]]:
        return self.repository.latest_events(limit)
