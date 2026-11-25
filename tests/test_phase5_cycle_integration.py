from __future__ import annotations

import copy
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src.alerts.alert_gate import AlertDecision
from src.alerts.commands import TelegramCommandService
from src.hyperliquid.schemas import AssetContext, BookLevel, BookSnapshot
from src.providers.types import (
    CanonicalMarketTruth, MarketType, ProviderStatus, VenueContribution,
)
from src.service.analyzer import AnalyzerService
from src.service.config import load_config


class OfflineCycleREST:
    def __init__(self, now_ms: int):
        self.now_ms = now_ms

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def meta_and_asset_contexts(self):
        contexts = {
            coin: AssetContext(
                coin, self.now_ms, mark, mark, mark, 0.0001, 10_000,
                5_000_000, 0.0, mark, self.now_ms, self.now_ms,
            )
            for coin, mark in (("SOL", 100.0), ("BTC", 60_000.0))
        }
        return {"universe": [{"name": "SOL"}, {"name": "BTC"}]}, contexts

    async def l2_book(self, coin: str):
        return BookSnapshot(
            coin, self.now_ms,
            (BookLevel(99.99, 25_000, 10),),
            (BookLevel(100.01, 25_000, 10),),
            self.now_ms,
        )


class FakeTelegram:
    def __init__(self):
        self.messages: list[str] = []
        self.chat_id = "test"

    @property
    def configured(self) -> bool:
        return True

    async def send(self, text: str) -> str:
        self.messages.append(text)
        return str(len(self.messages))

    async def send_to(self, _chat_id: str, text: str) -> str:
        return await self.send(text)


def production_signal(direction: str = "LONG", *, stale: bool = False, depth: float = 25_000) -> dict[str, Any]:
    probabilities = (
        {"LONG": 68.0, "SHORT": 12.0, "WAIT": 20.0}
        if direction == "LONG" else {"LONG": 15.0, "SHORT": 15.0, "WAIT": 70.0}
    )
    plan = (
        {"entry_zone": (99.5, 100.2), "tp1": 104.0, "tp2": 108.0,
         "invalidation": 96.0, "expected_zone": (104.0, 108.0)}
        if direction == "LONG" else
        {"entry_zone": None, "tp1": None, "tp2": None, "invalidation": None,
         "expected_zone": (98.0, 102.0)}
    )
    return {
        "coin": "SOL", "timestamp": 0, "mark": 100.0,
        "horizons": {"1h": probabilities}, "primary_horizon": "1h",
        "direction": direction, "probability": probabilities[direction],
        "plan": plan, "plans": {direction: plan}, "regime": "TREND_UP",
        "reasons": ["current production technical/structure agreement"],
        "derivatives": {},
        "orderbook": {
            "available": True, "relative_spread": .0002,
            "bid_depth": depth, "ask_depth": depth,
        },
        "tradeflow": {}, "btc_context": {}, "model_status": "MODEL_WARMING_UP",
        "details": {"1h": {"ml_ready": False}},
        "production_model_version": "production-ensemble-v1-no-accepted-ml",
        "decision_source": {"interval": "15m", "open_time_ms": 1_750_000_000_000,
                            "received_at_ms": 1_750_000_001_000},
        "source_lineage": ["hyperliquid:context:SOL:1", "hyperliquid:l2Book:SOL:1"],
        "confidence": 75.0 if direction == "LONG" else 20.0,
        "signal_strength": 78.0 if direction == "LONG" else 10.0,
        "directional_gap": 56.0 if direction == "LONG" else 0.0,
        "timeframe_agreement": .8, "component_agreement": .75,
        "independent_confirmations": 3 if direction == "LONG" else 0,
        "expected_move": (4.0, 8.0) if direction == "LONG" else (-2.0, 2.0),
        "expected_range": (98.0, 108.0),
        "target_range": (104.0, 108.0) if direction == "LONG" else None,
        "invalidation": 96.0 if direction == "LONG" else None,
        "support": 96.0, "resistance": 108.0,
        "volatility": {"percent": 2.0, "atr": 2.0, "range_expansion": 1.0},
        "data_freshness": {
            "status": "STALE" if stale else "FRESH", "age_seconds": 300.0 if stale else 1.0,
            "quality": 0.0 if stale else .95,
        },
        "market_events": [], "outcome_threshold_pct": 1.0,
    }


