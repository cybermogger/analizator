from __future__ import annotations

import json
import time
from typing import Any, Iterable

from src.data.storage import SQLiteStorage

from .entities import EventEntity
from .types import (
    AuthenticityState,
    EventClaim,
    NormalizedEvent,
    RawEvent,
    RelationType,
    SourceTier,
    canonical_json,
)


class EventRepository:
    def __init__(self, storage: SQLiteStorage):
        self.storage = storage

    def sync_sources(self, sources: dict[str, dict[str, Any]], now_ms: int | None = None) -> None:
        timestamp = now_ms or int(time.time() * 1000)
        rows = []
        for source_id, settings in sources.items():
            safe = {
                key: value for key, value in settings.items()
                if not any(token in str(key).lower() for token in ("secret", "private", "bearer", "token", "key"))
            }
            rows.append((
                source_id, str(settings.get("source_tier", "unofficial_social")),
                str(settings.get("adapter", "unknown")), int(bool(settings.get("enabled", False))),
                int(bool(settings.get("official", False))), canonical_json(safe), timestamp,
            ))
        if rows:
            self.storage.executemany(
                """INSERT INTO event_sources(source_id,source_tier,adapter,enabled,official,config_json,updated_at_ms)
                VALUES (?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
                source_tier=excluded.source_tier,adapter=excluded.adapter,enabled=excluded.enabled,
                official=excluded.official,config_json=excluded.config_json,updated_at_ms=excluded.updated_at_ms""",
                rows,
            )

    def sync_entities(self, entities: Iterable[EventEntity], now_ms: int | None = None) -> None:
        timestamp = now_ms or int(time.time() * 1000)
        rows = [(
            item.project_id, item.canonical_asset_id, item.name,
            canonical_json(item.tickers), canonical_json(item.contracts),
            canonical_json(item.official_account_ids), canonical_json(item.official_domains), timestamp,
        ) for item in entities]
        if rows:
            self.storage.executemany(
                """INSERT INTO event_entities(project_id,canonical_asset_id,name,tickers_json,
                contracts_json,official_accounts_json,official_domains_json,updated_at_ms)
                VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET
                canonical_asset_id=excluded.canonical_asset_id,name=excluded.name,
                tickers_json=excluded.tickers_json,contracts_json=excluded.contracts_json,
                official_accounts_json=excluded.official_accounts_json,
                official_domains_json=excluded.official_domains_json,updated_at_ms=excluded.updated_at_ms""",
                rows,
            )

    def insert_raw(self, raw: RawEvent) -> bool:
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO raw_events(
            raw_event_id,source_id,source_tier,author_id,url,published_at_ms,received_at_ms,
            ingestion_latency_ms,content_hash,language,payload_json,text,relation_type,
            related_event_id,thread_id,authenticity,explicit_project_id,contracts_json,deleted,recorded_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                raw.raw_event_id, raw.source_id, raw.source_tier.value, raw.author_id, raw.url,
                raw.published_at_ms, raw.received_at_ms, raw.ingestion_latency_ms, raw.content_hash,
                raw.language, canonical_json(dict(raw.payload)), raw.text, raw.relation_type.value,
                raw.related_event_id, raw.thread_id, raw.authenticity.value,
                raw.explicit_project_id, canonical_json(raw.contract_addresses), int(raw.deleted),
                int(time.time() * 1000),
            ),
        )
        if cursor.rowcount == 0:
            existing = self.storage.query(
                "SELECT content_hash FROM raw_events WHERE raw_event_id=?", (raw.raw_event_id,)
            )
            if existing and str(existing[0]["content_hash"]) != raw.content_hash:
                self.add_revision(
                    raw.raw_event_id,
                    "content_changed",
                    raw.received_at_ms,
                    {"content_hash": raw.content_hash, "payload": dict(raw.payload), "text": raw.text},
                )
            return False
        self.storage.execute(
            "INSERT INTO event_relations(raw_event_id,relation_type,related_event_id,thread_id) VALUES (?,?,?,?)",
            (raw.raw_event_id, raw.relation_type.value, raw.related_event_id, raw.thread_id),
        )
        revision_type = str(raw.payload.get("revision_type") or "")
        if raw.deleted:
            revision_type = "deleted"
        elif raw.authenticity is AuthenticityState.RETRACTED:
            revision_type = "retracted"
        if revision_type:
            self.add_revision(raw.raw_event_id, revision_type, raw.received_at_ms, {"url": raw.url})
        return True

    def save_normalized(self, event: NormalizedEvent) -> None:
        self.storage.execute(
            """INSERT INTO normalized_events(
            event_id,event_type,summary,novelty,credibility,confirmation_count,expected_direction,
            likely_horizons_json,decay_half_life_minutes,contradiction_state,confidence,
            first_published_at_ms,first_received_at_ms,latest_received_at_ms,raw_event_ids_json,
            source_ids_json,evidence_urls_json,authenticity_risk,high_impact,updated_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET
            event_type=excluded.event_type,summary=excluded.summary,novelty=excluded.novelty,
            credibility=excluded.credibility,confirmation_count=excluded.confirmation_count,
            expected_direction=excluded.expected_direction,likely_horizons_json=excluded.likely_horizons_json,
            decay_half_life_minutes=excluded.decay_half_life_minutes,
            contradiction_state=excluded.contradiction_state,confidence=excluded.confidence,
            first_published_at_ms=excluded.first_published_at_ms,
            first_received_at_ms=excluded.first_received_at_ms,
            latest_received_at_ms=excluded.latest_received_at_ms,
            raw_event_ids_json=excluded.raw_event_ids_json,source_ids_json=excluded.source_ids_json,
            evidence_urls_json=excluded.evidence_urls_json,
            authenticity_risk=excluded.authenticity_risk,high_impact=excluded.high_impact,
            updated_at_ms=excluded.updated_at_ms""",
            (
                event.event_id, event.event_type.value, event.summary, event.novelty, event.credibility,
                event.confirmation_count, event.expected_direction.value,
                canonical_json(event.likely_horizons), event.decay_half_life_minutes,
                event.contradiction_state.value, event.confidence, event.first_published_at_ms,
                event.first_received_at_ms, event.latest_received_at_ms,
                canonical_json(event.raw_event_ids), canonical_json(event.source_ids),
                canonical_json(event.evidence_urls),
                int(event.authenticity_risk), int(event.high_impact), int(time.time() * 1000),
            ),
        )
        self.storage.execute("DELETE FROM event_asset_links WHERE event_id=?", (event.event_id,))
        rows = [(
            event.event_id, item.project_id, item.canonical_asset_id, item.chain_id,
            item.contract_address, item.expected_direction.value, canonical_json(item.likely_horizons),
        ) for item in event.impacted_assets]
        if rows:
            self.storage.executemany(
                """INSERT INTO event_asset_links(event_id,project_id,canonical_asset_id,chain_id,
                contract_address,expected_direction,likely_horizons_json) VALUES (?,?,?,?,?,?,?)""",
                rows,
            )
        for raw_id in event.raw_event_ids:
            rows = self.storage.query("SELECT source_id,received_at_ms FROM raw_events WHERE raw_event_id=?", (raw_id,))
            if rows:
                self.storage.execute(
                    """INSERT OR IGNORE INTO event_confirmations(
                    event_id,raw_event_id,source_id,confirmation_kind,confirmed_at_ms
                    ) VALUES (?,?,?,?,?)""",
                    (event.event_id, raw_id, rows[0]["source_id"], "corroboration", rows[0]["received_at_ms"]),
                )

    def add_revision(self, raw_event_id: str, revision_type: str, observed_at_ms: int, payload: dict[str, Any]) -> None:
        self.storage.execute(
            "INSERT INTO event_revisions(raw_event_id,revision_type,observed_at_ms,payload_json) VALUES (?,?,?,?)",
            (raw_event_id, revision_type, observed_at_ms, canonical_json(payload)),
        )

    def save_claim(self, claim: EventClaim) -> None:
        self.storage.execute(
            """INSERT OR IGNORE INTO event_claims(
            claim_id,event_id,claim_type,evidence_url,payload_json,recorded_at_ms
            ) VALUES (?,?,?,?,?,?)""",
            (
                claim.claim_id, claim.event_id, claim.claim_type.value, claim.evidence_url,
                canonical_json(dict(claim.payload)), int(time.time() * 1000),
            ),
        )

    def save_source_health(
        self,
        source_id: str,
        status: str,
        checked_at_ms: int,
        last_received_at_ms: int | None,
        consecutive_failures: int,
        last_error: str | None,
        last_successful_poll_at_ms: int | None = None,
    ) -> None:
        lag = max(0, checked_at_ms - last_received_at_ms) if last_received_at_ms else None
        self.storage.execute(
            """INSERT INTO event_source_health(
            source_id,status,checked_at_ms,last_received_at_ms,lag_ms,consecutive_failures,last_error,
            last_successful_poll_at_ms
            ) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET
            status=excluded.status,checked_at_ms=excluded.checked_at_ms,
            last_received_at_ms=excluded.last_received_at_ms,lag_ms=excluded.lag_ms,
            consecutive_failures=excluded.consecutive_failures,last_error=excluded.last_error,
            last_successful_poll_at_ms=excluded.last_successful_poll_at_ms""",
            (source_id, status, checked_at_ms, last_received_at_ms, lag, consecutive_failures,
             last_error, last_successful_poll_at_ms),
        )

    def event_source_health(self) -> list[dict[str, Any]]:
        return self.storage.query("SELECT * FROM event_source_health ORDER BY source_id")

    def latest_events(self, limit: int = 10, as_of_ms: int | None = None) -> list[dict[str, Any]]:
        where = "" if as_of_ms is None else "WHERE latest_received_at_ms<=?"
        params: tuple[Any, ...] = () if as_of_ms is None else (as_of_ms,)
        rows = self.storage.query(
            f"SELECT * FROM normalized_events {where} ORDER BY latest_received_at_ms DESC LIMIT ?",
            (*params, max(1, min(int(limit), 100))),
        )
        for row in rows:
            row["impacted_assets"] = self.storage.query(
                "SELECT * FROM event_asset_links WHERE event_id=? ORDER BY project_id", (row["event_id"],)
            )
            for key in ("likely_horizons_json", "raw_event_ids_json", "source_ids_json", "evidence_urls_json"):
                row[key[:-5]] = json.loads(row[key])
        return rows

    def raw_event_count(self) -> int:
        rows = self.storage.query("SELECT COUNT(*) AS count FROM raw_events")
        return int(rows[0]["count"]) if rows else 0

    def load_raw_events(self, limit: int = 1000) -> tuple[RawEvent, ...]:
        rows = self.storage.query(
            "SELECT * FROM raw_events ORDER BY received_at_ms DESC LIMIT ?", (max(1, min(limit, 10_000)),)
        )
        values = []
        for row in reversed(rows):
            values.append(RawEvent(
                raw_event_id=str(row["raw_event_id"]),
                source_id=str(row["source_id"]),
                source_tier=SourceTier(str(row["source_tier"])),
                author_id=row["author_id"],
                url=str(row["url"]),
                published_at_ms=int(row["published_at_ms"]),
                received_at_ms=int(row["received_at_ms"]),
                content_hash=str(row["content_hash"]),
                language=row["language"],
                payload=json.loads(row["payload_json"]),
                text=str(row["text"]),
                relation_type=RelationType(str(row["relation_type"])),
                related_event_id=row["related_event_id"],
                thread_id=row["thread_id"],
                authenticity=AuthenticityState(str(row["authenticity"])),
                explicit_project_id=row["explicit_project_id"],
                contract_addresses=tuple(tuple(item) for item in json.loads(row["contracts_json"])),
                deleted=bool(row["deleted"]),
            ))
        return tuple(values)
