from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import Any

from src.data.storage import SQLiteStorage

from .types import Recommendation, RiskDecision


def stable_hash(*values: object) -> str:
    encoded = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class PaperRepository:
    """Append-only decision, transition, outcome, alert and accounting store."""

    def __init__(self, storage: SQLiteStorage):
        self.storage = storage

    def save_decision(self, recommendation: Recommendation, risk: RiskDecision) -> bool:
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO paper_decisions(
            decision_id,opportunity_id,canonical_asset_id,instrument_id,decision_at_ms,
            direction,status,recommendation_json,risk_json,created_at_ms
            ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                recommendation.decision_id, recommendation.opportunity_id,
                recommendation.canonical_asset_id, recommendation.instrument_id,
                recommendation.decision_at_ms, recommendation.direction,
                recommendation.status, self.storage.json(recommendation.as_dict()),
                self.storage.json(asdict(risk)), recommendation.decision_at_ms,
            ),
        )
        return cursor.rowcount > 0

    def save_revision(
        self, recommendation: Recommendation, material_hash: str, observed_at_ms: int
    ) -> bool:
        revision_id = stable_hash(recommendation.decision_id, material_hash)
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO paper_recommendation_revisions(
            revision_id,decision_id,observed_at_ms,material_hash,recommendation_json
            ) VALUES (?,?,?,?,?)""",
            (
                revision_id, recommendation.decision_id, observed_at_ms, material_hash,
                self.storage.json(recommendation.as_dict()),
            ),
        )
        return cursor.rowcount > 0

    def append_transition(
        self,
        decision_id: str,
        previous_state: str | None,
        new_state: str,
        reason: str,
        occurred_at_ms: int,
        payload: dict[str, Any] | None = None,
    ) -> bool:
        transition_id = stable_hash(decision_id, previous_state, new_state, reason, occurred_at_ms)
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO paper_state_transitions(
            transition_id,decision_id,previous_state,new_state,reason,occurred_at_ms,payload_json
            ) VALUES (?,?,?,?,?,?,?)""",
            (
                transition_id, decision_id, previous_state, new_state, reason,
                occurred_at_ms, self.storage.json(payload or {}),
            ),
        )
        return cursor.rowcount > 0

    def append_ledger(self, entry: dict[str, Any]) -> bool:
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO paper_ledger(
            entry_id,decision_id,event_type,occurred_at_ms,direction,quantity_delta,price,
            notional_usd,cash_delta_usd,gross_pnl_usd,net_pnl_usd,fees_usd,
            funding_usd,slippage_usd,metadata_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                entry["entry_id"], entry.get("decision_id"), entry["event_type"],
                entry["occurred_at_ms"], entry.get("direction", 0),
                entry.get("quantity_delta", 0.0), entry.get("price", 0.0),
                entry.get("notional_usd", 0.0), entry.get("cash_delta_usd", 0.0),
                entry.get("gross_pnl_usd", 0.0), entry.get("net_pnl_usd", 0.0),
                entry.get("fees_usd", 0.0), entry.get("funding_usd", 0.0),
                entry.get("slippage_usd", 0.0), self.storage.json(entry.get("metadata", {})),
            ),
        )
        return cursor.rowcount > 0

    def save_outcome(
        self,
        decision_id: str,
        evaluated_at_ms: int,
        outcome_state: str,
        gross_pnl_usd: float,
        net_pnl_usd: float,
        payload: dict[str, Any],
    ) -> bool:
        # One decision has one terminal accounting outcome. State transitions
        # retain later expiry/retraction observations without contradictory PnL.
        if self.storage.query(
            "SELECT 1 FROM paper_outcomes WHERE decision_id=? LIMIT 1", (decision_id,)
        ):
            return False
        outcome_id = stable_hash(decision_id, "terminal-outcome")
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO paper_outcomes(
            outcome_id,decision_id,evaluated_at_ms,outcome_state,gross_pnl_usd,
            net_pnl_usd,payload_json) VALUES (?,?,?,?,?,?,?)""",
            (
                outcome_id, decision_id, evaluated_at_ms, outcome_state,
                gross_pnl_usd, net_pnl_usd, self.storage.json(payload),
            ),
        )
        return cursor.rowcount > 0

    def save_alert(
        self,
        decision_id: str,
        material_hash: str,
        sent_at_ms: int,
        message: str,
        telegram_message_id: str | None,
    ) -> bool:
        alert_id = stable_hash(decision_id, material_hash)
        cursor = self.storage.execute(
            """INSERT OR IGNORE INTO paper_alerts(
            alert_id,decision_id,material_hash,sent_at_ms,telegram_message_id,message
            ) VALUES (?,?,?,?,?,?)""",
            (alert_id, decision_id, material_hash, sent_at_ms, telegram_message_id, message),
        )
        return cursor.rowcount > 0

    def has_alert(self, decision_id: str, material_hash: str) -> bool:
        return bool(self.storage.query(
            "SELECT 1 FROM paper_alerts WHERE decision_id=? AND material_hash=?",
            (decision_id, material_hash),
        ))

    def decisions(self, limit: int = 20, asset: str | None = None) -> list[dict[str, Any]]:
        if asset:
            rows = self.storage.query(
                """SELECT * FROM paper_decisions
                WHERE canonical_asset_id=? OR instrument_id=?
                ORDER BY decision_at_ms DESC LIMIT ?""", (asset, asset, limit)
            )
        else:
            rows = self.storage.query(
                "SELECT * FROM paper_decisions ORDER BY decision_at_ms DESC LIMIT ?", (limit,)
            )
        for row in rows:
            revisions = self.storage.query(
                """SELECT recommendation_json FROM paper_recommendation_revisions
                WHERE decision_id=? ORDER BY observed_at_ms DESC,rowid DESC LIMIT 1""",
                (row["decision_id"],),
            )
            encoded = revisions[0]["recommendation_json"] if revisions else row["recommendation_json"]
            row["recommendation"] = json.loads(encoded)
            row["risk"] = json.loads(row["risk_json"])
        return rows

    def ledger(self) -> list[dict[str, Any]]:
        return self.storage.query(
            "SELECT * FROM paper_ledger ORDER BY occurred_at_ms,entry_id"
        )

    def transitions(self, decision_id: str | None = None) -> list[dict[str, Any]]:
        if decision_id:
            return self.storage.query(
                """SELECT * FROM paper_state_transitions WHERE decision_id=?
                ORDER BY occurred_at_ms,rowid""", (decision_id,)
            )
        return self.storage.query(
            "SELECT * FROM paper_state_transitions ORDER BY occurred_at_ms,rowid"
        )

    def outcomes(self) -> list[dict[str, Any]]:
        return self.storage.query("SELECT * FROM paper_outcomes ORDER BY evaluated_at_ms")

    def counts(self) -> dict[str, int]:
        tables = (
            "paper_decisions", "paper_recommendation_revisions", "paper_state_transitions",
            "paper_ledger", "paper_outcomes", "paper_alerts",
        )
        return {
            table: int(self.storage.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
            for table in tables
        }