def healthy_truth(now_ms: int, *, divergence_bps: float = 12.0) -> CanonicalMarketTruth:
    contributions = (
        VenueContribution("hyperliquid", "hyperliquid", "hyperliquid:perpetual:SOL-USDC",
                          MarketType.PERPETUAL, 100.0, 1.0, 1, 1, 2.0, 5_000_000),
        VenueContribution("coinbase", "coinbase-advanced", "coinbase:spot:SOL-USD",
                          MarketType.SPOT, 100.01, 1.0, 1, 1, 2.0, 5_000_000),
    )
    return CanonicalMarketTruth(
        "native:solana", now_ms, ProviderStatus.HEALTHY, 100.005, 2, 2,
        divergence_bps, 1.0, 2.0, .5, (), (), contributions,
    )


async def build_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    database: Path | None = None,
    signal: dict[str, Any] | None = None,
    truth: CanonicalMarketTruth | None = None,
    provider_status: str = "healthy",
    include_truth: bool = True,
) -> AnalyzerService:
    original, _ = load_config("config.yaml")
    config = copy.deepcopy(original)
    # This fixture exercises the legacy paper notification path explicitly.
    config['market_forecast']['profit_policy_enabled'] = False
    config["market"]["assets"] = ["SOL"]
    config["storage"]["sqlite_path"] = str(database or tmp_path / "cycle.db")
    config["storage"]["raw_path"] = str(tmp_path / "raw")
    config["storage"]["models_path"] = str(tmp_path / "models")
    config["storage"]["logs_path"] = str(tmp_path / "logs")
    config["alerts"]["confirmation_cycles"] = 1
    config["alerts"]["enabled"] = True
    config["paper"]["cooldown_seconds"] = 0
    service = AnalyzerService(config)
    now_ms = int(time.time() * 1000)
    fake_signal = signal or production_signal()

    async def frames(_rest):
        return {"SOL": {}, "BTC": {}}

    async def analyze(_coin, _frames, _btc_frames, _context, _book, timestamp):
        value = copy.deepcopy(fake_signal)
        value["timestamp"] = timestamp
        return value

    monkeypatch.setattr(service, "_rest", lambda: OfflineCycleREST(now_ms))
    monkeypatch.setattr(service, "_load_all_frames", frames)
    monkeypatch.setattr(service, "_analyze_coin", analyze)
    service.gate = SimpleNamespace(evaluate=lambda value: AlertDecision(
        value["direction"] == "LONG", "TEST_SIGNAL" if value["direction"] == "LONG" else None,
        value["direction"], float(value["probability"]), "fixture production gate",
    ))
    service.telegram = FakeTelegram()
    service.telegram_commands.telegram = service.telegram
    service.feed_status = SimpleNamespace(update=_async_none)
    service.market_data.truth = (
        {"native:solana": truth or healthy_truth(now_ms)} if include_truth else {}
    )
    monkeypatch.setattr(service.market_data, "health", lambda: {
        "hyperliquid": {"status": provider_status, "lag_ms": 1, "consecutive_failures": 0},
        "coinbase": {"status": "healthy", "lag_ms": 1, "consecutive_failures": 0},
    })
    return service


