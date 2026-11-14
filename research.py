from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from src.data.storage import SQLiteStorage
from src.research.dataset import DatasetSnapshot
from src.research.experiment import not_evaluable_model_card
from src.research.runner import run_snapshot_experiment, write_model_card


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic offline Phase 4 experiment; never places orders."
    )
    parser.add_argument("--snapshot", type=Path, help="immutable causal dataset JSON")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--db", type=Path, help="research SQLite registry path")
    parser.add_argument("--output", type=Path, help="model-card JSON output path")
    arguments = parser.parse_args()
    if arguments.snapshot is None:
        print(json.dumps(not_evaluable_model_card(), indent=2, sort_keys=True))
        return 0
    if arguments.db is None or arguments.output is None:
        parser.error("--db and --output are required with --snapshot")

    # Deliberately parse YAML directly: offline research never loads .env.
    config = yaml.safe_load(arguments.config.read_text(encoding="utf-8"))
    snapshot = DatasetSnapshot.read(arguments.snapshot)
    storage = SQLiteStorage(arguments.db)
    try:
        run = run_snapshot_experiment(
            snapshot=snapshot,
            snapshot_path=arguments.snapshot,
            config=config,
            storage=storage,
        )
        write_model_card(run.model_card, arguments.output)
        print(json.dumps({
            "status": run.model_card["status"],
            "experiment_id": run.experiment_id,
            "snapshot_id": snapshot.snapshot_id,
            "evaluated_tasks": len(run.model_card.get("task_metrics", {})),
            "test_access_count": run.test_access_count,
            "promotion": bool(run.model_card["promotion"]["promote"]),
            "model_card": str(arguments.output.resolve()),
            "registry": str(arguments.db.resolve()),
        }, indent=2, sort_keys=True))
    finally:
        storage.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
