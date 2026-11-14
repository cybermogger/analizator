from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.alerts.commands import HELP_TEXT, TelegramCommandService
from src.alerts.feed_status import FeedStatusNotifier
from src.data.repository import MarketRepository
from src.data.storage import SQLiteStorage
from src.hyperliquid.schemas import AssetContext
from src.service.config import load_config
from src.service.provider_guard import validate_provider_config
from src.signals.performance import ForecastPerformance, summarize_outcomes
from src.signals.quality import enrich_signal_quality, shift_overconfidence_to_wait


def base_payload(timestamp=1_700_000_000_000, mark=100.0, probabilities=None):
    probabilities = probabilities or {"LONG": 60.0, "SHORT": 15.0, "WAIT": 25.0}
    components = {
        name: {"probabilities": probabilities, "quality": 1.0}
        for name in ("technical", "structure", "orderbook", "tradeflow", "btc_context", "ml")
    }
    return {
        "coin": "SOL", "timestamp": timestamp, "mark": mark, "primary_horizon": "15m",
        "horizons": {"15m": probabilities}, "direction": "LONG", "probability": probabilities["LONG"],
        "regime": "TREND_UP", "signal_strength": 75.0, "confidence": 75.0,
        "outcome_threshold_pct": 0.5, "model_status": "MODEL_WARMING_UP",
        "details": {"15m": {"components": components}},
    }


def test_quality_snapshot_and_probabilities_are_bounded():
    now = 1_700_000_100_000
    horizons = {
        "15m": {"LONG": 58.0, "SHORT": 17.0, "WAIT": 25.0},
        "1h": {"LONG": 55.0, "SHORT": 18.0, "WAIT": 27.0},
        "4h": {"LONG": 52.0, "SHORT": 20.0, "WAIT": 28.0},
    }
    components = {
        name: {"probabilities": {"LONG": 60.0, "SHORT": 15.0, "WAIT": 25.0}, "quality": 0.8}
        for name in ("technical", "structure", "orderbook", "tradeflow")
    }
    details = {horizon: {"components": components, "forward_return_quantiles": {"p10": -0.01, "p90": 0.015}} for horizon in horizons}
    plans = {
        "LONG": {"expected_zone": (101.2, 102.4), "invalidation": 99.1},
        "SHORT": {"expected_zone": (97.8, 98.8), "invalidation": 100.9},
        "WAIT": {"expected_zone": (99.0, 101.0), "invalidation": None},
    }
    row = {"open_time": now - 60_000, "received_at_ms": now - 250, "atr_14": 1.0, "atr_pct": 0.01, "rolling_std_20": 0.006, "support": 98.5, "resistance": 102.5, "range_expansion": 1.1}
    result = enrich_signal_quality(
        mark=100, timestamp=now, context_timestamp=now - 1_000, source_interval="5m", source_row=row,
        horizons=horizons, primary_horizon="15m", direction="LONG", details=details, plans=plans,
        regime="TREND_UP", orderbook={"available": True, "timestamp": now - 500, "exchange_timestamp_ms": now - 500, "received_at_ms": now - 200},
        tradeflow={"available": True, "aggressive_imbalance": 0.4}, btc_context={"available": True, "momentum": {"15m": 0.01}},
        settings={"horizon_steps": 3, "max_data_age_seconds": 180},
        context_received_at_ms=now - 100,
    )
    assert 0 <= result["confidence"] <= 100
    assert 0 <= result["signal_strength"] <= 100
    assert result["data_freshness"]["status"] == "FRESH"
    assert result["target_range"] == (101.2, 102.4)
    shifted = shift_overconfidence_to_wait(horizons["15m"], 0.8)
    assert abs(sum(shifted.values()) - 100) < 1e-8
    assert shifted["WAIT"] > horizons["15m"]["WAIT"]