async def _async_none(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_normal_cycle_creates_decision_and_restart_is_idempotent(tmp_path, monkeypatch):
    database = tmp_path / "restart.db"
    first = await build_service(tmp_path, monkeypatch, database=database)
    try:
        signals = await first.analysis_cycle(send_telegram=True)
        assert signals[0]["paper_decision"]["status"] == "RECOMMENDED"
        counts = first.paper.repository.counts()
        assert counts["paper_decisions"] == counts["paper_alerts"] == 1
        assert len(first.paper.portfolio.positions()) == 1
        recommendation = first.paper.latest(1)[0]["recommendation"]
        assert recommendation["model_name"] == "current-production-ensemble"
        assert "phase4" not in recommendation["model_version"].lower()
        assert recommendation["data_lineage"]
        expected_net = .04 - (
            first.paper.config.entry_fee_bps + first.paper.config.exit_fee_bps
            + 2 * recommendation["estimated_slippage_bps"]
        ) / 10_000
        assert recommendation["expected_net_move"] == pytest.approx(expected_net)
        assert len([message for message in first.telegram.messages if "SHADOW" in message]) == 1
        operations = first.health()["operations"]
        assert "provider_lag_ms" in operations and "event_lag_ms" in operations
        assert operations["universe_size"] == operations["deep_watch_size"] == 0
        assert "event_ingestion" in operations and "dropped_events" in operations["event_ingestion"]
        assert "paper_performance" in operations and operations["paper_performance"]["reconciles"]
    finally:
        await first.close()

    second = await build_service(tmp_path, monkeypatch, database=database)
    try:
        await second.analysis_cycle(send_telegram=True)
        after = second.paper.repository.counts()
        assert after["paper_decisions"] == counts["paper_decisions"]
        assert after["paper_alerts"] == counts["paper_alerts"]
        assert len(second.paper.portfolio.positions()) == 1
        assert not [message for message in second.telegram.messages if "SHADOW" in message]
    finally:
        await second.close()


@pytest.mark.asyncio
async def test_wait_cycle_persists_no_trade_without_position(tmp_path, monkeypatch):
    service = await build_service(tmp_path, monkeypatch, signal=production_signal("WAIT"))
    try:
        signals = await service.analysis_cycle(send_telegram=False)
        assert signals[0]["paper_decision"]["status"] == "NO_TRADE"
        row = service.paper.latest(1)[0]
        assert row["recommendation"]["direction"] == "NO_TRADE"
        assert not service.paper.portfolio.positions()
        assert {item["outcome_state"] for item in service.paper.repository.outcomes()} == {"NO_TRADE"}
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(("stale", "provider_status", "divergence", "include_truth", "reason"), [
    (True, "healthy", 12.0, True, "STALE_FEED"),
    (False, "disconnected", 12.0, True, "PROVIDER_DISAGREEMENT"),
    (False, "healthy", 500.0, True, "VENUE_DIVERGENCE"),
    (False, "healthy", 12.0, False, "PROVIDER_DISAGREEMENT"),
])
async def test_live_service_health_prevents_opening(
    tmp_path, monkeypatch, stale, provider_status, divergence, include_truth, reason
):
    now_ms = int(time.time() * 1000)
    truth = healthy_truth(now_ms, divergence_bps=divergence)
    if provider_status == "disconnected":
        truth = CanonicalMarketTruth(
            truth.canonical_asset_id, truth.calculated_at_ms, ProviderStatus.DEGRADED,
            None, 1, 2, divergence, None, None, None, (), (), truth.contributions,
            "primary venue outage",
        )
    service = await build_service(
        tmp_path, monkeypatch, signal=production_signal(stale=stale),
        truth=truth, provider_status=provider_status, include_truth=include_truth,
    )
    try:
        signals = await service.analysis_cycle(send_telegram=False)
        assert signals[0]["paper_decision"]["status"] == "REJECTED"
        assert reason in signals[0]["paper_decision"]["risk_reasons"]
        assert not service.paper.portfolio.positions()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_live_price_gap_is_derived_from_consecutive_service_cycles(tmp_path, monkeypatch):
    service = await build_service(tmp_path, monkeypatch)
    try:
        await service.analysis_cycle(send_telegram=False)
        jumped = production_signal()
        jumped["mark"] = 110.0

        async def analyze(_coin, _frames, _btc_frames, _context, _book, timestamp):
            value = copy.deepcopy(jumped)
            value["timestamp"] = timestamp
            return value

        monkeypatch.setattr(service, "_analyze_coin", analyze)
        signals = await service.analysis_cycle(send_telegram=False)
        assert signals[0]["paper_decision"]["status"] == "RETRACTED"
        assert "EXTREME_GAP" in signals[0]["paper_decision"]["risk_reasons"]
        assert not service.paper.portfolio.positions()
        assert service.paper.current_state(signals[0]["paper_decision"]["decision_id"]) == "RETRACTED"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["liquidity", "retraction"])
