from __future__ import annotations

import json
import time
from typing import Any

from src.data.storage import SQLiteStorage

from .types import (
    CandidateAssessment,
    OnChainEvent,
    OnChainEventType,
    TokenIdentity,
    canonical_json,
)


class OnChainRepository:
    """Immutable evidence store with point-in-time canonical-chain views."""

    def __init__(self, storage: SQLiteStorage):
        self.storage = storage

    def sync_providers(self, providers: dict[str, dict[str, Any]], now_ms: int | None = None) -> None:
        timestamp = now_ms or int(time.time() * 1000)
        rows: list[tuple[Any, ...]] = []
        for provider_id, settings in providers.items():
            safe = {
                key: value for key, value in settings.items()
                if not any(word in str(key).lower() for word in ("secret", "private", "seed", "token", "key", "authorization"))
            }
            rows.append((
                provider_id, str(settings["chain_id"]), str(settings["adapter"]),
                int(bool(settings.get("enabled", False))), canonical_json(safe), timestamp,
            ))
        if rows:
            self.storage.executemany(
                """INSERT INTO onchain_providers(
                provider_id,chain_id,adapter,enabled,config_json,updated_at_ms
                ) VALUES (?,?,?,?,?,?) ON CONFLICT(provider_id) DO UPDATE SET
                chain_id=excluded.chain_id,adapter=excluded.adapter,enabled=excluded.enabled,
                config_json=excluded.config_json,updated_at_ms=excluded.updated_at_ms""",
                rows,
            )

    def insert_event(self, event: OnChainEvent) -> str:
        existing = self.storage.query(
            "SELECT payload_hash,block_hash FROM onchain_events WHERE event_id=?", (event.event_id,)
        )
        if existing:
            if (
                str(existing[0]["payload_hash"]) != event.payload_hash
                or str(existing[0]["block_hash"]) != event.block_hash
            ):
                raise ValueError(f"event_id collision with changed evidence: {event.event_id}")
            return "duplicate"

        payload = dict(event.payload)
        self.storage.execute(
            """INSERT INTO onchain_assets(
            identity_key,chain_id,contract_address,symbol,name,first_seen_at_ms,last_seen_at_ms,metadata_json
            ) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(identity_key) DO UPDATE SET
            symbol=COALESCE(excluded.symbol,onchain_assets.symbol),
            name=COALESCE(excluded.name,onchain_assets.name),
            first_seen_at_ms=MIN(onchain_assets.first_seen_at_ms,excluded.first_seen_at_ms),
            last_seen_at_ms=MAX(onchain_assets.last_seen_at_ms,excluded.last_seen_at_ms)""",
            (
                event.identity.key, event.identity.chain_id, event.identity.contract_address,
                payload.get("symbol"), payload.get("name"), event.received_at_ms,
                event.received_at_ms, "{}",
            ),
        )
        self.storage.execute(
            """INSERT INTO onchain_events(
            event_id,provider_id,identity_key,chain_id,contract_address,event_type,
            block_number,block_hash,parent_hash,transaction_id,transaction_index,log_index,
            position_key,exchange_timestamp_ms,received_at_ms,payload_json,payload_hash,
            chain_verified,confirmations,recorded_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                event.event_id, event.provider_id, event.identity.key, event.identity.chain_id,
                event.identity.contract_address, event.event_type.value, event.block_number,
                event.block_hash, event.parent_hash, event.transaction_id,
                event.transaction_index, event.log_index, event.position_key,
                event.exchange_timestamp_ms, event.received_at_ms, canonical_json(payload),
                event.payload_hash, int(event.chain_verified), event.confirmations,
                int(time.time() * 1000),
            ),
        )
        reorg = self._rebuild_position_history(event.position_key)
        return "reorg" if reorg else "inserted"

    def _rebuild_position_history(self, position_key: str) -> bool:
        events = self.storage.query(
            """SELECT event_id,block_hash,received_at_ms FROM onchain_events
            WHERE position_key=? ORDER BY received_at_ms,event_id""",
            (position_key,),
        )
        if not events:
            return False
        ids = [str(row["event_id"]) for row in events]
        marks = ",".join("?" for _ in ids)
        self.storage.execute(
            f"DELETE FROM onchain_canonicality_history WHERE event_id IN ({marks})", ids
        )
        self.storage.execute("DELETE FROM onchain_reorgs WHERE position_key=?", (position_key,))
        previous: dict[str, Any] | None = None
        reorg = False
        for row in events:
            observed = int(row["received_at_ms"])
            if previous is not None:
                self.storage.execute(
                    """INSERT INTO onchain_canonicality_history(
                    event_id,observed_at_ms,is_canonical,block_hash,reason
                    ) VALUES (?,?,?,?,?)""",
                    (previous["event_id"], observed, 0, previous["block_hash"], "superseded_position"),
                )
                if str(previous["block_hash"]) != str(row["block_hash"]):
                    reorg = True
                    self.storage.execute(
                        """INSERT INTO onchain_reorgs(
                        position_key,old_event_id,new_event_id,old_block_hash,new_block_hash,observed_at_ms
                        ) VALUES (?,?,?,?,?,?)""",
                        (
                            position_key, previous["event_id"], row["event_id"],
                            previous["block_hash"], row["block_hash"], observed,
                        ),
                    )
            self.storage.execute(
                """INSERT INTO onchain_canonicality_history(
                event_id,observed_at_ms,is_canonical,block_hash,reason
                ) VALUES (?,?,?,?,?)""",
                (row["event_id"], observed, 1, row["block_hash"], "observed"),
            )
            previous = row
        return reorg

    def canonical_events(self, identity: TokenIdentity, as_of_ms: int) -> tuple[OnChainEvent, ...]:
        rows = self.storage.query(
            """SELECT e.* FROM onchain_events e
            WHERE e.identity_key=? AND e.received_at_ms<=?
            AND NOT EXISTS (
              SELECT 1 FROM onchain_events newer
              WHERE newer.position_key=e.position_key AND newer.received_at_ms<=?
              AND (newer.received_at_ms>e.received_at_ms
                   OR (newer.received_at_ms=e.received_at_ms AND newer.event_id>e.event_id))
            )
            ORDER BY e.block_number,e.transaction_index,e.log_index,
                     e.exchange_timestamp_ms,e.received_at_ms,e.event_id""",
            (identity.key, as_of_ms, as_of_ms),
        )
        return tuple(self._event(row) for row in rows)

    @staticmethod
    def _event(row: dict[str, Any]) -> OnChainEvent:
        return OnChainEvent(
            event_id=str(row["event_id"]), provider_id=str(row["provider_id"]),
            identity=TokenIdentity(str(row["chain_id"]), str(row["contract_address"])),
            event_type=OnChainEventType(str(row["event_type"])),
            block_number=int(row["block_number"]), block_hash=str(row["block_hash"]),
            parent_hash=row["parent_hash"], transaction_id=str(row["transaction_id"]),
            transaction_index=int(row["transaction_index"]), log_index=int(row["log_index"]),
            exchange_timestamp_ms=int(row["exchange_timestamp_ms"]),
            received_at_ms=int(row["received_at_ms"]), payload=json.loads(row["payload_json"]),
            payload_hash=str(row["payload_hash"]), chain_verified=bool(row["chain_verified"]),
            confirmations=int(row["confirmations"]),
        )

    def save_assessment(self, assessment: CandidateAssessment) -> None:
        self.storage.execute(
            """INSERT INTO onchain_assessments(
            identity_key,as_of_ms,status,payload_json,recorded_at_ms
            ) VALUES (?,?,?,?,?) ON CONFLICT(identity_key,as_of_ms) DO UPDATE SET
            status=excluded.status,payload_json=excluded.payload_json,
            recorded_at_ms=excluded.recorded_at_ms""",
            (
                assessment.identity.key, assessment.as_of_ms, assessment.status.value,
                canonical_json(assessment.as_dict()), int(time.time() * 1000),
            ),
        )

    def latest_assessments(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self.storage.query(
            """SELECT a.payload_json FROM onchain_assessments a
            WHERE NOT EXISTS (
              SELECT 1 FROM onchain_assessments newer
              WHERE newer.identity_key=a.identity_key AND newer.as_of_ms>a.as_of_ms
            ) ORDER BY a.as_of_ms DESC LIMIT ?""",
            (max(1, min(int(limit), 100)),),
        )
        return [json.loads(row["payload_json"]) for row in rows]

    def save_provider_health(
        self,
        provider_id: str,
        chain_id: str,
        status: str,
        checked_at_ms: int,
        last_received_at_ms: int | None,
        consecutive_failures: int,
        last_error: str | None,
    ) -> None:
        lag = max(0, checked_at_ms - last_received_at_ms) if last_received_at_ms else None
        self.storage.execute(
            """INSERT INTO onchain_provider_health(
            provider_id,chain_id,status,checked_at_ms,last_received_at_ms,lag_ms,
            consecutive_failures,last_error
            ) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(provider_id) DO UPDATE SET
            chain_id=excluded.chain_id,status=excluded.status,checked_at_ms=excluded.checked_at_ms,
            last_received_at_ms=excluded.last_received_at_ms,lag_ms=excluded.lag_ms,
            consecutive_failures=excluded.consecutive_failures,last_error=excluded.last_error""",
            (
                provider_id, chain_id, status, checked_at_ms, last_received_at_ms,
                lag, consecutive_failures, last_error,
            ),
        )

    def provider_health(self) -> list[dict[str, Any]]:
        return self.storage.query("SELECT * FROM onchain_provider_health ORDER BY provider_id")
