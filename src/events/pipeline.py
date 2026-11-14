from __future__ import annotations

import hashlib
import inspect
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .entities import EntityResolver, EventEntity
from .extraction import DeterministicExtractor, Extraction
from .policy import assess_source, combined_credibility, may_publish_as_confirmed
from .repository import EventRepository
from .types import (
    AuthenticityState,
    ContradictionState,
    EventClaim,
    EventType,
    ExpectedDirection,
    ImpactedAsset,
    NormalizedEvent,
    RawEvent,
    SourceTier,
)


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{2,}", re.sub(r"https?://\S+", " ", text.lower())))


def _similarity(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


@dataclass(slots=True)
class _Evidence:
    raw: RawEvent
    extraction: Extraction
    entities: tuple[EventEntity, ...]


class EventIntelligencePipeline:
    """Causal evidence pipeline. It intentionally exposes no trade/signal API."""

    def __init__(
        self,
        resolver: EntityResolver,
        repository: EventRepository | None = None,
        extractor: Any | None = None,
        *,
        dedup_similarity: float = 0.72,
        dedup_window_hours: float = 72.0,
        hydrate: bool = True,
    ):
        self.resolver = resolver
        self.repository = repository
        self.extractor = extractor or DeterministicExtractor()
        self.dedup_similarity = max(0.5, min(1.0, dedup_similarity))
        self.dedup_window_ms = int(max(1.0, dedup_window_hours) * 3_600_000)
        self._clusters: dict[str, list[_Evidence]] = {}
        self._raw_to_cluster: dict[str, str] = {}
        self._latest: dict[str, NormalizedEvent] = {}
        if repository is not None and hydrate:
            for raw in repository.load_raw_events():
                self._ingest_loaded(raw)

    def _extract_sync(self, raw: RawEvent) -> Extraction:
        result = self.extractor.extract(raw)
        if inspect.isawaitable(result):
            raise TypeError("async extractor requires ingest_async")
        return result

    async def _extract_async(self, raw: RawEvent) -> Extraction:
        result = self.extractor.extract(raw)
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _entity_keys(entities: tuple[EventEntity, ...]) -> set[str]:
        return {item.project_id for item in entities}

    def _match_cluster(self, evidence: _Evidence) -> str | None:
        raw = evidence.raw
        if raw.related_event_id and raw.related_event_id in self._raw_to_cluster:
            return self._raw_to_cluster[raw.related_event_id]
        entity_keys = self._entity_keys(evidence.entities)
        for cluster_id, values in reversed(tuple(self._clusters.items())):
            first, latest = values[0], values[-1]
            if raw.received_at_ms - latest.raw.received_at_ms > self.dedup_window_ms:
                continue
            other_keys = self._entity_keys(first.entities)
            if entity_keys or other_keys:
                if not entity_keys.intersection(other_keys):
                    continue
            elif raw.content_hash != first.raw.content_hash:
                continue
            candidate_types = {evidence.extraction.event_type, first.extraction.event_type}
            if len(candidate_types) > 1 and EventType.RUMOR not in candidate_types:
                continue
            if raw.content_hash == first.raw.content_hash or _similarity(raw.text, first.raw.text) >= self.dedup_similarity:
                return cluster_id
        return None

    @staticmethod
    def _cluster_id(raw: RawEvent) -> str:
        return "evt_" + hashlib.sha256(raw.raw_event_id.encode("utf-8")).hexdigest()[:24]

    def _append(self, evidence: _Evidence) -> str:
        cluster_id = self._match_cluster(evidence) or self._cluster_id(evidence.raw)
        self._clusters.setdefault(cluster_id, []).append(evidence)
        self._clusters[cluster_id].sort(key=lambda item: (item.raw.received_at_ms, item.raw.published_at_ms, item.raw.raw_event_id))
        self._raw_to_cluster[evidence.raw.raw_event_id] = cluster_id
        return cluster_id

    def _ingest_loaded(self, raw: RawEvent) -> None:
        evidence = _Evidence(raw, self._extract_sync(raw), self.resolver.resolve(raw))
        cluster_id = self._append(evidence)
        self._latest[cluster_id] = self._normalize(cluster_id)

    def ingest(self, raw: RawEvent) -> NormalizedEvent:
        if self.repository is not None and not self.repository.insert_raw(raw):
            cluster_id = self._raw_to_cluster.get(raw.raw_event_id)
            if cluster_id:
                return self._latest[cluster_id]
            raise ValueError(f"duplicate raw event not present in hydrated state: {raw.raw_event_id}")
        evidence = _Evidence(raw, self._extract_sync(raw), self.resolver.resolve(raw))
        cluster_id = self._append(evidence)
        event = self._normalize(cluster_id)
        self._latest[cluster_id] = event
        if self.repository is not None:
            self.repository.save_normalized(event)
        return event

    async def ingest_async(self, raw: RawEvent) -> NormalizedEvent:
        if self.repository is not None and not self.repository.insert_raw(raw):
            cluster_id = self._raw_to_cluster.get(raw.raw_event_id)
            if cluster_id:
                return self._latest[cluster_id]
            raise ValueError(f"duplicate raw event not present in hydrated state: {raw.raw_event_id}")
        evidence = _Evidence(raw, await self._extract_async(raw), self.resolver.resolve(raw))
        cluster_id = self._append(evidence)
        event = self._normalize(cluster_id)
        self._latest[cluster_id] = event
        if self.repository is not None:
            self.repository.save_normalized(event)
        return event

    def _normalize(self, cluster_id: str) -> NormalizedEvent:
        evidence = tuple(self._clusters[cluster_id])
        raws = tuple(item.raw for item in evidence)
        unique_sources = tuple(sorted({item.source_id for item in raws}))
        credible = sorted(
            evidence,
            key=lambda item: (assess_source(item.raw).credibility, item.raw.received_at_ms),
            reverse=True,
        )
        selected = next(
            (
                item for item in credible
                if not assess_source(item.raw).force_rumor
                and may_publish_as_confirmed(item.extraction.event_type, raws)
            ),
            credible[0],
        )
        selected_type = selected.extraction.event_type
        if not may_publish_as_confirmed(selected_type, raws):
            selected_type = EventType.RUMOR

        contradiction = ContradictionState.NONE
        for raw in raws:
            assessed = assess_source(raw)
            if assessed.contradiction is ContradictionState.RETRACTED:
                contradiction = ContradictionState.RETRACTED
                break
            if assessed.contradiction is ContradictionState.DISPUTED:
                contradiction = ContradictionState.DISPUTED

        entity_by_project: dict[str, EventEntity] = {}
        for item in evidence:
            for entity in item.entities:
                entity_by_project[entity.project_id] = entity
        impacts: list[ImpactedAsset] = []
        for entity in entity_by_project.values():
            explicit_contracts = [
                (chain, address)
                for raw in raws for chain, address in raw.contract_addresses
                if (chain, address) in entity.contracts
            ]
            # The registry may contain knowledge learned later. Replay may expose a
            # contract only after the exact chain/address appeared in received evidence.
            chain, contract = explicit_contracts[0] if explicit_contracts else (None, None)
            impacts.append(ImpactedAsset(
                project_id=entity.project_id,
                canonical_asset_id=entity.canonical_asset_id,
                chain_id=chain,
                contract_address=contract,
                expected_direction=selected.extraction.expected_direction,
                likely_horizons=selected.extraction.likely_horizons,
            ))

        credibility = combined_credibility(raws)
        if selected_type is EventType.RUMOR:
            credibility = min(credibility, 0.35)
        novelty = max(0.10, 1.0 - 0.12 * (len(raws) - 1))
        confidence = credibility * min(1.0, 0.78 + 0.08 * len(unique_sources))
        if contradiction is ContradictionState.DISPUTED:
            confidence *= 0.55
        elif contradiction is ContradictionState.RETRACTED:
            confidence = 0.0
        launch_risk = selected_type is EventType.OFFICIAL_TOKEN_LAUNCH and not any(
            raw.source_tier in {SourceTier.OFFICIAL_PROJECT, SourceTier.OFFICIAL_EXCHANGE}
            and raw.authenticity is AuthenticityState.VERIFIED
            for raw in raws
        )
        authenticity_risk = launch_risk or any(assess_source(raw).authenticity_risk for raw in raws)
        return NormalizedEvent(
            event_id=cluster_id,
            event_type=selected_type,
            summary=selected.extraction.summary,
            novelty=round(novelty, 6),
            credibility=round(credibility, 6),
            confirmation_count=len(unique_sources),
            impacted_assets=tuple(sorted(impacts, key=lambda item: item.project_id)),
            expected_direction=selected.extraction.expected_direction,
            likely_horizons=selected.extraction.likely_horizons,
            decay_half_life_minutes=selected.extraction.decay_half_life_minutes,
            contradiction_state=contradiction,
            confidence=round(max(0.0, min(1.0, confidence)), 6),
            first_published_at_ms=min(item.published_at_ms for item in raws),
            first_received_at_ms=min(item.received_at_ms for item in raws),
            latest_received_at_ms=max(item.received_at_ms for item in raws),
            raw_event_ids=tuple(item.raw_event_id for item in raws),
            authenticity_risk=authenticity_risk,
            high_impact=selected.extraction.high_impact,
            source_ids=unique_sources,
            evidence_urls=tuple(dict.fromkeys(item.url for item in raws)),
        )

    def save_claim(self, claim: EventClaim) -> None:
        if self.repository is None:
            return
        if claim.event_id not in self._clusters:
            raise ValueError("claim references unknown event")
        self.repository.save_claim(claim)

    def latest(self, limit: int = 10) -> tuple[NormalizedEvent, ...]:
        return tuple(sorted(self._latest.values(), key=lambda item: item.latest_received_at_ms, reverse=True)[:limit])

    @classmethod
    def replay(
        cls,
        raw_events: Iterable[RawEvent],
        *,
        resolver: EntityResolver,
        as_of_ms: int,
        extractor: Any | None = None,
    ) -> tuple[NormalizedEvent, ...]:
        pipeline = cls(resolver, extractor=extractor, hydrate=False)
        for raw in sorted(raw_events, key=lambda item: (item.received_at_ms, item.published_at_ms, item.raw_event_id)):
            if raw.received_at_ms <= as_of_ms:
                pipeline.ingest(raw)
        return pipeline.latest(1000)
