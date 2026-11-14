from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class FeatureObservation:
    """One feature value with the times required for point-in-time validation."""

    name: str
    value: float | int | str | bool | None
    available_at_ms: int
    source_kind: str
    lineage_id: str
    published_at_ms: int | None = None
    revision_observed_at_ms: int | None = None
    candle_close_at_ms: int | None = None
    candle_closed: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FeatureObservation":
        return cls(**dict(value))


@dataclass(frozen=True)
class ForwardOutcome:
    horizon: str
    horizon_ms: int
    label_end_at_ms: int
    gross_return: float
    net_return: float
    tp_before_stop: bool | None
    mae: float
    mfe: float
    realized_volatility: float
    exit_feasible: bool
    direction: int = 1
    entry_price: float | None = None
    exit_price: float | None = None
    entry_liquidity_usd: float | None = None
    exit_liquidity_usd: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ForwardOutcome":
        return cls(**dict(value))


@dataclass(frozen=True)
class ResearchRow:
    row_id: str
    instrument_id: str
    canonical_asset_id: str
    universe_bucket: str
    regime: str
    decision_at_ms: int
    universe_valid_from_ms: int
    universe_valid_to_ms: int | None
    instrument_active_at_decision: bool
    features: tuple[FeatureObservation, ...]
    outcomes: tuple[ForwardOutcome, ...] = field(default_factory=tuple)

    @property
    def label_end_at_ms(self) -> int:
        if not self.outcomes:
            return self.decision_at_ms
        return max(item.label_end_at_ms for item in self.outcomes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "instrument_id": self.instrument_id,
            "canonical_asset_id": self.canonical_asset_id,
            "universe_bucket": self.universe_bucket,
            "regime": self.regime,
            "decision_at_ms": self.decision_at_ms,
            "universe_valid_from_ms": self.universe_valid_from_ms,
            "universe_valid_to_ms": self.universe_valid_to_ms,
            "instrument_active_at_decision": self.instrument_active_at_decision,
            "features": [item.to_dict() for item in self.features],
            "outcomes": [item.to_dict() for item in self.outcomes],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResearchRow":
        data = dict(value)
        data["features"] = tuple(FeatureObservation.from_dict(item) for item in data["features"])
        data["outcomes"] = tuple(ForwardOutcome.from_dict(item) for item in data.get("outcomes", ()))
        return cls(**data)
