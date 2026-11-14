from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from src.data.storage import SQLiteStorage

from .dataset import DatasetSnapshot


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    snapshot_id: str | None
    created_at_ms: int
    status: str
    split_plan: Mapping[str, Any]
    cost_model: Mapping[str, Any]
    baselines: Mapping[str, Any]
    challenger: Mapping[str, Any]
    model_card: Mapping[str, Any]
    promotion_decision: Mapping[str, Any]


class ResearchRegistry:
    def __init__(self, storage: SQLiteStorage) -> None:
        self.storage = storage

    def register_snapshot(
        self,
        snapshot: DatasetSnapshot,
        path: str | Path,
        *,
        created_at_ms: int,
    ) -> None:
        loaded = DatasetSnapshot.read(path)
        if loaded.content_hash != snapshot.content_hash:
            raise ValueError("registered file does not reproduce dataset snapshot")
        self.storage.execute(
            """INSERT OR IGNORE INTO research_dataset_snapshots(
                 snapshot_id,content_hash,created_at_ms,decision_start_ms,decision_end_ms,
                 row_count,code_version,config_hash,feature_schema_json,lineage_json,
                 universe_json,snapshot_path
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot.snapshot_id, snapshot.content_hash, created_at_ms,
                snapshot.decision_start_ms, snapshot.decision_end_ms, len(snapshot.rows),
                snapshot.code_version, snapshot.config_hash,
                SQLiteStorage.json(snapshot.feature_schema), SQLiteStorage.json(snapshot.lineage),
                SQLiteStorage.json(snapshot.universe_definition), str(Path(path).resolve()),
            ),
        )
        rows = self.storage.query(
            "SELECT content_hash,snapshot_path FROM research_dataset_snapshots WHERE snapshot_id=?",
            (snapshot.snapshot_id,),
        )
        if not rows or rows[0]["content_hash"] != snapshot.content_hash:
            raise ValueError("snapshot registry identity collision")

    def reproduce_snapshot(self, snapshot_id: str) -> DatasetSnapshot:
        rows = self.storage.query(
            "SELECT content_hash,snapshot_path FROM research_dataset_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        )
        if not rows:
            raise KeyError(snapshot_id)
        snapshot = DatasetSnapshot.read(rows[0]["snapshot_path"])
        if snapshot.content_hash != rows[0]["content_hash"]:
            raise ValueError("registered dataset no longer reproduces")
        return snapshot

    def register_experiment(self, record: ExperimentRecord) -> None:
        promoted = bool(record.promotion_decision.get("promote", False))
        self.storage.execute(
            """INSERT OR IGNORE INTO research_experiments(
                 experiment_id,snapshot_id,created_at_ms,status,split_plan_json,
                 cost_model_json,baseline_metrics_json,challenger_metrics_json,
                 model_card_json,promotion_decision,promoted
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                record.experiment_id, record.snapshot_id, record.created_at_ms, record.status,
                SQLiteStorage.json(record.split_plan), SQLiteStorage.json(record.cost_model),
                SQLiteStorage.json(record.baselines), SQLiteStorage.json(record.challenger),
                SQLiteStorage.json(record.model_card),
                SQLiteStorage.json(record.promotion_decision), int(promoted),
            ),
        )
        stored = self.experiment(record.experiment_id)
        normalize = lambda value: json.loads(SQLiteStorage.json(value))
        expected = {
            "snapshot_id": record.snapshot_id,
            "status": record.status,
            "split_plan_json": normalize(record.split_plan),
            "cost_model_json": normalize(record.cost_model),
            "baseline_metrics_json": normalize(record.baselines),
            "challenger_metrics_json": normalize(record.challenger),
            "model_card_json": normalize(record.model_card),
            "promotion_decision": normalize(record.promotion_decision),
            "promoted": int(promoted),
        }
        if any(stored[key] != value for key, value in expected.items()):
            raise ValueError("experiment registry identity collision")

    def experiment(self, experiment_id: str) -> dict[str, Any]:
        rows = self.storage.query(
            "SELECT * FROM research_experiments WHERE experiment_id=?", (experiment_id,)
        )
        if not rows:
            raise KeyError(experiment_id)
        row = rows[0]
        for key in (
            "split_plan_json", "cost_model_json", "baseline_metrics_json",
            "challenger_metrics_json", "model_card_json", "promotion_decision",
        ):
            row[key] = json.loads(row[key])
        return row