async def test_live_state_loss_retracts_once_without_contradictory_outcome(
    tmp_path, monkeypatch, failure
):
    service = await build_service(tmp_path, monkeypatch)
    try:
        first = await service.analysis_cycle(send_telegram=False)
        decision_id = first[0]["paper_decision"]["decision_id"]
        assert decision_id in service.paper.portfolio.positions()
        if failure == "liquidity":
            low = production_signal(depth=100)

            async def analyze(_coin, _frames, _btc_frames, _context, _book, timestamp):
                value = copy.deepcopy(low)
                value["timestamp"] = timestamp
                return value
            monkeypatch.setattr(service, "_analyze_coin", analyze)
        else:
            monkeypatch.setattr(service.events.repository, "latest_events", lambda *_args, **_kwargs: [{
                "event_id": "official-retraction", "event_type": "RUMOR",
                "summary": "Official source retracted the claim", "credibility": .9,
                "confirmation_count": 1, "expected_direction": "positive",
                "contradiction_state": "retracted", "authenticity_risk": False,
                "latest_received_at_ms": int(time.time() * 1000) - 1,
                "raw_event_ids": ["raw-retraction"], "evidence_urls": ["https://solana.com/retraction"],
                "impacted_assets": [{"canonical_asset_id": "native:solana", "chain_id": None,
                                     "contract_address": None}],
            }])
        await service.analysis_cycle(send_telegram=False)
        transitions = service.paper.repository.transitions(decision_id)
        outcomes = service.paper.repository.outcomes()
        assert service.paper.current_state(decision_id) == "RETRACTED"
        assert service.paper.latest(1)[0]["recommendation"]["status"] == "RETRACTED"
        assert decision_id not in service.paper.portfolio.positions()
        assert [row["new_state"] for row in transitions].count("RETRACTED") == 1
        assert [row["outcome_state"] for row in outcomes] == ["RETRACTED"]
        before = service.paper.repository.counts()
        await service.analysis_cycle(send_telegram=False)
        assert service.paper.repository.counts()["paper_state_transitions"] == before["paper_state_transitions"]
        assert service.paper.repository.counts()["paper_outcomes"] == before["paper_outcomes"]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_telegram_commands_show_cycle_created_state(tmp_path, monkeypatch):
    service = await build_service(tmp_path, monkeypatch)
    try:
        await service.analysis_cycle(send_telegram=False)
        commands = TelegramCommandService(service)
        assert "SHADOW" in (await commands.handle("/opportunities"))[0]
        assert "native:solana" in (await commands.handle("/asset native:solana"))[0]
        assert "RECONCILED" in (await commands.handle("/paper"))[0]
        assert any("Shadow paper portfolio" in value for value in await commands.handle("/performance"))
        assert "Source health" in (await commands.handle("/sources"))[0]
        assert "Paper: SHADOW" in (await commands.handle("/health"))[0]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_event_market_paper_alert_position_and_outcome_flow_is_causal(tmp_path, monkeypatch):
    service = await build_service(tmp_path, monkeypatch)
    received_at = int(time.time() * 1000) - 250
    exact_event = {
        "event_id": "official-sol-event", "raw_event_ids": ["rss:official-1"],
        "event_type": "LISTING", "summary": "Official venue listing notice",
        "credibility": .95, "confirmation_count": 2,
        "expected_direction": "positive", "contradiction_state": "none",
        "authenticity_risk": False, "latest_received_at_ms": received_at,
        "evidence_urls": ["https://www.kraken.com/learn/listing"],
        "impacted_assets": [{"canonical_asset_id": "native:solana", "chain_id": None,
                             "contract_address": None}],
    }
    ticker_collision = {
        **exact_event, "event_id": "unrelated-sol-ticker", "raw_event_ids": ["rss:collision"],
        "impacted_assets": [{"canonical_asset_id": "other:sol", "chain_id": "base",
                             "contract_address": "0x1111111111111111111111111111111111111111"}],
    }
    monkeypatch.setattr(
        service.events.repository, "latest_events",
        lambda *_args, **_kwargs: [ticker_collision, exact_event],
    )
    try:
        opened = await service.analysis_cycle(send_telegram=True)
        assert opened[0]["paper_decision"]["status"] == "RECOMMENDED"
        decision_id = opened[0]["paper_decision"]["decision_id"]
        assert decision_id in service.paper.portfolio.positions()
        recommendation = service.paper.latest(1)[0]["recommendation"]
        assert [item["source_id"] for item in recommendation["supporting_evidence"] if item["category"] == "event"] == ["official-sol-event"]
        assert all("collision" not in str(item) for item in recommendation["supporting_evidence"])
        assert any("SHADOW" in message for message in service.telegram.messages)

        degraded_liquidity = production_signal(depth=100)

        async def analyze(_coin, _frames, _btc_frames, _context, _book, timestamp):
            value = copy.deepcopy(degraded_liquidity)
            value["timestamp"] = timestamp
            return value

        monkeypatch.setattr(service, "_analyze_coin", analyze)
        closed = await service.analysis_cycle(send_telegram=True)
        assert service.paper.current_state(decision_id) == "RETRACTED"
        assert decision_id not in service.paper.portfolio.positions()
        assert service.paper.repository.outcomes()[0]["outcome_state"] == "RETRACTED"
        assert any("retracted" in message.lower() for message in service.telegram.messages)
        assert service.paper.performance()["reconciles"]
    finally:
        await service.close()
