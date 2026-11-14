from __future__ import annotations

from dataclasses import dataclass, replace
import json
from typing import Any

from src.providers.types import ProviderStatus

from .repository import stable_hash
from .service import PaperDecisionService
from .types import Evidence, PaperOpportunity, Recommendation, RiskContext, RiskDecision


@dataclass(frozen=True)
class OrchestratedDecision:
    recommendation: Recommendation
    risk: RiskDecision
    alert_needed: bool
    terminal_state: str | None
    closed_positions: tuple[dict[str, Any], ...] = ()


class ShadowDecisionOrchestrator:
    """Causal adapter from the current production signal path into Phase 5."""

    def __init__(
        self,
        paper: PaperDecisionService,
        market_data: Any,
        events: Any,
        onchain: Any,
        forecast_performance: Any,
    ) -> None:
        self.paper = paper
        self.market_data = market_data
        self.events = events
        self.onchain = onchain
        self.forecast_performance = forecast_performance

    def _instrument(self, coin: str) -> Any:
        matches = [
            item for item in self.market_data.instruments.for_provider("hyperliquid")
            if item.base == coin and item.market_type.value == "perpetual"
        ]
        if len(matches) != 1:
            raise ValueError(f"expected one exact Hyperliquid perpetual identity for {coin}")
        return matches[0]

    @staticmethod
    def _event_evidence(
        event: dict[str, Any], now_ms: int, direction: str
    ) -> tuple[Evidence, bool, bool]:
        expected = str(event.get("expected_direction", "unknown")).lower()
        contradiction = str(event.get("contradiction_state", "none")).lower()
        authenticity_risk = bool(event.get("authenticity_risk", False))
        confirmed = not (
            str(event.get("event_type", "")).upper() == "RUMOR"
            and int(event.get("confirmation_count", 0)) < 2
        ) and not authenticity_risk and contradiction not in {"contradicted", "retracted"}
        supports = (
            contradiction == "none"
            and not authenticity_risk
            and (expected == "unknown" or expected == "mixed"
                 or direction == "LONG" and expected == "positive"
                 or direction == "SHORT" and expected == "negative")
        )
        evidence = Evidence(
            category="event", source_id=str(event.get("event_id", "unknown-event")),
            summary=str(event.get("summary", event.get("event_type", "event")))[:500],
            url=next((str(url) for url in event.get("evidence_urls", []) if url), None),
            credibility=float(event.get("credibility", 0.0)),
            freshness_seconds=max(0.0, (now_ms - int(event.get("latest_received_at_ms", now_ms))) / 1000),
            lineage_ids=tuple(str(value) for value in event.get("raw_event_ids", [])),
            confirmed=confirmed,
        )
        return evidence, supports, contradiction == "retracted"

    def _evidence(
        self,
        signal: dict[str, Any],
        instrument: Any,
        now_ms: int,
        truth: Any | None,
        liquidity_usd: float,
        spread_bps: float,
    ) -> tuple[tuple[Evidence, ...], tuple[Evidence, ...], bool, bool, float]:
        freshness = signal.get("data_freshness", {})
        market_lineage = tuple(str(value) for value in signal.get("source_lineage", []))
        supporting: list[Evidence] = [Evidence(
            category="market", source_id="hyperliquid:live-book-context",
            summary=f"Hyperliquid decision-time spread {spread_bps:.2f} bps; visible L2 depth ${liquidity_usd:,.0f}",
            url=None, credibility=float(freshness.get("quality", 0.0)),
            freshness_seconds=float(freshness.get("age_seconds") or 0.0),
            lineage_ids=market_lineage, confirmed=freshness.get("status") == "FRESH",
        )]
        contradicting: list[Evidence] = []
        if truth is not None:
            truth_evidence = Evidence(
                category="market_truth", source_id=f"market-truth:{instrument.canonical_asset_id}",
                summary=(
                    f"multi-venue status={truth.status.value}; quorum={truth.quorum}/"
                    f"{truth.required_quorum}; divergence={truth.venue_divergence_bps} bps"
                ),
                url=None, credibility=1.0 if truth.status is ProviderStatus.HEALTHY else 0.5,
                freshness_seconds=max(0.0, (now_ms - truth.calculated_at_ms) / 1000),
                lineage_ids=tuple(
                    f"{item.provider}:{item.instrument_id}:{truth.calculated_at_ms}"
                    for item in truth.contributions
                ),
                confirmed=truth.canonical_price is not None,
            )
            (supporting if truth.status is ProviderStatus.HEALTHY else contradicting).append(truth_evidence)

        event_retracted = False
        news_confirmed = True
        repository = getattr(self.events, "repository", None)
        events = repository.latest_events(100, as_of_ms=now_ms) if repository is not None else []
        for event in events:
            links = [
                link for link in event.get("impacted_assets", [])
                if str(link.get("canonical_asset_id")) == instrument.canonical_asset_id
                and (
                    not instrument.onchain_identity
                    or (
                        str(link.get("chain_id")) == instrument.chain
                        and str(link.get("contract_address", "")).lower()
                        == str(instrument.contract_address).lower()
                    )
                )
            ]
            if not links:
                continue
            item, supports, retracted = self._event_evidence(event, now_ms, signal["direction"])
            (supporting if supports else contradicting).append(item)
            event_retracted = event_retracted or retracted
            news_confirmed = news_confirmed and item.confirmed

        if instrument.onchain_identity:
            for assessment in self.onchain.latest(100):
                if int(assessment.get("as_of_ms", 0)) > now_ms:
                    continue
                if (
                    str(assessment.get("chain_id")) != instrument.chain
                    or str(assessment.get("contract_address", "")).lower()
                    != str(instrument.contract_address).lower()
                ):
                    continue
                eligible = str(assessment.get("status", "")).upper() == "ELIGIBLE"
                item = Evidence(
                    category="onchain",
                    source_id=f"onchain:{instrument.chain}:{instrument.contract_address}",
                    summary=(
                        f"on-chain status={assessment.get('status')}; liquidity="
                        f"${float(assessment.get('liquidity_usd', 0)):,.0f}; "
                        f"flags={','.join(str(value) for value in assessment.get('risk_flags', [])) or 'none'}"
                    ),
                    url=None, credibility=1.0 if eligible else 0.7,
                    freshness_seconds=max(0.0, (now_ms - int(assessment["as_of_ms"])) / 1000),
                    lineage_ids=tuple(str(value) for value in assessment.get("evidence_event_ids", [])),
                    confirmed=eligible,
                )
                (supporting if eligible else contradicting).append(item)

        evidence = supporting + contradicting
        credibility = sum(item.credibility for item in evidence) / len(evidence) if evidence else 0.0
        return tuple(supporting), tuple(contradicting), news_confirmed, event_retracted, credibility

    def _model_drift(self, coin: str) -> bool:
        summary = self.forecast_performance.summary(coin)
        minimum = int(self.paper.config.model_drift_min_samples)
        if int(summary.get("samples", 0)) < minimum:
            return False
        balanced = summary.get("balanced_accuracy")
        brier = summary.get("brier")
        ece = summary.get("ece")
        return bool(
            balanced is None or float(balanced) < self.paper.config.model_drift_min_balanced_accuracy
            or brier is None or float(brier) > self.paper.config.model_drift_max_brier
            or ece is None or float(ece) > self.paper.config.model_drift_max_ece
        )

    def process(self, signal: dict[str, Any], now_ms: int) -> OrchestratedDecision:
        coin = str(signal["coin"])
        instrument = self._instrument(coin)
        truth = self.market_data.truth.get(instrument.canonical_asset_id)
        if truth is not None and truth.calculated_at_ms > now_ms:
            truth = None
        book = signal.get("orderbook", {})
        mark = float(signal["mark"])
        spread_bps = max(0.0, float(book.get("relative_spread", 0.0)) * 10_000)
        liquidity_usd = max(
            0.0, (float(book.get("bid_depth", 0.0)) + float(book.get("ask_depth", 0.0))) * mark
        )
        probe = min(self.paper.config.maximum_paper_risk_usd, self.paper.config.initial_equity_usd * .0025)
        estimated_slippage_bps = spread_bps / 2 + probe / max(liquidity_usd, 1.0) * 10_000
        divergence_bps = max(0.0, float(truth.venue_divergence_bps or 0.0)) if truth else 0.0
        supporting, contradicting, news_confirmed, evidence_retracted, credibility = self._evidence(
            signal, instrument, now_ms, truth, liquidity_usd, spread_bps
        )
        complete_lineage = tuple(dict.fromkeys((
            *(str(value) for value in signal.get("source_lineage", [])),
            *(value for item in (*supporting, *contradicting) for value in item.lineage_ids),
        )))

        source = signal.get("decision_source", {})
        anchor = int(source.get("open_time_ms", signal["timestamp"]))
        production_version = str(signal.get("production_model_version", "production-ensemble-v1"))
        opportunity_id = stable_hash(
            "production-signal", instrument.instrument_id, signal["primary_horizon"],
            source.get("interval"), anchor, signal["direction"],
        )
        direction = str(signal["direction"])
        paper_direction = direction if direction in {"LONG", "SHORT"} else "NO_TRADE"
        plan = signal.get("plan", {})
        entry_zone = tuple(float(value) for value in (plan.get("entry_zone") or (mark, mark)))
        invalidation = float(plan.get("invalidation") or mark * .999)
        targets = tuple(
            float(value) for value in (plan.get("tp1"), plan.get("tp2"))
            if value is not None and float(value) > 0
        ) or tuple(float(value) for value in signal.get("expected_range", (mark, mark)))
        if paper_direction == "LONG":
            gross_move = max(0.0, min(targets) / mark - 1.0)
        elif paper_direction == "SHORT":
            gross_move = max(0.0, 1.0 - max(targets) / mark)
        else:
            gross_move = 0.0
        expected_net_move = gross_move - (
            self.paper.config.entry_fee_bps + self.paper.config.exit_fee_bps
            + 2 * estimated_slippage_bps
        ) / 10_000
        if paper_direction == "NO_TRADE":
            expected_net_move = 0.0
        probabilities = signal["horizons"][signal["primary_horizon"]]
        probability = float(probabilities[direction if direction in probabilities else "WAIT"]) / 100
        freshness = signal.get("data_freshness", {})
        alert_eligible = bool(signal.get("alert_decision", {}).get("eligible", False))
        opportunity = PaperOpportunity(
            opportunity_id=opportunity_id,
            canonical_asset_id=instrument.canonical_asset_id,
            instrument_id=instrument.instrument_id, segment=(
                "majors_l1" if instrument.canonical_asset_id in {"native:solana", "native:bitcoin"}
                else "liquid_alts"
            ),
            direction=paper_direction, decision_at_ms=int(signal["timestamp"]),
            horizon=str(signal["primary_horizon"]), calibrated_probability=probability,
            expected_net_move=expected_net_move, entry_zone=(entry_zone[0], entry_zone[-1]),
            invalidation=invalidation, scenarios=tuple(str(value) for value in signal.get("reasons", []))
            or (f"current production ensemble in {signal.get('regime', 'UNKNOWN')} regime",),
            targets=targets, spread_bps=spread_bps, liquidity_usd=liquidity_usd,
            estimated_slippage_bps=estimated_slippage_bps,
            venue_divergence_bps=divergence_bps,
            predicted_volatility=max(0.0, float(signal.get("volatility", {}).get("percent", 0.0)) / 100),
            predicted_mae=-abs(invalidation / mark - 1.0),
            exit_feasibility_probability=min(
                1.0, liquidity_usd / max(self.paper.config.minimum_liquidity_usd, 1.0)
            ),
            chain_id=instrument.chain, contract_address=instrument.contract_address,
            venue=instrument.venue, supporting_evidence=supporting,
            contradicting_evidence=contradicting,
            alternative_scenario=(
                f"Remain in cash or invalidate if price crosses {invalidation:.8g}; "
                "contradicting evidence takes precedence."
            ),
            source_credibility=credibility,
            data_freshness_seconds=float(freshness.get("age_seconds") or 0.0),
            model_name="current-production-ensemble", model_version=production_version,
            data_lineage=complete_lineage,
            score=float(signal.get("signal_strength", 0.0)),
            risk_eligible=paper_direction in {"LONG", "SHORT"} and alert_eligible,
            news_confirmed=news_confirmed, evidence_retracted=evidence_retracted,
            correlation_cluster=("solana-beta" if coin == "SOL" else "crypto-beta"),
        )

        base = self.paper.context(opportunity, now_ms)
        provider_health = self.market_data.health()
        primary = provider_health.get(instrument.provider, {})
        primary_status = str(primary.get("status", "unknown")).lower()
        truth_stale = bool(
            truth is not None
            and now_ms - truth.calculated_at_ms > self.market_data.truth_engine.max_source_age_ms
        )
        provider_disagreement = bool(
            truth is None or truth.status is not ProviderStatus.HEALTHY
            or truth.canonical_price is None or truth.quorum < truth.required_quorum
            or truth_stale
        )
        previous = self.paper.latest(1, instrument.canonical_asset_id)
        previous_liquidity = (
            float(previous[0]["recommendation"].get("liquidity_usd", 0.0)) if previous else 0.0
        )
        liquidity_loss = bool(
            previous_liquidity > 0
            and liquidity_usd < previous_liquidity * self.paper.config.liquidity_loss_fraction
        )
        previous_signal = self.market_data.repository.latest_signal(coin)
        previous_mark = None
        if previous_signal is not None:
            try:
                prior_payload = json.loads(previous_signal["payload_json"])
                previous_mark = float(prior_payload["mark"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                previous_mark = None
        price_gap_bps = (
            abs(mark / previous_mark - 1.0) * 10_000
            if previous_mark is not None and previous_mark > 0 else 0.0
        )
        risk_context = replace(
            base,
            feed_stale=str(freshness.get("status", "STALE")) != "FRESH",
            provider_disagreement=provider_disagreement,
            primary_venue_outage=primary_status in {"disconnected", "stale", "disabled"},
            liquidity_loss=liquidity_loss,
            model_drift=self._model_drift(coin),
            extreme_gap=(
                spread_bps > self.paper.config.maximum_gap_bps
                or price_gap_bps > self.paper.config.maximum_gap_bps
            ),
        )
        recommendation, risk, alert_needed = self.paper.consider(
            opportunity, risk_context, observed_at_ms=now_ms
        )
        # Risk retractions (stale/disagreeing feeds, gaps, liquidity loss) take
        # precedence over sampled target/horizon exits for the same observation.
        closed_positions = tuple(self.paper.evaluate_open_positions(
            instrument.instrument_id, mark, now_ms
        ))
        return OrchestratedDecision(
            recommendation, risk, alert_needed,
            self.paper.current_state(recommendation.decision_id), closed_positions,
        )
