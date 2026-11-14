from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from typing import Mapping, Sequence

from .instruments import InstrumentRegistry


class UniverseBucket(StrEnum):
    MAJORS_L1 = "majors_l1"
    DERIVATIVES_MOVERS = "derivatives_movers"
    DEX_MEMECOIN_CANDIDATES = "dex_memecoin_candidates"
    LIQUID_SPOT_CANDIDATES = "liquid_spot_candidates"


@dataclass(frozen=True, slots=True)
class UniverseCandidate:
    instrument_id: str
    bucket: UniverseBucket
    liquidity_quote_units: float | None
    quote_turnover_24h: float | None
    change_magnitude_24h_pct: float | None
    derivatives_activity: float | None
    freshness: float | None
    spread_bps: float | None = None
    quote_asset: str = "USD"
    admitted: bool = True
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UniverseMembership:
    instrument_id: str
    canonical_asset_id: str
    bucket: UniverseBucket
    score: float
    deep_watchlist: bool
    reasons: tuple[str, ...] = ()


class DynamicUniverse:
    def __init__(self, instruments: InstrumentRegistry, settings: Mapping[str, object]):
        self.instruments = instruments
        self.max_scan = int(settings.get("max_scan_instruments", 150))
        self.max_deep = int(settings.get("max_deep_watchlist", 30))
        self.bucket_minimums = {
            UniverseBucket(key): int(value)
            for key, value in dict(settings.get("bucket_minimums", {})).items()
        }
        self.weights = {str(key): float(value) for key, value in dict(settings.get("weights", {})).items()}
        if not 1 <= self.max_scan <= 150 or not 1 <= self.max_deep <= min(30, self.max_scan):
            raise ValueError("invalid universe budgets")
        if abs(sum(self.weights.values()) - 1.0) > 1e-9:
            raise ValueError("universe score weights must sum to 1")

    @staticmethod
    def _rank(values: list[float]) -> list[float]:
        if not values:
            return []
        ordered = sorted(set(values))
        if len(ordered) == 1:
            return [1.0 for _ in values]
        lookup = {value: index / (len(ordered) - 1) for index, value in enumerate(ordered)}
        return [lookup[value] for value in values]

    def select(self, candidates: Sequence[UniverseCandidate]) -> tuple[UniverseMembership, ...]:
        valid: list[UniverseCandidate] = []
        seen: set[str] = set()
        for item in candidates:
            if item.instrument_id in seen:
                continue
            instrument = self.instruments.get(item.instrument_id)
            metrics = (
                item.liquidity_quote_units, item.quote_turnover_24h, item.change_magnitude_24h_pct,
                item.derivatives_activity, item.freshness, item.spread_bps,
            )
            if not instrument.active or not item.admitted or any(
                value is not None and (not math.isfinite(float(value)) or value < 0) for value in metrics
            ):
                continue
            seen.add(item.instrument_id)
            valid.append(item)

        # Scores compare ordinal ranks only within the same quote asset. USD, USDT and
        # USDC turnover/depth are never treated as equal units without an FX feed.
        metric_values: dict[str, dict[str, float]] = {name: {} for name in (*self.weights, "spread")}
        quote_groups: dict[str, list[UniverseCandidate]] = {}
        for item in valid:
            quote_groups.setdefault(item.quote_asset.upper(), []).append(item)
        for group in quote_groups.values():
            metrics_by_name = {
                "liquidity": [math.log1p(item.liquidity_quote_units) if item.liquidity_quote_units is not None else None for item in group],
                "volume": [math.log1p(item.quote_turnover_24h) if item.quote_turnover_24h is not None else None for item in group],
                "change_magnitude": [item.change_magnitude_24h_pct for item in group],
                "derivatives_activity": [math.log1p(item.derivatives_activity) if item.derivatives_activity is not None else None for item in group],
                "freshness": [item.freshness for item in group],
                "spread": [item.spread_bps for item in group],
            }
            for name, values in metrics_by_name.items():
                present = [float(value) for value in values if value is not None]
                ranks = self._rank(present)
                index = 0
                for item, value in zip(group, values, strict=True):
                    if value is None:
                        continue
                    rank = ranks[index]
                    index += 1
                    metric_values[name][item.instrument_id] = 1.0 - rank if name == "spread" else rank
        scored: list[tuple[UniverseCandidate, float]] = []
        for item in valid:
            available_weights = {name: weight for name, weight in self.weights.items() if item.instrument_id in metric_values.get(name, {})}
            if "spread" in self.weights and item.instrument_id in metric_values["spread"]:
                available_weights["spread"] = self.weights["spread"]
            weight_total = sum(available_weights.values())
            if weight_total <= 0:
                continue
            score = sum(weight * metric_values[name][item.instrument_id] for name, weight in available_weights.items()) / weight_total
            scored.append((item, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0].instrument_id))

        selected: list[tuple[UniverseCandidate, float]] = []
        selected_ids: set[str] = set()
        for bucket, minimum in self.bucket_minimums.items():
            for item, score in (pair for pair in scored if pair[0].bucket == bucket):
                if len([entry for entry in selected if entry[0].bucket == bucket]) >= minimum:
                    break
                if item.instrument_id not in selected_ids and len(selected) < self.max_scan:
                    selected.append((item, score))
                    selected_ids.add(item.instrument_id)
        for item, score in scored:
            if len(selected) >= self.max_scan:
                break
            if item.instrument_id not in selected_ids:
                selected.append((item, score))
                selected_ids.add(item.instrument_id)
        selected.sort(key=lambda pair: (-pair[1], pair[0].instrument_id))
        deep_ids = {item.instrument_id for item, _ in selected[:self.max_deep]}
        return tuple(
            UniverseMembership(
                instrument_id=item.instrument_id,
                canonical_asset_id=self.instruments.get(item.instrument_id).canonical_asset_id,
                bucket=item.bucket,
                score=score,
                deep_watchlist=item.instrument_id in deep_ids,
                reasons=item.reasons,
            )
            for item, score in selected
        )

    def subscription_plan(
        self, membership: Sequence[UniverseMembership], provider_limits: Mapping[str, int]
    ) -> dict[str, dict[str, tuple[str, ...]]]:
        plan: dict[str, dict[str, list[str]]] = {}
        for item in membership:
            instrument = self.instruments.get(item.instrument_id)
            provider_plan = plan.setdefault(instrument.provider, {"scan": [], "deep": []})
            limit = max(0, int(provider_limits.get(instrument.provider, 0)))
            if len(provider_plan["scan"]) >= limit:
                continue
            provider_plan["scan"].append(item.instrument_id)
            if item.deep_watchlist:
                provider_plan["deep"].append(item.instrument_id)
        return {
            provider: {tier: tuple(values) for tier, values in tiers.items()}
            for provider, tiers in plan.items()
        }
