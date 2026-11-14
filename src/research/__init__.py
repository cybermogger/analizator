"""Point-in-time forecasting and honest backtest primitives.

This package is intentionally separate from the live SOL/HYPE signal path.  It
contains no broker, wallet, signing, order-placement, or execution capability.
"""

from .dataset import CausalDatasetBuilder, DatasetSnapshot, LeakageError
from .execution import ExecutionAssumptions, ExecutionResult, TradeOpportunity, simulate_execution
from .promotion import PromotionDecision, PromotionPolicy, decide_promotion
from .runner import UntouchedTestAccessError, UntouchedTestVault, run_snapshot_experiment
from .splits import PurgedWalkForwardSplitter, SplitFold, SplitPlan
from .types import FeatureObservation, ForwardOutcome, ResearchRow

__all__ = [
    "CausalDatasetBuilder", "DatasetSnapshot", "ExecutionAssumptions", "ExecutionResult",
    "FeatureObservation", "ForwardOutcome", "LeakageError", "PromotionDecision",
    "PromotionPolicy", "PurgedWalkForwardSplitter", "ResearchRow", "SplitFold",
    "SplitPlan", "TradeOpportunity", "UntouchedTestAccessError", "UntouchedTestVault",
    "decide_promotion", "run_snapshot_experiment", "simulate_execution",
]
