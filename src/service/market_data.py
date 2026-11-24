from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
import time
from typing import Any, Sequence

import structlog

from src.data.repository import MarketRepository
from src.providers.base import ProviderCapabilityError
from src.providers.base import MarketDataProvider
from src.providers.instruments import InstrumentRegistry
from src.providers.market_truth import MarketTruthEngine
from src.providers.registry import ProviderRegistry
from src.providers.types import (
    CanonicalMarketTruth, Instrument, MarketCatalogEntry, MarketCatalogScope,
    NormalizedBBO, NormalizedEvent,
)
from src.providers.universe import DynamicUniverse, UniverseBucket, UniverseCandidate, UniverseMembership


class _ProviderRequestGate:
    """Bound concurrent work and space request starts per provider."""

    def __init__(self, concurrency: int, minimum_interval_ms: int):
        self._semaphore = asyncio.Semaphore(max(1, concurrency-1))
        self._quote_semaphore = asyncio.Semaphore(1) if concurrency>1 else self._semaphore
        self._rate_lock = asyncio.Lock()
        self._minimum_interval = max(0, minimum_interval_ms) / 1000
        self._next_request_at = 0.0
        self.waiting = 0
        self.in_flight = 0
        self.maximum_in_flight = 0
        self.requests = 0
        self.failures = 0
        self.rate_waits = 0
        self.rate_wait_ms = 0.0

    async def run(self, operation: Callable[[], Awaitable[Any]], *, priority: bool = False) -> Any:
        self.waiting += 1
        counted_waiting = True
        try:
            async with (self._quote_semaphore if priority else self._semaphore):
                async with self._rate_lock:
                    now = time.monotonic()
                    delay = max(0.0, self._next_request_at - now)
                    if delay:
                        self.rate_waits += 1
                        self.rate_wait_ms += delay * 1000
                        await asyncio.sleep(delay)
                    self._next_request_at = time.monotonic() + self._minimum_interval
                self.waiting -= 1
                counted_waiting = False
                self.in_flight += 1
                self.maximum_in_flight = max(self.maximum_in_flight, self.in_flight)
                self.requests += 1
                try:
                    return await operation()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.failures += 1
                    raise
                finally:
                    self.in_flight -= 1
        finally:
            if counted_waiting:
                self.waiting -= 1

    def snapshot(self) -> dict[str, int | float]:
        return {
            "queue_depth": self.waiting,
            "in_flight": self.in_flight,
            "maximum_in_flight": self.maximum_in_flight,
            "requests": self.requests,
            "failures": self.failures,
            "rate_waits": self.rate_waits,
            "rate_wait_ms": round(self.rate_wait_ms, 3),
            "dropped": 0,
        }


