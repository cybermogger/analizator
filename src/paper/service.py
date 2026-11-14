from __future__ import annotations

from dataclasses import replace
import time
from typing import Any

from src.data.storage import SQLiteStorage

from .accounting import ShadowPortfolio
from .repository import PaperRepository, stable_hash
from .risk import PaperRiskConfig, PaperRiskEngine
from .types import PaperOpportunity, Recommendation, RiskContext, RiskDecision


HORIZON_MILLISECONDS = {
    "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
    "4h": 14_400_000, "24h": 86_400_000,
}


class PaperDecisionService:
    """Manual recommendations plus shadow-only accounting; never submits an order."""

    def __init__(self, storage: SQLiteStorage, config: dict[str, Any] | None = None):
        self.config = PaperRiskConfig.from_mapping(config)
        self.repository = PaperRepository(storage)
        self.risk = PaperRiskEngine(self.config)
        self.portfolio = ShadowPortfolio(self.repository, self.config.initial_equity_usd)

    @staticmethod
    def decision_identity(opportunity: PaperOpportunity) -> str:
        return stable_hash(
            "paper-decision-v2", opportunity.opportunity_id, opportunity.instrument_id,
            opportunity.horizon, opportunity.model_version,
        )

    @staticmethod
    def material_hash(recommendation: Recommendation) -> str:
        return stable_hash(
            recommendation.decision_id, round(recommendation.calibrated_probability, 3),
            tuple(round(value, 8) for value in recommendation.entry_zone),
            round(recommendation.liquidity_usd, 2),
            round(recommendation.score, 3),
            tuple((item.source_id, item.summary, item.url, item.confirmed)
                  for item in recommendation.supporting_evidence),
            tuple((item.source_id, item.summary, item.url, item.confirmed)
                  for item in recommendation.contradicting_evidence),
            recommendation.status, recommendation.risk_reasons,
        )

    def context(self, opportunity: PaperOpportunity, now_ms: int | None = None) -> RiskContext:
        now_ms = now_ms or int(time.time() * 1000)
        report = self.portfolio.report()
        positions = self.portfolio.positions()
        current_position = positions.get(self.decision_identity(opportunity))
        clusters: dict[str, float] = {}
        last: dict[str, int] = {}
        current_decision_id = self.decision_identity(opportunity)
        for row in self.repository.decisions(100_000):
            rec = row["recommendation"]
            if row["decision_id"] != current_decision_id:
                last[str(row["instrument_id"])] = max(
                    last.get(str(row["instrument_id"]), 0), int(row["decision_at_ms"])
                )
            if row["decision_id"] in positions and row["decision_id"] != current_decision_id:
                cluster = str(rec.get("correlation_cluster", "uncategorized"))
                clusters[cluster] = clusters.get(cluster, 0.0) + positions[row["decision_id"]].notional_usd
        day_ms = 86_400_000
        day_start = now_ms - now_ms % day_ms
        daily_net_pnl = sum(
            float(row["net_pnl_usd"]) for row in self.repository.ledger()
            if int(row["occurred_at_ms"]) >= day_start
        )
        return RiskContext(
            paper_equity_usd=float(report["equity_usd"]),
            current_gross_exposure_usd=max(
                0.0, float(report["gross_exposure_usd"])
                - (current_position.notional_usd if current_position else 0.0)
            ),
            open_position_count=len(positions) - int(current_position is not None),
            cluster_exposure_usd=clusters,
            last_decision_at_ms=last, daily_net_pnl_usd=daily_net_pnl,
            now_ms=now_ms,
        )

    def consider(
        self,
        opportunity: PaperOpportunity,
        context: RiskContext | None = None,
        *,
        observed_at_ms: int | None = None,
    ) -> tuple[Recommendation, RiskDecision, bool]:
        if observed_at_ms is not None:
            now_ms = int(observed_at_ms)
        elif context is not None and context.now_ms:
            now_ms = int(context.now_ms)
        else:
            now_ms = int(time.time() * 1000)
        context = context or self.context(opportunity, now_ms)
        risk = self.risk.evaluate(opportunity, context)
        decision_id = self.decision_identity(opportunity)
        prior_state = self.current_state(decision_id)
        if risk.eligible and prior_state in {"REJECTED", "NO_TRADE", "CLOSED", "RETRACTED", "EXPIRED"}:
            risk = replace(
                risk, eligible=False,
                reasons=tuple(dict.fromkeys((*risk.reasons, "DECISION_TERMINAL"))),
                paper_notional_usd=0.0,
            )
        no_trade = opportunity.direction == "NO_TRADE"
        already_open = decision_id in self.portfolio.positions()
        terminal_statuses = {"REJECTED", "NO_TRADE", "CLOSED", "RETRACTED", "EXPIRED"}
        status = (
            prior_state if prior_state in terminal_statuses else
            "RETRACTED" if already_open and not risk.eligible else
            "NO_TRADE" if no_trade else "RECOMMENDED" if risk.eligible else "REJECTED"
        )
        direction = opportunity.direction if risk.eligible else "NO_TRADE"
        recommendation = Recommendation(
            decision_id=decision_id, opportunity_id=opportunity.opportunity_id,
            canonical_asset_id=opportunity.canonical_asset_id,
            instrument_id=opportunity.instrument_id, chain_id=opportunity.chain_id,
            contract_address=opportunity.contract_address, venue=opportunity.venue,
            decision_at_ms=opportunity.decision_at_ms,
            age_seconds=max(0.0, (now_ms - opportunity.decision_at_ms) / 1000),
            ttl_seconds=self.config.recommendation_ttl_seconds, direction=direction,
            calibrated_probability=opportunity.calibrated_probability,
            expected_net_move=opportunity.expected_net_move,
            entry_zone=opportunity.entry_zone, invalidation=opportunity.invalidation,
            scenarios=opportunity.scenarios, targets=opportunity.targets,
            horizon=opportunity.horizon, spread_bps=opportunity.spread_bps,
            liquidity_usd=opportunity.liquidity_usd,
            estimated_slippage_bps=opportunity.estimated_slippage_bps,
            venue_divergence_bps=opportunity.venue_divergence_bps,
            supporting_evidence=opportunity.supporting_evidence,
            contradicting_evidence=opportunity.contradicting_evidence,
            alternative_scenario=opportunity.alternative_scenario,
            source_credibility=opportunity.source_credibility,
            data_freshness_seconds=opportunity.data_freshness_seconds,
            model_name=opportunity.model_name, model_version=opportunity.model_version,
            data_lineage=opportunity.data_lineage, score=opportunity.score,
            maximum_paper_risk_usd=risk.paper_risk_usd,
            paper_notional_usd=risk.paper_notional_usd if risk.eligible else 0.0,
            segment=opportunity.segment, correlation_cluster=opportunity.correlation_cluster,
            status=status, risk_reasons=risk.reasons,
        )
        inserted = self.repository.save_decision(recommendation, risk)
        material_hash = self.material_hash(recommendation)
        self.repository.save_revision(recommendation, material_hash, now_ms)
        if inserted:
            self.repository.append_transition(
                decision_id, None, status, "RISK_EVALUATED", now_ms,
                {"risk_reasons": list(risk.reasons)},
            )
        open_before = decision_id in self.portfolio.positions()
        terminal_before = self.current_state(decision_id) in {"CLOSED", "RETRACTED", "EXPIRED"}
        if risk.eligible and not terminal_before:
            entry_price = sum(opportunity.entry_zone) / 2
            self.portfolio.open(
                decision_id, opportunity.direction, risk.paper_notional_usd, entry_price,
                now_ms, self.config.entry_fee_bps, opportunity.estimated_slippage_bps,
            )
        elif not risk.eligible:
            if open_before:
                self.portfolio.close(
                    decision_id, sum(opportunity.entry_zone) / 2, now_ms,
                    self.config.exit_fee_bps, opportunity.estimated_slippage_bps,
                    reason="RETRACTION:RISK_GATE:" + ",".join(risk.reasons),
                )
            elif not terminal_before:
                self.repository.save_outcome(
                    decision_id, now_ms, status, 0.0, 0.0,
                    {"risk_reasons": list(risk.reasons), "paper_position_opened": False},
                )
            if context.liquidity_loss or opportunity.evidence_retracted:
                self.retract_open_instrument(
                    opportunity.instrument_id, sum(opportunity.entry_zone) / 2, now_ms,
                    "LIQUIDITY_LOSS" if context.liquidity_loss else "EVIDENCE_RETRACTION",
                    exclude_decision_id=decision_id,
                )
        return recommendation, risk, not self.repository.has_alert(
            decision_id, material_hash
        )

    def record_alert(
        self,
        recommendation: Recommendation,
        message: str,
        sent_at_ms: int,
        telegram_message_id: str | None = None,
    ) -> bool:
        return self.repository.save_alert(
            recommendation.decision_id, self.material_hash(recommendation), sent_at_ms,
            message, telegram_message_id,
        )

    def record_exit_alert(
        self, decision_id: str, reason: str, price: float, message: str,
        sent_at_ms: int, telegram_message_id: str | None = None,
    ) -> bool:
        material_hash = stable_hash(
            "paper-position-exit", decision_id, reason, round(float(price), 10)
        )
        if self.repository.has_alert(decision_id, material_hash):
            return False
        return self.repository.save_alert(
            decision_id, material_hash, sent_at_ms, message, telegram_message_id
        )

    def retract(
        self,
        recommendation: Recommendation,
        price: float,
        occurred_at_ms: int,
        reason: str = "SOURCE_RETRACTION",
    ) -> bool:
        if self.current_state(recommendation.decision_id) in {"CLOSED", "RETRACTED", "EXPIRED"}:
            return False
        closed = self.portfolio.close(
            recommendation.decision_id, price, occurred_at_ms,
            self.config.exit_fee_bps, recommendation.estimated_slippage_bps,
            reason=f"RETRACTION:{reason}",
        )
        if not closed:
            previous = self.current_state(recommendation.decision_id)
            inserted = self.repository.append_transition(
                recommendation.decision_id, previous, "RETRACTED", reason,
                occurred_at_ms, {"price": price},
            )
            if inserted:
                self.repository.save_outcome(
                    recommendation.decision_id, occurred_at_ms, "RETRACTED", 0.0, 0.0,
                    {"reason": reason, "paper_position_opened": False},
                )
            return inserted
        return True

    def expire(self, recommendation: Recommendation, occurred_at_ms: int) -> bool:
        if self.current_state(recommendation.decision_id) in {"CLOSED", "RETRACTED", "EXPIRED"}:
            return False
        if self.portfolio.positions().get(recommendation.decision_id):
            raise ValueError("an open shadow position must be closed, not expired")
        previous = self.current_state(recommendation.decision_id)
        inserted = self.repository.append_transition(
            recommendation.decision_id, previous, "EXPIRED", "TTL_EXPIRED",
            occurred_at_ms, {},
        )
        if inserted:
            self.repository.save_outcome(
                recommendation.decision_id, occurred_at_ms, "EXPIRED", 0.0, 0.0,
                {"paper_position_opened": False},
            )
        return inserted

    def retract_open_instrument(
        self,
        instrument_id: str,
        price: float,
        occurred_at_ms: int,
        reason: str,
        *,
        exclude_decision_id: str | None = None,
    ) -> int:
        positions = self.portfolio.positions()
        closed = 0
        for row in self.repository.decisions(100_000):
            decision_id = str(row["decision_id"])
            if (
                row["instrument_id"] != instrument_id
                or decision_id == exclude_decision_id
                or decision_id not in positions
            ):
                continue
            recommendation = row["recommendation"]
            if self.portfolio.close(
                decision_id, price, occurred_at_ms, self.config.exit_fee_bps,
                float(recommendation.get("estimated_slippage_bps", 0.0)),
                reason=f"RETRACTION:{reason}",
            ):
                closed += 1
        return closed

    def evaluate_open_positions(
        self, instrument_id: str, mark_price: float, observed_at_ms: int
    ) -> list[dict[str, Any]]:
        """Close open shadow positions on observed target, invalidation, or horizon."""
        if mark_price <= 0:
            raise ValueError("paper outcome mark must be positive")
        positions = self.portfolio.positions()
        closed: list[dict[str, Any]] = []
        for row in self.repository.decisions(100_000):
            decision_id = str(row["decision_id"])
            if row["instrument_id"] != instrument_id or decision_id not in positions:
                continue
            recommendation = row["recommendation"]
            direction = str(recommendation.get("direction", ""))
            invalidation = float(recommendation.get("invalidation", 0.0) or 0.0)
            targets = tuple(float(value) for value in recommendation.get("targets", []))
            horizon = str(recommendation.get("horizon", ""))
            opened_at = int(recommendation.get("decision_at_ms", 0))
            reason: str | None = None
            # If a sampled mark crosses both bounds, honor the adverse bound.
            if direction == "LONG" and invalidation > 0 and mark_price <= invalidation:
                reason = "INVALIDATION_EXIT"
            elif direction == "SHORT" and invalidation > 0 and mark_price >= invalidation:
                reason = "INVALIDATION_EXIT"
            elif direction == "LONG" and targets and mark_price >= min(targets):
                reason = "TAKE_PROFIT_EXIT"
            elif direction == "SHORT" and targets and mark_price <= max(targets):
                reason = "TAKE_PROFIT_EXIT"
            elif (
                horizon in HORIZON_MILLISECONDS
                and observed_at_ms >= opened_at + HORIZON_MILLISECONDS[horizon]
            ):
                reason = "HORIZON_EXIT"
            if reason and self.portfolio.close(
                decision_id, mark_price, observed_at_ms, self.config.exit_fee_bps,
                float(recommendation.get("estimated_slippage_bps", 0.0)), reason=reason,
            ):
                closed.append({"decision_id": decision_id, "reason": reason, "price": mark_price})
        return closed

    def current_state(self, decision_id: str) -> str | None:
        transitions = self.repository.transitions(decision_id)
        return str(transitions[-1]["new_state"]) if transitions else None

    def close(
        self, recommendation: Recommendation, price: float, occurred_at_ms: int,
        funding_usd: float = 0.0,
    ) -> bool:
        return self.portfolio.close(
            recommendation.decision_id, price, occurred_at_ms,
            self.config.exit_fee_bps, recommendation.estimated_slippage_bps,
            funding_usd=funding_usd,
        )

    def latest(self, limit: int = 10, asset: str | None = None) -> list[dict[str, Any]]:
        return self.repository.decisions(limit, asset)

    def performance(self) -> dict[str, Any]:
        return self.portfolio.report()

    def health(self) -> dict[str, Any]:
        return {
            "mode": "SHADOW", "real_execution": False,
            "counts": self.repository.counts(), "accounting": self.portfolio.report(),
        }