def test_outcome_evaluation_and_calibration_buckets(tmp_path: Path):
    storage = SQLiteStorage(tmp_path / "outcomes.db")
    repository = MarketRepository(storage)
    start = 1_700_000_000_000
    payload = base_payload(start)
    signal_id = repository.save_signal(payload)
    due = start + 900_000
    repository.upsert_contexts([AssetContext("SOL", due + 1_000, 101.0, 101.0, 101.0, 0, 1, 1, 0, 100)])
    performance = ForecastPerformance(repository, {"minimum_samples": 2})
    try:
        assert performance.evaluate_due(due + 2_000) == 1
        rows = repository.evaluated_signal_rows("SOL")
        assert rows[0]["signal_id"] == signal_id and rows[0]["actual_label"] == "LONG"
        summary = performance.summary("SOL")
        assert summary["samples"] == 1 and len(summary["calibration_buckets"]) == 5
    finally:
        storage.close()


def test_performance_metrics_and_adaptive_weight_protection():
    rows = []
    for index, label in enumerate(("LONG", "LONG", "SHORT", "WAIT")):
        payload = base_payload(probabilities={"LONG": 60.0, "SHORT": 15.0, "WAIT": 25.0})
        rows.append({"payload_json": payload, "actual_label": label, "forward_return": [0.01, 0.02, -0.01, 0][index]})
    summary = summarize_outcomes(rows)
    assert summary["samples"] == 4
    assert summary["brier"] is not None and len(summary["calibration_buckets"]) == 5

    repository = SimpleNamespace(evaluated_signal_rows=lambda _coin, _limit: rows)
    protected = ForecastPerformance(repository, {"minimum_samples": 10})
    assert protected.component_multipliers("SOL", "15m", "TREND_UP", ["technical", "ml"]) == {"technical": 1.0, "ml": 1.0}
    adaptive = ForecastPerformance(repository, {
        "minimum_samples": 2, "regularization_samples": 100,
        "weight_multiplier_bounds": [0.9, 1.1], "max_weight_adjustment": 0.05,
    }).component_multipliers("SOL", "15m", "TREND_UP", ["technical", "ml"])
    assert all(0.9 <= value <= 1.1 for value in adaptive.values())


class FakeTelegram:
    configured = True
    chat_id = "42"

    def __init__(self):
        self.messages = []

    async def send(self, text):
        self.messages.append(text)
        return str(len(self.messages))


@pytest.mark.asyncio
async def test_feed_warning_is_deduplicated_and_recovery_is_sent(tmp_path: Path):
    storage = SQLiteStorage(tmp_path / "feed.db")
    repository = MarketRepository(storage)
    telegram = FakeTelegram()
    notifier = FeedStatusNotifier(repository, telegram, warning_after_seconds=10)
    try:
        assert await notifier.update(False, "network", 1_000) is None
        assert await notifier.update(False, "network", 12_000) == "FEED_WARNING"
        assert await notifier.update(False, "network", 20_000) is None
        assert await notifier.update(True, None, 21_000) == "FEED_RESTORED"
        assert len(telegram.messages) == 2
    finally:
        storage.close()


def test_configurable_provider_registry_guard():
    config, _ = load_config("config.yaml")
    validate_provider_config(config)
    forbidden = copy.deepcopy(config)
    forbidden["providers"]["hyperliquid"]["rest_url"] = "https://api.binance.com/info"
    with pytest.raises(ValueError):
        validate_provider_config(forbidden)


@pytest.mark.asyncio
async def test_telegram_command_list_and_health():
    assert all(command in HELP_TEXT for command in ("/status", "/sol", "/hype", "/signals", "/performance", "/health", "/help"))
    repository = SimpleNamespace(
        latest_signal=lambda _coin: None,
        recent_signal_alerts=lambda _limit: [],
    )
    analyzer = SimpleNamespace(
        telegram=FakeTelegram(), repository=repository,
        config={"market": {"assets": ["SOL", "HYPE"]}, "service": {"cycle_timeout_seconds": 60}},
        forecast_performance=SimpleNamespace(summary=lambda: {"samples": 0}),
        health=lambda: {"ws_connected": False, "db": {"integrity": "ok", "schema_version": 3}},
    )
    commands = TelegramCommandService(analyzer)
    response = await commands.handle("/health")
    assert "WS: DISCONNECTED" in response[0] and "SQLite: ok" in response[0]
