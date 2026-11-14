from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


@dataclass(frozen=True)
class PromotionPolicy:
    minimum_test_samples: int = 200
    minimum_trades: int = 30
    minimum_time_blocks: int = 3
    maximum_drawdown: float = 0.20
    maximum_ece: float = 0.10
    require_all_blocks_positive: bool = True


@dataclass(frozen=True)
class PromotionDecision:
    status: str
    promote: bool
    incumbent: str
    challenger: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "promote": self.promote,
            "incumbent": self.incumbent,
            "challenger": self.challenger,
            "reasons": list(self.reasons),
        }


def decide_promotion(
    *,
    challenger_name: str,
    challenger: Mapping[str, object],
    baselines: Mapping[str, Mapping[str, object]],
    incumbent: str,
    policy: PromotionPolicy,
) -> PromotionDecision:
    reasons: list[str] = []
    if challenger.get("status") != "EVALUATED":
        return PromotionDecision(
            "NOT_EVALUABLE", False, incumbent, challenger_name,
            (str(challenger.get("reason", "challenger was not evaluated")),),
        )
    if int(challenger.get("sample_count", 0)) < policy.minimum_test_samples:
        reasons.append("insufficient untouched test samples")
    if int(challenger.get("trade_count", 0)) < policy.minimum_trades:
        reasons.append("insufficient executed opportunities")
    if int(challenger.get("time_block_count", 0)) < policy.minimum_time_blocks:
        reasons.append("insufficient independent time blocks")
    if float(challenger.get("maximum_drawdown", float("inf"))) > policy.maximum_drawdown:
        reasons.append("drawdown limit exceeded")
    if float(challenger.get("ece", float("inf"))) > policy.maximum_ece:
        reasons.append("calibration limit exceeded")
    blocks = challenger.get("time_blocks", {})
    if policy.require_all_blocks_positive and isinstance(blocks, Mapping) and any(
        float(value.get("net_expectancy", 0.0)) <= 0
        for value in blocks.values() if isinstance(value, Mapping)
    ):
        reasons.append("challenger is not positive in every required time block")

    challenger_ci = challenger.get("net_expectancy_ci95")
    if not isinstance(challenger_ci, list) or len(challenger_ci) != 2:
        reasons.append("challenger expectancy uncertainty is unavailable")
    else:
        applicable = {
            name: metrics for name, metrics in baselines.items()
            if metrics.get("status") == "EVALUATED"
        }
        if not applicable:
            reasons.append("no applicable evaluated baseline")
        for name, metrics in applicable.items():
            baseline_ci = metrics.get("net_expectancy_ci95")
            baseline_upper = (
                float(baseline_ci[1])
                if isinstance(baseline_ci, list) and len(baseline_ci) == 2
                else float(metrics.get("net_expectancy", 0.0))
            )
            if float(challenger_ci[0]) <= baseline_upper:
                reasons.append(f"does not beat {name} after costs with uncertainty")
            baseline_brier = metrics.get("brier")
            if baseline_brier is not None and float(challenger.get("brier", 1.0)) > float(baseline_brier):
                reasons.append(f"calibration Brier is worse than {name}")
            if float(challenger.get("maximum_drawdown", float("inf"))) > float(
                metrics.get("maximum_drawdown", float("inf"))
            ):
                reasons.append(f"drawdown is worse than {name}")
    if reasons:
        return PromotionDecision("RETAIN_CHAMPION", False, incumbent, challenger_name,
                                 tuple(dict.fromkeys(reasons)))
    return PromotionDecision("PROMOTE_CHALLENGER", True, incumbent, challenger_name, ())
