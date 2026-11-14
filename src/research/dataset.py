from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .types import FeatureObservation, ResearchRow


class LeakageError(ValueError):
    """Raised when an observation was not knowable at decision time."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DatasetSnapshot:
    snapshot_id: str
    content_hash: str
    rows: tuple[ResearchRow, ...]
    code_version: str
    config_hash: str
    feature_schema: Mapping[str, str]
    lineage: tuple[str, ...]
    universe_definition: Mapping[str, Any]

    @property
    def decision_start_ms(self) -> int | None:
        return min((row.decision_at_ms for row in self.rows), default=None)

    @property
    def decision_end_ms(self) -> int | None:
        return max((row.decision_at_ms for row in self.rows), default=None)

    def content(self) -> dict[str, Any]:
        return {
            "format_version": 1,
            "code_version": self.code_version,
            "config_hash": self.config_hash,
            "feature_schema": dict(sorted(self.feature_schema.items())),
            "lineage": sorted(self.lineage),
            "universe_definition": self.universe_definition,
            "rows": [row.to_dict() for row in self.rows],
        }

    def write(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "snapshot_id": self.snapshot_id,
            "content_hash": self.content_hash,
            "content": self.content(),
        }
        destination.write_text(_canonical_json(envelope) + "\n", encoding="utf-8")
        return destination

    @classmethod
    def read(cls, path: str | Path) -> "DatasetSnapshot":
        envelope = json.loads(Path(path).read_text(encoding="utf-8"))
        content = envelope["content"]
        digest = stable_hash(content)
        if digest != envelope.get("content_hash") or digest != envelope.get("snapshot_id"):
            raise ValueError("Dataset snapshot hash mismatch")
        rows = tuple(ResearchRow.from_dict(item) for item in content["rows"])
        return cls(
            snapshot_id=digest,
            content_hash=digest,
            rows=rows,
            code_version=str(content["code_version"]),
            config_hash=str(content["config_hash"]),
            feature_schema=dict(content["feature_schema"]),
            lineage=tuple(content["lineage"]),
            universe_definition=dict(content["universe_definition"]),
        )


class CausalDatasetBuilder:
    """Build an immutable dataset after strict availability-time validation."""

    def __init__(
        self,
        *,
        code_version: str,
        config: Mapping[str, Any],
        feature_schema: Mapping[str, str],
        universe_definition: Mapping[str, Any],
    ) -> None:
        self.code_version = code_version
        self.config_hash = stable_hash(config)
        self.feature_schema = dict(feature_schema)
        self.universe_definition = dict(universe_definition)

    @staticmethod
    def _validate_feature(feature: FeatureObservation, decision_at_ms: int) -> None:
        if feature.available_at_ms > decision_at_ms:
            raise LeakageError(f"future feature {feature.name}: available after decision")
        if feature.published_at_ms is not None and feature.published_at_ms > decision_at_ms:
            raise LeakageError(f"future publication {feature.name}")
        if (
            feature.revision_observed_at_ms is not None
            and feature.revision_observed_at_ms > decision_at_ms
        ):
            raise LeakageError(f"future revision {feature.name}")
        if feature.source_kind == "candle":
            if feature.candle_closed is not True:
                raise LeakageError(f"open candle {feature.name}")
            if feature.candle_close_at_ms is None or feature.candle_close_at_ms > decision_at_ms:
                raise LeakageError(f"future candle close {feature.name}")

    @classmethod
    def validate_row(cls, row: ResearchRow) -> None:
        if not row.instrument_active_at_decision:
            raise LeakageError(f"inactive instrument {row.instrument_id} at decision")
        if row.universe_valid_from_ms > row.decision_at_ms:
            raise LeakageError(f"future universe membership {row.instrument_id}")
        if row.universe_valid_to_ms is not None and row.decision_at_ms >= row.universe_valid_to_ms:
            raise LeakageError(f"expired universe membership {row.instrument_id}")
        if len({feature.name for feature in row.features}) != len(row.features):
            raise ValueError(f"duplicate feature names in {row.row_id}")
        for feature in row.features:
            cls._validate_feature(feature, row.decision_at_ms)
        for outcome in row.outcomes:
            if outcome.label_end_at_ms <= row.decision_at_ms:
                raise ValueError(f"non-forward outcome {outcome.horizon} in {row.row_id}")

    def build(self, rows: Iterable[ResearchRow]) -> DatasetSnapshot:
        ordered = tuple(sorted(rows, key=lambda item: (item.decision_at_ms, item.row_id)))
        if len({row.row_id for row in ordered}) != len(ordered):
            raise ValueError("row_id must be unique")
        for row in ordered:
            self.validate_row(row)
        lineage = tuple(sorted({feature.lineage_id for row in ordered for feature in row.features}))
        content = {
            "format_version": 1,
            "code_version": self.code_version,
            "config_hash": self.config_hash,
            "feature_schema": dict(sorted(self.feature_schema.items())),
            "lineage": list(lineage),
            "universe_definition": self.universe_definition,
            "rows": [row.to_dict() for row in ordered],
        }
        digest = stable_hash(content)
        return DatasetSnapshot(
            snapshot_id=digest,
            content_hash=digest,
            rows=ordered,
            code_version=self.code_version,
            config_hash=self.config_hash,
            feature_schema=self.feature_schema,
            lineage=lineage,
            universe_definition=self.universe_definition,
        )