class MultiVenueMarketService:
    """Failure-isolated public feed coordinator; it never feeds the trading model in phase 1."""

    def __init__(self, config: dict[str, Any], repository: MarketRepository):
        self.config = config
        self.repository = repository
        self.log = structlog.get_logger("multi_venue")
        self.instruments = InstrumentRegistry.from_config(config["instruments"])
        self.providers = ProviderRegistry(config["providers"], self.instruments)
        self.truth_engine = MarketTruthEngine(self.instruments, config["market_truth"])
        self.universe = DynamicUniverse(self.instruments, config["universe"])
        self.membership: tuple[UniverseMembership, ...] = ()
        self.universe_snapshot: dict[str, Any] = {}
        self.truth: dict[str, CanonicalMarketTruth] = {}
        # Descriptive multi-asset analysis reads the latest accepted venue book
        # directly. This is not merged into canonical truth or the legacy model.
        self.latest_l2: dict[str, Any] = {}
        self._catalog_entries: dict[str, tuple[MarketCatalogEntry, ...]] = {}
        self._catalog_state: dict[str, dict[str, Any]] = {}
        self._catalog_dirty_scopes: set[str] = set()
        self._last_catalog_refresh_ms = 0
        self._catalog_next_attempt_ms: dict[str, int] = {}
        catalog = config.get("market_catalog", {})
        self._catalog_refresh_ms = max(60_000, int(float(catalog.get("refresh_interval_seconds", 900)) * 1000))
        self._catalog_max_age_ms = max(self._catalog_refresh_ms, int(float(catalog.get("max_age_seconds", 1800)) * 1000))
        self._minimum_turnover = max(0.0, float(catalog.get("minimum_quote_turnover_24h", 1_000_000)))
        self._maximum_spread_bps = max(0.0, float(catalog.get("maximum_spread_bps", 50)))
        self._allowed_quotes = {str(value).upper() for value in catalog.get("allowed_quote_assets", ("USD", "USDT", "USDC"))}
        self._excluded_bases = {str(value).upper() for value in catalog.get("excluded_base_assets", ())}
        self._stop = asyncio.Event()
        self._request_gates = {
            provider_id: _ProviderRequestGate(
                max(1, min(10, int(settings.get("max_concurrent_requests", 4)))),
                max(0, min(10_000, int(settings.get("minimum_request_interval_ms", 100)))),
            )
            for provider_id, settings in config["providers"].items()
            if bool(settings.get("enabled", False))
        }
        self._raw_retention_days = max(1, int(config.get("storage", {}).get("raw_retention_days", 7)))
        self._last_retention_at_ms = 0
        self._last_retention_pruned = 0
        self._depth_cursor = 0
        self.repository.sync_provider_catalog(config["providers"], self.instruments, int(time.time() * 1000))

    async def _provider_request(
        self, provider_id: str, operation: Callable[[], Awaitable[Any]], *, priority: bool = False
    ) -> Any:
        gate = self._request_gates.get(provider_id)
        if gate is None:
            return await operation()
        return await gate.run(operation, priority=priority)

    async def _capture(self, event: NormalizedEvent) -> bool:
        accepted = self.truth_engine.ingest(event)
        if accepted:
            self.repository.save_normalized_event(event)
            from src.providers.types import NormalizedL2
            if isinstance(event, NormalizedL2):
                self.latest_l2[event.metadata.instrument_id] = event
        return accepted

    @staticmethod
    def _supports_catalog(provider: object) -> bool:
        method = getattr(type(provider), "discover_catalog", None)
        return callable(getattr(provider, "discover_catalog", None)) and method is not MarketDataProvider.discover_catalog

    async def _refresh_catalogs(self, now_ms: int) -> dict[str, str]:
        if now_ms - self._last_catalog_refresh_ms < self._catalog_refresh_ms:
            return {}
        force = self._last_catalog_refresh_ms == 0
        self._last_catalog_refresh_ms = now_ms
        providers = [provider for provider in self.providers.enabled() if self._supports_catalog(provider)
            and (force or now_ms >= self._catalog_next_attempt_ms.get(provider.provider_id, 0))]
        results = await asyncio.gather(*(
            asyncio.wait_for(self._provider_request(provider.provider_id, provider.discover_catalog),
                timeout=max(5.0,min(60.0,float(self.config.get('market_catalog',{}).get('request_timeout_seconds',30)))))
            for provider in providers
        ), return_exceptions=True)
        # Catalog receive times are captured after HTTP responses. Evaluate freshness
        # against a clock sampled after those requests, never the pre-request cycle time.
        now_ms = int(time.time() * 1000)
        errors: dict[str, str] = {}
        decision_rows: list[dict[str, Any]] = []
        accepted_scopes: list[MarketCatalogScope] = []
        changed = False
        for provider, result in zip(providers, results, strict=True):
            complete = not isinstance(result, BaseException) and all(scope.complete and scope.entries for scope in result)
            self._catalog_next_attempt_ms[provider.provider_id] = now_ms + (self._catalog_refresh_ms if complete else 60_000)
            if isinstance(result, BaseException):
                message = f"{type(result).__name__}: {result}"[:300]
                errors[provider.provider_id] = message
                scopes = [key for key in self._catalog_state if key.startswith(provider.provider_id + ":")]
                for key in scopes:
                    self._catalog_state[key].update(status="stale", last_error=message, checked_at_ms=now_ms)
                    self._catalog_dirty_scopes.add(key)
                continue
            for snapshot in result:
                key = f"{snapshot.provider}:{snapshot.scope}"
                if snapshot.provider != provider.provider_id or not snapshot.complete or not snapshot.entries:
                    message = snapshot.error or "incomplete or empty catalog scope"
                    errors[key] = message
                    self._catalog_state.setdefault(key, {}).update(
                        status="stale", last_error=message, checked_at_ms=now_ms,
                        received_at_ms=self._catalog_state.get(key, {}).get("received_at_ms"),
                    )
                    self._catalog_dirty_scopes.add(key)
                    continue
                if any(entry.instrument.provider != snapshot.provider or entry.scope != snapshot.scope for entry in snapshot.entries):
                    errors[key] = "catalog scope identity mismatch"
                    self._catalog_state.setdefault(key, {}).update(status="stale", last_error=errors[key], checked_at_ms=now_ms)
                    self._catalog_dirty_scopes.add(key)
                    continue
                previous = {
                    item.instrument.instrument_id: item
                    for item in self._catalog_entries.get(key, ())
                }
                incoming = tuple(entry.instrument for entry in snapshot.entries)
                try:
                    reconciled = self.instruments.reconcile_catalog_scope(snapshot.provider, snapshot.scope, incoming)
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"[:300]
                    errors[key] = message
                    self._catalog_state.setdefault(key, {}).update(status="stale", last_error=message, checked_at_ms=now_ms)
                    self._catalog_dirty_scopes.add(key)
                    continue
                entries_by_id = {entry.instrument.instrument_id: entry for entry in snapshot.entries}
                complete_entries: list[MarketCatalogEntry] = list(snapshot.entries)
                for instrument in reconciled:
                    if instrument.instrument_id not in entries_by_id:
                        old = previous.get(instrument.instrument_id)
                        source = snapshot.source
                        complete_entries.append(MarketCatalogEntry(
                            instrument=instrument, scope=snapshot.scope, source=source,
                            received_at_ms=snapshot.received_at_ms,
                            exchange_timestamp_ms=None, quote_turnover_24h=None, last_price=None,
                            active=False, admitted=False, reasons=("not_returned_by_complete_catalog",),
                        ))
                        decision_rows.append({
                            "instrument_id": instrument.instrument_id, "provider_id": snapshot.provider,
                            "scope": snapshot.scope, "source": source,
                            "catalog_received_at_ms": snapshot.received_at_ms,
                            "evaluated_at_ms": now_ms, "decision": "DELISTED",
                            "reasons": ("not_returned_by_complete_catalog",),
                            "metrics": {"previous_catalog_received_at_ms": old.received_at_ms if old else None},
                        })
                self._catalog_entries[key] = tuple(complete_entries)
                self._catalog_state[key] = {
                    "status": "fresh", "received_at_ms": snapshot.received_at_ms,
                    "checked_at_ms": now_ms, "last_error": None, "page_count": snapshot.page_count,
                    "entry_count": len(snapshot.entries), "source": snapshot.source,
                }
                self._catalog_dirty_scopes.add(key)
                for entry in snapshot.entries:
                    decision_rows.append({
                        "instrument_id": entry.instrument.instrument_id, "provider_id": snapshot.provider,
                        "scope": snapshot.scope, "source": entry.source,
                        "catalog_received_at_ms": entry.received_at_ms, "evaluated_at_ms": now_ms,
                        "decision": "EXCLUDED" if (not entry.active or not entry.admitted) else "ADMITTED",
                        "reasons": entry.reasons,
                        "metrics": {
                            "quote": entry.instrument.quote, "turnover_24h_quote": entry.quote_turnover_24h,
                            "spread_bps": ((entry.ask - entry.bid) / ((entry.ask + entry.bid) / 2) * 10_000)
                            if entry.bid is not None and entry.ask is not None else None,
                            "exchange_timestamp_ms": entry.exchange_timestamp_ms,
                            "receive_age_ms_at_catalog": max(0, now_ms - entry.received_at_ms),
                        },
                    })
                accepted_scopes.append(snapshot)
                changed = True
        if changed:
            self.repository.sync_provider_catalog(self.config["providers"], self.instruments, now_ms)
        if decision_rows:
            self.repository.save_market_discovery_decisions(decision_rows)
        if self._catalog_next_attempt_ms:
            # Retry only failed venues after one minute. A slow venue must
            # neither freeze nor repeatedly reload successful catalogs.
            self._last_catalog_refresh_ms = min(self._catalog_next_attempt_ms.values()) - self._catalog_refresh_ms
        on_catalog_scope = getattr(self, "on_catalog_scope", None)
        if on_catalog_scope is not None:
            for snapshot in accepted_scopes:
                try:
                    await on_catalog_scope(snapshot, int(time.time() * 1000))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log.warning("catalog_alert_failed", error=type(exc).__name__)
        return errors

    def _fresh_catalog_entries(self, now_ms: int) -> list[MarketCatalogEntry]:
        result: list[MarketCatalogEntry] = []
        for key, entries in self._catalog_entries.items():
            state = self._catalog_state.get(key, {})
            age = now_ms - int(state.get("received_at_ms") or 0)
            if state.get("status") != "fresh" or age < 0 or age > self._catalog_max_age_ms:
                continue
            result.extend(entry for entry in entries if entry.instrument.active and entry.active)
        return result

    async def _bbo(self, instrument: Instrument) -> tuple[Instrument, object]:
        provider = self.providers.get(instrument.provider)
        try:
            result = await self._provider_request(
                instrument.provider, lambda: provider.fetch_bbo(instrument), priority=True
            )
            return instrument, result
        except Exception as exc:
            return instrument, exc

    def _candidate(
        self, instrument: Instrument, now_ms: int, entry: MarketCatalogEntry | None = None,
        *, catalog_age_ms: int | None = None,
    ) -> UniverseCandidate:
        bbo = self.truth_engine.latest_bbo(instrument.instrument_id)
        reasons = list(entry.reasons if entry else ())
        bbo_age = None if bbo is None else max(0, now_ms - bbo.metadata.received_at_ms)
        bbo_fresh = bbo is not None and bbo_age is not None and bbo_age <= self.truth_engine.max_source_age_ms
        if bbo_fresh:
            liquidity = min(bbo.bid * bbo.bid_size, bbo.ask * bbo.ask_size)
            fresh_value = max(0.0, 1.0 - float(bbo_age) / self.truth_engine.max_source_age_ms)
            spread = bbo.spread_bps
        else:
            liquidity = fresh_value = spread = None
            if entry is not None:
                reasons.append("fresh_bbo_unavailable")
        volume = (float(bbo.quote_volume_24h) if bbo_fresh and bbo.quote_volume_24h is not None
                  else entry.quote_turnover_24h if entry else None)
        change_magnitude = entry.abs_price_change_24h_pct if entry else None
        if instrument.canonical_asset_id in {"native:bitcoin", "native:solana"}:
            bucket = UniverseBucket.MAJORS_L1
        elif instrument.market_type.value == "perpetual":
            bucket = UniverseBucket.DERIVATIVES_MOVERS
        else:
            bucket = UniverseBucket.LIQUID_SPOT_CANDIDATES
        admitted = True
        if entry is not None:
            if instrument.quote.upper() not in self._allowed_quotes:
                reasons.append("quote_asset_not_supported_without_normalization")
                admitted = False
            if instrument.base.upper() in self._excluded_bases:
                reasons.append("base_asset_is_quote_or_fiat")
                admitted = False
            if entry.quote_turnover_24h is None:
                reasons.append("quote_turnover_unavailable")
                admitted = False
            elif entry.quote_turnover_24h < self._minimum_turnover:
                reasons.append("below_minimum_quote_turnover")
                admitted = False
            if not bbo_fresh:
                admitted = False
            elif spread is not None and spread > self._maximum_spread_bps:
                reasons.append("spread_above_limit")
                admitted = False
        if catalog_age_ms is not None and catalog_age_ms > self._catalog_max_age_ms:
            reasons.append("catalog_stale")
            admitted = False
        return UniverseCandidate(
            instrument_id=instrument.instrument_id,
            bucket=bucket,
            liquidity_quote_units=liquidity,
            quote_turnover_24h=volume,
            change_magnitude_24h_pct=change_magnitude,
            derivatives_activity=(volume if instrument.market_type.value == "perpetual" and volume is not None else None),
            freshness=fresh_value,
            spread_bps=spread,
            quote_asset=instrument.quote,
            admitted=admitted,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    async def _refresh_deep(self, membership: Sequence[UniverseMembership]) -> None:
        deep = [self.instruments.get(item.instrument_id) for item in membership if item.deep_watchlist]
        if deep:
            start = self._depth_cursor % len(deep)
            deep = (deep[start:] + deep[:start])[:5]
            self._depth_cursor = start + len(deep)

        async def fetch(instrument: Instrument) -> list[NormalizedEvent]:
            provider = self.providers.get(instrument.provider)
            results = await asyncio.gather(
                self._provider_request(
                    instrument.provider, lambda: provider.fetch_l2(instrument, 20)
                ),
                self._provider_request(
                    instrument.provider, lambda: provider.fetch_trades(instrument, 100)
                ),
                return_exceptions=True,
            )
            events: list[NormalizedEvent] = []
            for result in results:
                if isinstance(result, list):
                    events.extend(item for item in result if not isinstance(item, BaseException))
                elif not isinstance(result, BaseException):
                    events.append(result)
            return events

        batches = await asyncio.gather(*(fetch(instrument) for instrument in deep), return_exceptions=True)
        for batch in batches:
            if isinstance(batch, BaseException):
                continue
            for event in batch:
                await self._capture(event)

    async def refresh_once(self, *, include_deep: bool = False, refresh_catalog: bool = True) -> dict[str, Any]:
        errors = await self._refresh_catalogs(int(time.time() * 1000)) if refresh_catalog else {}
        now = int(time.time() * 1000)
        catalog_providers = {
            provider.provider_id for provider in self.providers.enabled() if self._supports_catalog(provider)
        }
        entries = self._fresh_catalog_entries(now)
        entry_by_id = {entry.instrument.instrument_id: entry for entry in entries}
        state_by_scope = self._catalog_state

        # Preselect from the broad catalog using measured 24h turnover and catalog
        # freshness. Price/depth enrichment is performed only after this bounded cut.
        pre_candidates: list[UniverseCandidate] = []
        eligible_by_id: dict[str, tuple[MarketCatalogEntry, tuple[str, ...]]] = {}
        for entry in entries:
            instrument = entry.instrument
            reasons = list(entry.reasons)
            if not entry.active or not instrument.active:
                reasons.append("market_not_active")
            if instrument.quote.upper() not in self._allowed_quotes:
                reasons.append("quote_asset_not_supported_without_normalization")
            if instrument.base.upper() in self._excluded_bases:
                reasons.append("base_asset_is_quote_or_fiat")
            if entry.quote_turnover_24h is None:
                reasons.append("quote_turnover_unavailable")
            elif entry.quote_turnover_24h < self._minimum_turnover:
                reasons.append("below_minimum_quote_turnover")
            state_key = f"{entry.instrument.provider}:{entry.scope}"
            catalog_received = int(state_by_scope.get(state_key, {}).get("received_at_ms") or 0)
            catalog_age = max(0, now - catalog_received) if catalog_received else self._catalog_max_age_ms + 1
            if catalog_age > self._catalog_max_age_ms:
                reasons.append("catalog_stale")
            reasons = list(dict.fromkeys(reasons))
            eligible = not reasons and entry.quote_turnover_24h is not None
            eligible_by_id[instrument.instrument_id] = entry, tuple(reasons)
            if not eligible:
                continue
            if instrument.canonical_asset_id in {"native:bitcoin", "native:solana"}:
                bucket = UniverseBucket.MAJORS_L1
            elif instrument.market_type.value == "perpetual":
                bucket = UniverseBucket.DERIVATIVES_MOVERS
            else:
                bucket = UniverseBucket.LIQUID_SPOT_CANDIDATES
            pre_candidates.append(UniverseCandidate(
                instrument.instrument_id, bucket, None, entry.quote_turnover_24h,
                entry.abs_price_change_24h_pct,
                entry.quote_turnover_24h if instrument.market_type.value == "perpetual" else None,
                max(0.0, 1.0 - catalog_age / max(1, self._catalog_max_age_ms)),
                quote_asset=instrument.quote, admitted=True, reasons=(),
            ))

        preselected = self.universe.select(pre_candidates)
        static = [
            item for item in self.instruments.all()
            if item.provider not in catalog_providers
        ]
        scan_limit = int(self.config["universe"]["max_scan_instruments"])
        dynamic_slots = max(0, scan_limit - min(len(static), scan_limit))
        dynamic_ids = [item.instrument_id for item in preselected[:dynamic_slots]]
        scan_instruments = (static[:scan_limit] + [self.instruments.get(item_id) for item_id in dynamic_ids])[:scan_limit]

        grouped: dict[str, list[Instrument]] = {}
        for instrument in scan_instruments:
            grouped.setdefault(instrument.provider, []).append(instrument)
        async def capture_group(provider_id, requested):
            try:
                provider = self.providers.get(provider_id)
                if getattr(provider, "bbo_batch_mode", "per_instrument") == "bulk":
                    batch = getattr(provider, "refresh_bbo_batch")
                    result = await self._provider_request(provider_id, lambda: batch(requested), priority=True)
                    for event in result:
                        if isinstance(event, NormalizedBBO):
                            await self._capture(event)
                else:
                    # Coinbase's public product-book endpoint is per-product. Keep
                    # that scan bounded, then gate every HTTP request individually.
                    enrichment_limit = max(1, int(
                        self.config["providers"][provider_id].get(
                            "bbo_enrichment_limit", len(requested) or 1
                        )
                    ))
                    bounded = requested[:enrichment_limit]
                    outcomes = await asyncio.gather(*(self._bbo(item) for item in bounded))
                    for instrument, event in outcomes:
                        if isinstance(event, BaseException):
                            if not isinstance(event, ProviderCapabilityError):
                                errors[instrument.instrument_id] = f"{type(event).__name__}: {event}"[:300]
                        else:
                            await self._capture(event)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"[:300]
                for instrument in requested:
                    errors[instrument.instrument_id] = message

        async def bounded_group(provider_id, requested):
            try:
                await asyncio.wait_for(capture_group(provider_id, requested), timeout=10.0)
            except TimeoutError:
                errors[provider_id] = "BBO group time budget exceeded"
        await asyncio.gather(*(bounded_group(provider_id, requested) for provider_id, requested in grouped.items()))

        # BBO freshness and decision timestamps must be measured after venue calls.
        now = int(time.time() * 1000)
        provider_health = self.providers.health()
        for health in provider_health.values():
            self.truth_engine.update_provider_health(health)
            self.repository.save_provider_health(health)

        candidates = [
            self._candidate(
                instrument, now, entry_by_id.get(instrument.instrument_id),
                catalog_age_ms=(
                    max(0, now - int(state_by_scope.get(
                        f"{instrument.provider}:{entry_by_id[instrument.instrument_id].scope}", {}
                    ).get("received_at_ms") or now))
                    if instrument.instrument_id in entry_by_id else None
                ),
            )
            for instrument in scan_instruments
        ]
        preselected_ids = set(dynamic_ids)
        self.membership = self.universe.select(candidates)
        self.repository.save_universe_membership(self.membership, now)

        membership_by_id = {item.instrument_id: item for item in self.membership}
        candidate_by_id = {item.instrument_id: item for item in candidates}
        snapshot_rows: dict[str, dict[str, Any]] = {}
        for instrument_id, (entry, eligibility_reasons) in eligible_by_id.items():
            instrument = entry.instrument
            candidate = candidate_by_id.get(instrument_id)
            member = membership_by_id.get(instrument_id)
            snapshot_rows[instrument_id] = {
                "instrument_id": instrument.instrument_id, "canonical_asset_id": instrument.canonical_asset_id,
                "provider": instrument.provider, "venue": instrument.venue, "market_type": instrument.market_type.value,
                "base": instrument.base, "quote": instrument.quote, "venue_symbol": instrument.venue_symbol,
                "chain": instrument.chain, "contract_address": instrument.contract_address,
                "candidate_admitted": candidate.admitted if candidate else False,
                "scanned": candidate is not None, "selected": member is not None,
                "deep_watch": bool(member and member.deep_watchlist),
                "bucket": (member.bucket.value if member else candidate.bucket.value if candidate else None),
                "score": member.score if member else None,
                "reasons": list(member.reasons if member else candidate.reasons if candidate else eligibility_reasons),
                "catalog_source": entry.source, "catalog_scope": entry.scope,
                "catalog_received_at_ms": entry.received_at_ms,
                "catalog_exchange_timestamp_ms": entry.exchange_timestamp_ms,
            }
        for item in candidates:
            if item.instrument_id in snapshot_rows:
                continue
            instrument = self.instruments.get(item.instrument_id)
            member = membership_by_id.get(item.instrument_id)
            snapshot_rows[item.instrument_id] = {
                "instrument_id": instrument.instrument_id, "canonical_asset_id": instrument.canonical_asset_id,
                "provider": instrument.provider, "venue": instrument.venue, "market_type": instrument.market_type.value,
                "base": instrument.base, "quote": instrument.quote, "venue_symbol": instrument.venue_symbol,
                "chain": instrument.chain, "contract_address": instrument.contract_address,
                "candidate_admitted": item.admitted, "scanned": True, "selected": member is not None,
                "deep_watch": bool(member and member.deep_watchlist),
                "bucket": (member.bucket.value if member else item.bucket.value),
                "score": member.score if member else None,
                "reasons": list(member.reasons if member else item.reasons),
                "source": "provider_candidate",
            }
        for item in pre_candidates:
            if item.instrument_id in snapshot_rows:
                continue
            instrument = self.instruments.get(item.instrument_id)
            snapshot_rows[item.instrument_id] = {
                "instrument_id": instrument.instrument_id, "canonical_asset_id": instrument.canonical_asset_id,
                "provider": instrument.provider, "venue": instrument.venue, "market_type": instrument.market_type.value,
                "base": instrument.base, "quote": instrument.quote, "venue_symbol": instrument.venue_symbol,
                "chain": instrument.chain, "contract_address": instrument.contract_address,
                "candidate_admitted": item.admitted, "scanned": False, "selected": False, "deep_watch": False,
                "bucket": item.bucket.value, "score": None,
                "reasons": ["outside_dynamic_scan_budget"], "source": "catalog_preselection",
            }
        self.universe_snapshot = {
            "observed_at_ms": now,
            "candidates": [snapshot_rows[key] for key in sorted(snapshot_rows)],
            "deep_instrument_ids": sorted(item.instrument_id for item in self.membership if item.deep_watchlist),
            "provider_error_scopes": sorted(key for key in errors),
            "catalog_scopes": {key: {"status": value.get("status"),
                "received_at_ms": value.get("received_at_ms"), "checked_at_ms": value.get("checked_at_ms"),
                "source": value.get("source"), "last_error": value.get("last_error")}
                for key, value in sorted(self._catalog_state.items())},
        }

        member_ids = {item.instrument_id for item in self.membership}
        decision_rows: list[dict[str, Any]] = []
        refresh_all = set(self._catalog_dirty_scopes)
        for scope_key, scope_entries in self._catalog_entries.items():
            state = self._catalog_state.get(scope_key, {})
            provider_id, scope = scope_key.split(":", 1)
            scope_age = max(0, now - int(state.get("received_at_ms") or 0)) if state.get("received_at_ms") else None
            is_stale = state.get("status") != "fresh" or scope_age is None or scope_age > self._catalog_max_age_ms
            if is_stale and state.get("status") == "fresh":
                state["status"] = "stale"
                state["last_error"] = "catalog exceeded max_age_seconds"
                self._catalog_dirty_scopes.add(scope_key)
                refresh_all.add(scope_key)
            for entry in scope_entries:
                instrument = entry.instrument
                if scope_key not in refresh_all and instrument.instrument_id not in preselected_ids:
                    continue
                reasons = list(eligible_by_id.get(instrument.instrument_id, (entry, ()))[1])
                candidate = next((item for item in candidates if item.instrument_id == instrument.instrument_id), None)
                if is_stale:
                    decision = "STALE_CATALOG"
                    reasons.append("catalog_stale_or_refresh_failed")
                elif not entry.active or not instrument.active:
                    decision = "DELISTED"
                    reasons.append("market_not_returned_as_active")
                elif instrument.instrument_id in member_ids:
                    decision = "ADMITTED"
                    reasons.extend(candidate.reasons if candidate else ())
                    reasons.append("selected_within_scan_budget")
                else:
                    decision = "EXCLUDED"
                    if not reasons and instrument.instrument_id not in preselected_ids:
                        reasons.append("outside_scan_budget_by_turnover_rank")
                    elif not reasons:
                        reasons.extend(candidate.reasons if candidate else ())
                        reasons.append("not_selected_after_fresh_market_quality_check")
                current_bbo = self.truth_engine.latest_bbo(instrument.instrument_id)
                metrics = {
                    "quote": instrument.quote,
                    "turnover_24h_quote": entry.quote_turnover_24h,
                    "spread_bps": current_bbo.spread_bps if current_bbo else None,
                    "best_bid_liquidity_quote": current_bbo.bid * current_bbo.bid_size if current_bbo else None,
                    "best_ask_liquidity_quote": current_bbo.ask * current_bbo.ask_size if current_bbo else None,
                    "abs_price_change_24h_pct_proxy": entry.abs_price_change_24h_pct,
                    "exchange_timestamp_ms": entry.exchange_timestamp_ms,
                    "catalog_age_ms": scope_age,
                    "bbo_received_at_ms": current_bbo.metadata.received_at_ms if current_bbo else None,
                    "universe_score": next((item.score for item in self.membership if item.instrument_id == instrument.instrument_id), None),
                }
                decision_rows.append({
                    "instrument_id": instrument.instrument_id, "provider_id": provider_id,
                    "scope": scope, "source": entry.source,
                    "catalog_received_at_ms": entry.received_at_ms, "evaluated_at_ms": now,
                    "decision": decision, "reasons": tuple(dict.fromkeys(reasons)), "metrics": metrics,
                })
        if decision_rows:
            self.repository.save_market_discovery_decisions(decision_rows)
        self._catalog_dirty_scopes.clear()

        if now - self._last_retention_at_ms >= 3_600_000:
            cutoff = now - self._raw_retention_days * 86_400_000
            self._last_retention_pruned = self.repository.prune_normalized_ticks(cutoff)
            self._last_retention_at_ms = now
        if include_deep:
            await self._refresh_deep(self.membership)

        truth_assets = {item.canonical_asset_id for item in scan_instruments}
        truth_assets.update(item.canonical_asset_id for item in self.membership)
        self.truth = {asset: self.truth_engine.calculate(asset, now) for asset in sorted(truth_assets)}
        return {
            "providers": {
                key: {
                    "status": health.status.value,
                    "lag_ms": health.lag_ms,
                    "consecutive_failures": health.consecutive_failures,
                    "last_error": health.last_error,
                }
                for key, health in provider_health.items()
            },
            "canonical": {
                key: {
                    "status": value.status.value,
                    "price": value.canonical_price,
                    "quorum": value.quorum,
                    "required_quorum": value.required_quorum,
                    "venue_divergence_bps": value.venue_divergence_bps,
                    "cross_exchange_basis_bps": value.cross_exchange_basis_bps,
                    "stale_sources": list(value.stale_sources),
                    "rejected_outliers": list(value.rejected_outliers),
                }
                for key, value in self.truth.items()
            },
            "universe_size": len(self.membership),
            "deep_watchlist_size": sum(item.deep_watchlist for item in self.membership),
            "catalog": {
                key: {**value, "age_ms": max(0, now - int(value.get("received_at_ms") or now))}
                for key, value in self._catalog_state.items()
            },
            "catalog_entries": len(entries),
            "request_metrics": self.request_metrics(),
            "retention": {
                "raw_retention_days": self._raw_retention_days,
                "last_pruned_rows": self._last_retention_pruned,
                "last_run_at_ms": self._last_retention_at_ms,
            },
            "errors": errors,
        }

    async def run_forever(self) -> None:
        interval = max(2.0, float(self.config["market_truth"].get("poll_interval_seconds", 5)))
        deep_every = max(interval, float(self.config["market_truth"].get("deep_refresh_seconds", 15)))
        async def background(operation, every):
            while not self._stop.is_set():
                try:
                    await operation()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log.warning("market_background_failed", error=type(exc).__name__)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=every)

        tasks = [
            asyncio.create_task(background(lambda: self._refresh_catalogs(int(time.time()*1000)), 5), name="market-catalog"),
            asyncio.create_task(background(lambda: self._refresh_deep(tuple(self.membership)), deep_every), name="market-depth"),
        ]
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    await self.refresh_once(refresh_catalog=False)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.log.warning("multi_venue_refresh_failed", error=type(exc).__name__)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, interval - (time.monotonic() - started)))
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def stop(self) -> None:
        self._stop.set()
        await self.providers.close()

    def health(self) -> dict[str, Any]:
        return {
            key: {
                "status": value.status.value,
                "last_received_at_ms": value.last_received_at_ms,
                "lag_ms": value.lag_ms,
                "consecutive_failures": value.consecutive_failures,
                "last_error": value.last_error,
            }
            for key, value in self.providers.health().items()
        }

    def _catalog_health(self) -> dict[str, dict[str, Any]]:
        now = int(time.time() * 1000)
        result = {}
        for key, value in self._catalog_state.items():
            received = value.get("received_at_ms")
            age = max(0, now - int(received)) if received else None
            status = value.get("status", "stale")
            if age is None or age > self._catalog_max_age_ms:
                status = "stale"
            result[key] = {
                "status": status, "received_at_ms": received, "age_ms": age,
                "entry_count": int(value.get("entry_count", 0)),
                "page_count": int(value.get("page_count", 0)),
                "last_error": value.get("last_error"), "source": value.get("source"),
            }
        for provider in self.providers.enabled():
            if self._supports_catalog(provider) and not any(key.startswith(provider.provider_id + ":") for key in result):
                result[provider.provider_id] = {"status": "stale", "received_at_ms": None, "age_ms": None,
                                                "entry_count": 0, "page_count": 0,
                                                "last_error": "catalog has not completed a successful refresh"}
        return result

    def operational_metrics(self) -> dict[str, Any]:
        statuses = self.providers.health()
        return {
            "providers": {
                key: {
                    "status": value.status.value,
                    "last_received_at_ms": value.last_received_at_ms,
                    "lag_ms": value.lag_ms,
                    "consecutive_failures": value.consecutive_failures,
                }
                for key, value in statuses.items()
            },
            "request_metrics": self.request_metrics(),
            "universe_size": len(self.membership),
            "deep_watchlist_size": sum(item.deep_watchlist for item in self.membership),
            "catalog": self._catalog_health(),
            "degraded_reasons": [
                f"{key}:{value.status.value}"
                for key, value in statuses.items()
                if value.status.value not in {"healthy", "disabled"}
            ],
        }

    def request_metrics(self) -> dict[str, Any]:
        return {provider: gate.snapshot() for provider, gate in self._request_gates.items()}
