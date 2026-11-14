from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from src.features.regime import MarketRegime, regime_component_multipliers

from .rules import ComponentProbability


@dataclass(frozen=True)
class EnsembleResult:
    probabilities: tuple[float, float, float]
    weights: dict[str, float]
    reasons: tuple[str, ...]


def combine_probabilities(
    components: Mapping[str, ComponentProbability],
    base_weights: Mapping[str, float],
    regime: MarketRegime,
    horizon: str,
    performance: Mapping[str, float] | None = None,
    prior: tuple[float, float, float] = (0.24, 0.24, 0.52),
    apply_regime_multipliers: bool = True,
) -> EnsembleResult:
    multipliers = regime_component_multipliers(regime, horizon) if apply_regime_multipliers else {}
    raw_weights: dict[str, float] = {}
    for name, component in components.items():
        perf = float((performance or {}).get(name, 1.0))
        raw_weights[name] = max(0.0, float(base_weights.get(name, 0)) * component.quality * multipliers.get(name, 1.0) * np.clip(perf, 0, 1.5))
    total = sum(raw_weights.values())
    if total <= 1e-12:
        return EnsembleResult(prior, {name: 0.0 for name in components}, ())
    weights = {name: weight / total for name, weight in raw_weights.items()}
    combined = sum(weights[name] * np.asarray(components[name].probabilities) for name in weights)
    aggregate_quality = min(1.0, total / max(sum(base_weights.values()), 1e-12))
    # Conservative uncertainty shrinkage during cold start/missing microstructure.
    combined = aggregate_quality * combined + (1 - aggregate_quality) * np.asarray(prior)
    combined = np.maximum(combined, 0)
    combined /= combined.sum()
    ranked = sorted(
        ((weights[name] * abs(components[name].probabilities[0] - components[name].probabilities[1]), name) for name in weights),
        reverse=True,
    )
    reasons: list[str] = []
    for _, name in ranked:
        reasons.extend(components[name].reasons)
        if len(reasons) >= 6:
            break
    return EnsembleResult(tuple(combined.tolist()), weights, tuple(dict.fromkeys(reasons[:6])))
