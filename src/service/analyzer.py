from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import structlog
from src.util.redaction import safe_exception

from src.alerts.alert_gate import AlertGate
from src.alerts.commands import TelegramCommandService
from src.alerts.critical import CriticalErrorNotifier
from src.alerts.feed_status import FeedStatusNotifier
from src.alerts.formatter import format_paper_recommendation, format_signal
from src.alerts.multi_asset import MultiAssetAlertService
from src.alerts.news import NewsAlertService
from src.alerts.new_tokens import NewTokenAlertService
from src.alerts.telegram import TelegramClient
from src.data.aggregation import RawParquetRotator
from src.data.repository import MarketRepository
from src.data.storage import SQLiteStorage
from src.features.btc_context import btc_context_features
from src.features.derivatives import derivatives_features
from src.features.orderbook import book_momentum, orderbook_features
from src.features.regime import detect_regime
from src.features.statistics import historical_analogue_probabilities
from src.features.structure import add_structure_features
from src.features.technical import MODEL_FEATURES, add_technical_features
from src.features.tradeflow import tradeflow_features
from src.hyperliquid.rest import HyperliquidREST
from src.hyperliquid.schemas import BookSnapshot
from src.hyperliquid.websocket import HyperliquidWebSocket, LiveMarketState
from src.models.ensemble import combine_probabilities
from src.models.ml import ModelRegistry
from src.models.rules import (
    ComponentProbability, btc_component, derivatives_component, orderbook_component,
    structure_component, technical_component, tradeflow_component,
)
from src.models.targets import calculate_trade_plan
from src.paper.service import PaperDecisionService
from src.paper.orchestrator import ShadowDecisionOrchestrator
from src.signals.performance import ForecastPerformance
from src.signals.quality import enrich_signal_quality, shift_overconfidence_to_wait

from .health import health_snapshot
from .event_intelligence import EventIntelligenceService
from .market_data import MultiVenueMarketService
from .market_analysis import MultiAssetAnalysisService
from .strategy_shadow import StrategyShadow
from .onchain_intelligence import OnChainIntelligenceService


INTERVAL_MS = {"1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000}


class AnalyzerService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.log = structlog.get_logger("analyzer")
        self.storage = SQLiteStorage(config["storage"]["sqlite_path"])
        self.repository = MarketRepository(self.storage)
        self.market_data = MultiVenueMarketService(config, self.repository)
        self.market_analysis = MultiAssetAnalysisService(
            self.market_data, config.get("market_analysis", {}), self.repository
        )
        self.strategy_shadow = StrategyShadow(self.storage, config.get("strategy_shadow", {}))
        self.market_analysis.strategy_shadow = self.strategy_shadow
        self.events = EventIntelligenceService(config, self.storage)
        self.onchain = OnChainIntelligenceService(config, self.storage, self.events.repository)
        self.paper = PaperDecisionService(self.storage, config.get("paper", {}))
        self.state = LiveMarketState()
        coins = list(dict.fromkeys(config["market"]["assets"] + [config["market"]["context_asset"]]))
        self.raw_writer = RawParquetRotator(config["storage"]["raw_path"], config["storage"]["raw_rotation_minutes"], config["storage"]["raw_retention_days"])
        self.websocket = HyperliquidWebSocket(config["market"]["websocket_url"], coins, config["market"]["timeframes"], self.state, self._on_ws_event)
        model_cfg = config["models"]
        self.models = ModelRegistry(config["storage"]["models_path"], model_cfg["min_balanced_accuracy"], model_cfg["max_brier_score"], model_cfg["min_brier_skill"])
        for coin in config["market"]["assets"]:
            for horizon in model_cfg["horizons"]:
                self.models.load(coin, horizon)
        alert_cfg = config["alerts"]
        self.gate = AlertGate(
            self.repository, alert_cfg["alert_threshold"], alert_cfg["confirmation_cycles"],
            alert_cfg["realert_delta"], alert_cfg["cooldown_minutes"], alert_cfg["strict_alert_mode"],
            minimum_directional_gap=alert_cfg.get("minimum_directional_gap", 10),
            minimum_confidence=alert_cfg.get("minimum_confidence", 55),
            minimum_signal_strength=alert_cfg.get("minimum_signal_strength", 55),
            maximum_wait=alert_cfg.get("maximum_wait", 45),
            minimum_confirmations=alert_cfg.get("minimum_component_confirmations", 2),
            strength_level_step=alert_cfg.get("strength_level_step", 10),
            weakened_delta=alert_cfg.get("weakened_delta", 8),
            invalidated_delta=alert_cfg.get("invalidated_delta", 14),
            target_change_percent=alert_cfg.get("target_change_percent", 0.5),
            fast_event_confidence=alert_cfg.get("fast_event_confidence", 70),
            fast_event_strength=alert_cfg.get("fast_event_strength", 70),
        )
        self.telegram = TelegramClient(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"), config["service"]["request_timeout_seconds"])
        self.multi_asset_alerts = MultiAssetAlertService(
            self.storage, alert_cfg.get("multi_asset", {}), self._send_once,
            excluded_bases=config["market"]["assets"],
        )
        if self.telegram.configured and alert_cfg.get("enabled", False):
            self.market_analysis.on_plans = self.multi_asset_alerts.consider
        self.news_alerts = NewsAlertService(self.storage, alert_cfg.get("news", {}), self._send_once)
        if self.telegram.configured and alert_cfg.get("enabled", False):
            self.events.on_event = self.news_alerts.consider
        self.new_token_alerts = NewTokenAlertService(self.storage,
            alert_cfg.get("new_tokens", {}), self._send_once)
        latest_telegram = self.repository.latest_telegram_event()
        self.last_alert_ms: int | None = int(latest_telegram["timestamp"]) if latest_telegram else None
        self.critical_errors = CriticalErrorNotifier(
            self.repository,
            self.telegram,
            alert_cfg.get("critical_error_cooldown_minutes", 60),
        )
        self.feed_status = FeedStatusNotifier(
            self.repository, self.telegram, alert_cfg.get("feed_warning_after_seconds", 180)
        )
        self.forecast_performance = ForecastPerformance(
            self.repository, config.get("forecast_performance", {})
        )
        self.paper_orchestrator = ShadowDecisionOrchestrator(
            self.paper, self.market_data, self.events, self.onchain, self.forecast_performance
        )
        self.telegram_commands = TelegramCommandService(self)
        self._cycle_lock = asyncio.Lock()
        self._last_health = 0.0
        self._previous_books: dict[str, dict[str, Any]] = {}

    async def _on_ws_event(self, channel: str, data: Any) -> None:
        if channel == "trades":
            self.raw_writer.add_trades(data)
        elif channel == "l2Book":
            self.raw_writer.add_book(data)
        elif channel == "candle":
            self.repository.upsert_candles(data)
        elif channel == "activeAssetCtx" and hasattr(data, "coin"):
            self.repository.upsert_contexts([data])
        self.raw_writer.flush_if_due()

    def _rest(self) -> HyperliquidREST:
        cfg = self.config["service"]
        return HyperliquidREST(self.config["market"]["rest_url"], cfg["request_timeout_seconds"], cfg["max_retries"])

    async def ensure_candle_history(self, rest: HyperliquidREST, coin: str, interval: str, desired: int | None = None) -> pd.DataFrame:
        desired = desired or int(self.config["service"]["history_candles"])
        now = int(time.time() * 1000)
        minimum = int(self.config["service"]["minimum_candles"])
        live_candle = self.state.candles.get((coin, interval))
        if live_candle is not None:
            self.repository.upsert_candles([live_candle])
        existing = self.repository.candles(coin, interval, desired)
        interval_ms = INTERVAL_MS[interval]
        current_interval_open = now - now % interval_ms
        live_received_at = (
            self.state.candle_received_at_ms.get((coin, interval))
            or (live_candle.received_at_ms if live_candle is not None else 0)
        )
        live_interval_valid = bool(
            live_candle is not None
            and current_interval_open - interval_ms - 5_000
            <= live_candle.open_time
            <= current_interval_open + 5_000
        )
        max_receive_age_ms = int(
            float(self.config.get("signal_quality", {}).get("max_data_age_seconds", 180)) * 1000
        )
        if (
            self.state.connected
            and live_candle is not None
            and len(existing) >= minimum
            and live_received_at > 0
            and now - live_received_at <= max_receive_age_ms
            and live_interval_valid
        ):
            return existing
        latest = self.repository.latest_candle_time(coin, interval)
        start = now - desired * interval_ms if latest is None else max(latest - 2 * interval_ms, now - 5 * interval_ms)
        try:
            candles = await rest.candles(coin, interval, start, now)
            self.repository.upsert_candles(candles)
            return self.repository.candles(coin, interval, desired)
        except Exception as exc:
            if len(existing) >= minimum:
                self.log.warning("candles_rest_fallback", coin=coin, interval=interval, cached=len(existing), error=safe_exception(exc))
                return existing
            raise

    async def _load_all_frames(self, rest: HyperliquidREST) -> dict[str, dict[str, pd.DataFrame]]:
        coins = list(dict.fromkeys(self.config["market"]["assets"] + [self.config["market"]["context_asset"]]))
        intervals = self.config["market"]["timeframes"]
        semaphore = asyncio.Semaphore(4)

        async def load(coin: str, interval: str) -> tuple[str, str, pd.DataFrame]:
            async with semaphore:
                raw = await self.ensure_candle_history(rest, coin, interval)
            if len(raw) < 20:
                raise RuntimeError(f"Insufficient candles for {coin}/{interval}: {len(raw)}")
            return coin, interval, add_structure_features(add_technical_features(raw))

        results = await asyncio.gather(*(load(c, i) for c in coins for i in intervals), return_exceptions=True)
        frames: dict[str, dict[str, pd.DataFrame]] = {coin: {} for coin in coins}
        for result in results:
            if isinstance(result, Exception):
                self.log.error("candle_load_failed", error=str(result))
            else:
                coin, interval, frame = result
                frames[coin][interval] = frame
        return frames

    @staticmethod
    def _price_changes(frames: dict[str, pd.DataFrame]) -> dict[str, float | None]:
        changes: dict[str, float | None] = {}
        for interval in ("1m", "5m", "15m", "30m", "1h", "4h"):
            frame = frames.get(interval)
            changes[interval] = float(frame["close"].pct_change().iloc[-1]) if frame is not None and len(frame) > 1 else None
        return changes

    @staticmethod
    def _horizon_weights(base: dict[str, float], horizon: str) -> dict[str, float]:
        focus = {
            "15m": {"1m": 1.3, "5m": 1.5, "15m": 1.35, "1h": 0.7, "4h": 0.5},
            "1h": {"1m": 0.5, "5m": 1.0, "15m": 1.35, "30m": 1.25, "1h": 1.4, "4h": 0.9},
            "4h": {"1m": 0.25, "5m": 0.5, "15m": 0.8, "30m": 1.0, "1h": 1.45, "4h": 1.6},
        }[horizon]
        values = {k: v * focus.get(k, 1.0) for k, v in base.items()}
        total = sum(values.values())
        return {k: v / total for k, v in values.items()}

    async def _analyze_coin(
        self,
        coin: str,
        frames: dict[str, pd.DataFrame],
        btc_frames: dict[str, pd.DataFrame],
        context: Any,
        book: BookSnapshot | None,
        timestamp: int,
    ) -> dict[str, Any]:
        base_tf_weights = self.config["market"]["timeframe_weights"]
        book_values = orderbook_features(book) if book is not None else {"available": False, "quality": 0.0}
        previous_book = self._previous_books.get(coin)
        book_values["book_momentum"] = book_momentum(book_values, previous_book)
        if book_values.get("available") and previous_book:
            current_walls = {(wall["side"], round(wall["price"], 8)) for wall in book_values.get("walls", [])}
            previous_walls = {(wall["side"], round(wall["price"], 8)) for wall in previous_book.get("walls", [])}
            book_values["walls_added"] = [{"side": side, "price": price} for side, price in sorted(current_walls - previous_walls)]
            book_values["walls_removed"] = [{"side": side, "price": price} for side, price in sorted(previous_walls - current_walls)]
        else:
            book_values["walls_added"], book_values["walls_removed"] = [], []
        if book_values.get("available"):
            self._previous_books[coin] = book_values
        if book_values.get("available"):
            self.repository.save_book_aggregate(coin, timestamp, book_values)
        trades = list(self.state.trades.get(coin, []))
        trade_values = tradeflow_features(trades, timestamp)
        if trade_values.get("available"):
            flow = float(trade_values.get("aggressive_imbalance", 0))
            pressure = float(book_values.get("weighted_imbalance", 0))
            trade_values["flow_book_divergence"] = bool(flow * pressure < 0)
        if trade_values.get("available"):
            self.repository.save_trade_aggregate(coin, timestamp, trade_values)
        history = self.repository.context_history(coin, timestamp - 5 * 60 * 60_000)
        derivative_values = derivatives_features(context, history, self._price_changes(frames))
        btc_values = btc_context_features(frames, btc_frames)
        primary_regime_frame = frames.get("1h")
        if primary_regime_frame is None or primary_regime_frame.empty:
            primary_regime_frame = frames.get("15m")
        if primary_regime_frame is None or primary_regime_frame.empty:
            raise RuntimeError(f"No regime frame for {coin}")
        regime = detect_regime(primary_regime_frame.iloc[-1], book_values, derivative_values)

        horizons: dict[str, dict[str, float]] = {}
        horizon_details: dict[str, Any] = {}
        analogue_quantiles: dict[str, dict[str, float]] = {}
        all_reasons: list[str] = []
        for horizon, horizon_cfg in self.config["models"]["horizons"].items():
            weights = self._horizon_weights(base_tf_weights, horizon)
            source = frames.get(horizon_cfg["source_interval"])
            if source is None or source.empty:
                raise RuntimeError(f"Missing source frame {coin}/{horizon_cfg['source_interval']}")
            technical = technical_component(frames, weights)
            structure = structure_component(frames, weights)
            derivatives = derivatives_component(derivative_values)
            orderbook = orderbook_component(book_values)
            tradeflow = tradeflow_component(trade_values)
            analogue = historical_analogue_probabilities(source, int(horizon_cfg["steps"]), MODEL_FEATURES[:12])
            statistical = ComponentProbability(tuple(analogue["probabilities"]), float(analogue.get("quality", 0)), (f"{analogue.get('analogues', 0)} historical analogues",) if analogue.get("available") else ())
            ml = self.models.component(coin, horizon, source)
            btc = btc_component(btc_values)
            components = {"technical": technical, "structure": structure, "derivatives": derivatives, "orderbook": orderbook, "tradeflow": tradeflow, "statistical": statistical, "ml": ml, "btc_context": btc}
            audited_weights = self.models.audited_weights(coin, horizon)
            weights_for_cycle = audited_weights or self.config["ensemble"]["base_weights"]
            adaptive_multipliers = self.forecast_performance.component_multipliers(
                coin, horizon, str(regime), components.keys()
            )
            result = combine_probabilities(
                components, weights_for_cycle, regime, horizon,
                performance=adaptive_multipliers,
                prior=tuple(self.config["ensemble"]["warming_prior"]),
                apply_regime_multipliers=audited_weights is None,
            )
            percentages = np.clip(np.asarray(result.probabilities, dtype=float), 0.0, 1.0)
            percentages = percentages / percentages.sum() * 100.0
            percentages[2] = 100.0 - percentages[0] - percentages[1]
            if not np.all(np.isfinite(percentages)) or np.any(percentages < -1e-9) or np.any(percentages > 100 + 1e-9) or not np.isclose(percentages.sum(), 100.0, atol=1e-8):
                raise ValueError(f"Invalid ensemble probabilities for {coin}/{horizon}: {percentages.tolist()}")
            probability_map = {"LONG": float(percentages[0]), "SHORT": float(percentages[1]), "WAIT": float(percentages[2])}
            active_votes = [
                "LONG" if component.probabilities[0] >= component.probabilities[1] else "SHORT"
                for component in components.values()
                if component.quality >= 0.2 and max(component.probabilities[:2]) > component.probabilities[2]
            ]
            contradiction_scale = 0.88 if "LONG" in active_votes and "SHORT" in active_votes else 1.0
            calibration_scale = self.forecast_performance.runtime_confidence_scale(coin, horizon)
            probability_map = shift_overconfidence_to_wait(
                probability_map, contradiction_scale * calibration_scale
            )
            horizons[horizon] = probability_map
            analogue_quantiles[horizon] = dict(analogue.get("forward_return_quantiles", {}))
            horizon_details[horizon] = {
                "weights": result.weights, "adaptive_multipliers": adaptive_multipliers,
                "regime": str(regime), "ml_ready": ml.quality > 0,
                "confidence_scale": calibration_scale, "contradiction_scale": contradiction_scale,
                "forward_return_quantiles": analogue_quantiles[horizon],
                "components": {
                    name: {
                        "probabilities": {
                            "LONG": component.probabilities[0] * 100,
                            "SHORT": component.probabilities[1] * 100,
                            "WAIT": component.probabilities[2] * 100,
                        },
                        "quality": component.quality,
                    }
                    for name, component in components.items()
                },
            }
            all_reasons.extend(result.reasons)

        def edge(item: tuple[str, dict[str, float]]) -> float:
            _, p = item
            return max(p["LONG"], p["SHORT"]) - 0.35 * p["WAIT"]
        primary_horizon, primary_probs = max(horizons.items(), key=edge)
        if primary_probs["WAIT"] >= max(primary_probs["LONG"], primary_probs["SHORT"]):
            direction = "WAIT"
            probability = primary_probs["WAIT"]
        else:
            direction = "LONG" if primary_probs["LONG"] >= primary_probs["SHORT"] else "SHORT"
            probability = primary_probs[direction]
        source_interval = self.config["models"]["horizons"][primary_horizon]["source_interval"]
        plan_row = frames[source_interval].iloc[-1]
        quantiles = analogue_quantiles.get(primary_horizon, {})
        plans = {
            "LONG": calculate_trade_plan(context.mark, "LONG", plan_row, max(0.0, float(quantiles.get("p75", 0)))),
            "SHORT": calculate_trade_plan(context.mark, "SHORT", plan_row, min(0.0, float(quantiles.get("p25", 0)))),
            "WAIT": calculate_trade_plan(context.mark, "WAIT", plan_row),
        }
        plan = plans[direction]
        model_status = "MODEL_READY" if horizon_details[primary_horizon]["ml_ready"] else "MODEL_WARMING_UP"
        artifact = self.models.artifacts.get((coin, primary_horizon))
        production_model_version = (
            f"production-ensemble-v1+accepted-ml-{artifact.model_version}-{artifact.trained_at}"
            if artifact is not None else "production-ensemble-v1-no-accepted-ml"
        )
        quality_settings = dict(self.config.get("signal_quality", {}))
        quality_settings["horizon_steps"] = int(self.config["models"]["horizons"][primary_horizon]["steps"])
        quality = enrich_signal_quality(
            mark=context.mark, timestamp=timestamp, context_timestamp=context.timestamp,
            source_interval=source_interval, source_row=plan_row, horizons=horizons,
            primary_horizon=primary_horizon, direction=direction, details=horizon_details,
            plans=plans, regime=str(regime), orderbook=book_values, tradeflow=trade_values,
            btc_context=btc_values, settings=quality_settings,
            context_received_at_ms=context.received_at_ms,
            context_exchange_timestamp_ms=context.exchange_timestamp_ms,
            freshness_checked_at_ms=int(time.time() * 1000),
            confidence_scale=horizon_details[primary_horizon]["confidence_scale"],
        )
        signal = {
            "coin": coin, "timestamp": timestamp, "mark": context.mark, "horizons": horizons,
            "primary_horizon": primary_horizon, "direction": direction, "probability": probability,
            "plan": plan, "plans": plans, "regime": str(regime), "reasons": list(dict.fromkeys(all_reasons))[:6],
            "derivatives": derivative_values, "orderbook": book_values, "tradeflow": trade_values,
            "btc_context": btc_values, "model_status": model_status, "details": horizon_details,
            "production_model_version": production_model_version,
            "decision_source": {
                "interval": source_interval,
                "open_time_ms": int(plan_row.get("open_time", timestamp)),
                "received_at_ms": int(plan_row.get("received_at_ms", 0) or 0),
            },
            "source_lineage": [
                f"hyperliquid:context:{coin}:{int(context.exchange_timestamp_ms or context.timestamp)}:{int(context.received_at_ms)}",
                *(
                    [f"hyperliquid:l2Book:{coin}:{int(book.exchange_timestamp_ms)}:{int(book.received_at_ms)}"]
                    if book is not None else []
                ),
                f"hyperliquid:candle:{coin}:{source_interval}:{int(plan_row.get('open_time', timestamp))}:{int(plan_row.get('received_at_ms', 0) or 0)}",
            ],
        }
        signal.update(quality)
        return signal

    async def analysis_cycle(
        self, send_telegram: bool = True, evaluate_alerts: bool = True
    ) -> list[dict[str, Any]]:
        async with self._cycle_lock:
            return await self._analysis_cycle(send_telegram, evaluate_alerts)

    async def _analysis_cycle(
        self, send_telegram: bool = True, evaluate_alerts: bool = True
    ) -> list[dict[str, Any]]:
        timestamp = int(time.time() * 1000)
        async with self._rest() as rest:
            required = self.config["market"]["assets"] + [self.config["market"]["context_asset"]]
            frame_result, context_result = await asyncio.gather(self._load_all_frames(rest), rest.meta_and_asset_contexts(), return_exceptions=True)
            if isinstance(frame_result, Exception):
                raise frame_result
            frames = frame_result
            if isinstance(context_result, Exception):
                self.log.warning("contexts_rest_fallback", error=str(context_result))
                contexts = {}
            else:
                _, contexts = context_result
                self.repository.upsert_contexts(contexts[coin] for coin in required if coin in contexts)
            for coin in required:
                if coin not in contexts:
                    fallback = self.state.contexts.get(coin) or self.repository.latest_context(coin)
                    if fallback is not None:
                        contexts[coin] = fallback
            missing = [coin for coin in required if coin not in contexts]
            if missing:
                raise RuntimeError(f"Assets missing from Hyperliquid meta: {missing}")
            books = await asyncio.gather(*(rest.l2_book(coin) for coin in self.config["market"]["assets"]), return_exceptions=True)

        signals: list[dict[str, Any]] = []
        asset_errors: list[tuple[str, BaseException]] = []
        btc_frames = frames[self.config["market"]["context_asset"]]
        for coin, book_result in zip(self.config["market"]["assets"], books, strict=True):
            try:
                if isinstance(book_result, Exception):
                    book = self.state.books.get(coin)
                else:
                    book = book_result
                signal = await self._analyze_coin(coin, frames.get(coin, {}), btc_frames, contexts[coin], book, timestamp)
                previous_alert = self.repository.last_signal_alert(coin)
                decision = self.gate.evaluate(signal) if evaluate_alerts else None
                signal["event"] = decision.event if decision else None
                signal["previous_direction"] = (
                    previous_alert.get("payload", {}).get("direction") if previous_alert else None
                )
                signal["alert_decision"] = {
                    "eligible": bool(decision and decision.send),
                    "event": decision.event if decision else None,
                    "reason": decision.reason if decision else "alert evaluation disabled for manual command",
                    "telegram_requested": send_telegram,
                }
                paper_decision = self.paper_orchestrator.process(signal, timestamp)
                signal["paper_decision"] = {
                    "decision_id": paper_decision.recommendation.decision_id,
                    "status": paper_decision.recommendation.status,
                    "direction": paper_decision.recommendation.direction,
                    "risk_reasons": list(paper_decision.risk.reasons),
                    "terminal_state": paper_decision.terminal_state,
                }
                signal["paper_positions_closed"] = list(paper_decision.closed_positions)
                self.repository.save_signal(signal)
                signals.append(signal)
                self.log.info(
                    "asset_analyzed", coin=coin, direction=signal["direction"],
                    probability=round(signal["probability"], 2), confidence=round(signal["confidence"], 1),
                    strength=round(signal["signal_strength"], 1), regime=signal["regime"],
                    signal_event=signal["event"], model=signal["model_status"],
                )
                if decision and decision.send and send_telegram and self.config["alerts"]["enabled"]:
                    message = format_signal(signal)
                    delivery_key = self._delivery_key("signal", (
                        coin, str(decision.event), signal["direction"],
                        round(float(signal["probability"]), 3),
                        round(float(signal["signal_strength"]), 3),
                        signal.get("plan", {}),
                    ))
                    delivered, message_id = await self._send_once(
                        delivery_key, message, timestamp
                    )
                    if delivered and message_id is not None:
                        self.repository.save_signal_alert(signal, str(decision.event), message, message_id)
                        self.last_alert_ms = timestamp
                    elif not delivered:
                        self.log.warning("telegram_not_sent", coin=coin, configured=self.telegram.configured)
                paper_alert = bool(
                    paper_decision.alert_needed
                    and (
                        paper_decision.recommendation.status == "RECOMMENDED"
                        or paper_decision.terminal_state == "RETRACTED"
                    )
                )
                if paper_alert and send_telegram and self.config["alerts"]["enabled"]:
                    message = format_paper_recommendation(paper_decision.recommendation.as_dict())
                    delivery_key = self._delivery_key("paper", (
                        paper_decision.recommendation.decision_id,
                        self.paper.material_hash(paper_decision.recommendation),
                    ))
                    delivered, message_id = await self._send_once(
                        delivery_key, message, timestamp
                    )
                    if delivered and message_id is not None and self.paper.record_alert(
                        paper_decision.recommendation, message, timestamp, message_id
                    ):
                        self.last_alert_ms = timestamp
                if send_telegram and self.config["alerts"]["enabled"]:
                    for exit_event in paper_decision.closed_positions:
                        message = (
                            "📒 Shadow paper position closed\n"
                            f"Decision: {exit_event['decision_id']}\n"
                            f"Reason: {exit_event['reason']}\n"
                            f"Observed mark: {float(exit_event['price']):.8g}\n"
                            "Read-only shadow accounting; no exchange order was sent."
                        )
                        delivery_key = self._delivery_key("paper-exit", (
                            str(exit_event["decision_id"]), str(exit_event["reason"]),
                            round(float(exit_event["price"]), 10),
                        ))
                        delivered, message_id = await self._send_once(
                            delivery_key, message, timestamp
                        )
                        if delivered and message_id is not None and self.paper.record_exit_alert(
                            str(exit_event["decision_id"]), str(exit_event["reason"]),
                            float(exit_event["price"]), message, timestamp, message_id,
                        ):
                            self.last_alert_ms = timestamp
            except Exception as exc:
                self.log.error("asset_analysis_failed", coin=coin, error=safe_exception(exc))
                asset_errors.append((coin, exc))
        if asset_errors:
            await asyncio.gather(
                *(self.notify_critical(f"analysis:{coin}", error) for coin, error in asset_errors),
                return_exceptions=True,
            )
        self.forecast_performance.evaluate_due(timestamp)
        feed_healthy = len(signals) == len(self.config["market"]["assets"]) and all(
            signal.get("data_freshness", {}).get("status") == "FRESH" for signal in signals
        )
        if send_telegram:
            await self.feed_status.update(feed_healthy, None if feed_healthy else "missing/stale asset analysis", timestamp)
        return signals

    async def notify_critical(self, scope: str, error: BaseException) -> bool:
        return await self.critical_errors.notify(scope, error)

    @staticmethod
    def _delivery_key(kind: str, identity: tuple[Any, ...]) -> str:
        encoded = json.dumps((kind, identity), sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _send_once(
        self, delivery_key: str, message: str, timestamp_ms: int
    ) -> tuple[bool, str | None]:
        """At-most-once Telegram delivery across crashes; ambiguous sends are surfaced."""
        if not self.telegram.configured:
            return False, None
        if not self.storage.claim_telegram_delivery(delivery_key, timestamp_ms, message):
            return False, None
        try:
            message_id = await self.telegram.send(message)
        except Exception as exc:
            self.storage.record_telegram_delivery(
                delivery_key, int(time.time() * 1000), "UNKNOWN", error_class=type(exc).__name__
            )
            self.log.error(
                "telegram_delivery_unknown", delivery_key=delivery_key,
                error=safe_exception(exc),
            )
            return False, None
        if message_id is None:
            self.storage.record_telegram_delivery(
                delivery_key, int(time.time() * 1000), "UNKNOWN", error_class="no_message_id"
            )
            return False, None
        self.storage.record_telegram_delivery(
            delivery_key, int(time.time() * 1000), "SENT", str(message_id)
        )
        return True, str(message_id)

    async def notify_feed_status(self, healthy: bool, details: str | None = None) -> str | None:
        return await self.feed_status.update(healthy, details)

    async def live_smoke_test(self) -> dict[str, Any]:
        now = int(time.time() * 1000)
        async with self._rest() as rest:
            (meta, contexts), mids = await asyncio.gather(rest.meta_and_asset_contexts(), rest.all_mids())
            result: dict[str, Any] = {"universe_size": len(meta.get("universe", [])), "assets": {}, "all_mids_count": len(mids)}
            for coin in ["SOL", "HYPE", "BTC"]:
                candles, book = await asyncio.gather(rest.candles(coin, "1m", now - 5 * 60_000, now), rest.l2_book(coin))
                result["assets"][coin] = {"mark": contexts[coin].mark, "candles": len(candles), "bid_levels": len(book.bids), "ask_levels": len(book.asks), "mid": mids.get(coin)}
            ws_task = asyncio.create_task(self.websocket.run_forever())
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline and not (self.state.connected and self.state.last_update_ms and self.state.books.get("SOL")):
                    await asyncio.sleep(0.1)
                result["websocket"] = {
                    "connected": self.state.connected,
                    "last_update_ms": self.state.last_update_ms,
                    "sol_book_received": "SOL" in self.state.books,
                    "subscriptions": len(self.websocket.subscriptions()),
                }
                sol_book = self.state.books.get("SOL")
                if sol_book is not None:
                    checked_at_ms = int(time.time() * 1000)
                    received_at_ms = self.state.book_received_at_ms.get("SOL", sol_book.received_at_ms)
                    result["websocket"].update({
                        "sol_book_exchange_timestamp_ms": sol_book.exchange_timestamp_ms,
                        "sol_book_received_at_ms": received_at_ms,
                        "sol_book_receive_age_seconds": max(0.0, (checked_at_ms - received_at_ms) / 1000),
                        "sol_book_exchange_clock_skew_seconds": (
                            received_at_ms - sol_book.exchange_timestamp_ms
                        ) / 1000,
                    })
            finally:
                await self.websocket.stop()
                ws_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ws_task
            result["multi_venue"] = await self.market_data.refresh_once(include_deep=False)
            try:
                events = await asyncio.wait_for(self.events.poll_once(), timeout=40.0)
                event_health = self.events.health()
                result["event_sources"] = {
                    "status": "completed",
                    "events_ingested": len(events),
                    "sources": {
                        source_id: value["status"] for source_id, value in event_health.items()
                    },
                }
            except TimeoutError:
                result["event_sources"] = {
                    "status": "TIMEOUT", "limit_seconds": 40,
                    "reason": "configured public-source smoke limit reached",
                }
            return result

    def health(self) -> dict[str, Any]:
        result = health_snapshot(
            self.storage, self.state, self.models, self.config["market"]["assets"], self.last_alert_ms,
            provider_health=self.market_data.health(),
            market_truth={key: value.as_dict() for key, value in self.market_data.truth.items()},
            event_health=self.events.health(),
            latest_events=self.events.latest(3),
            onchain_health=self.onchain.health(),
            latest_onchain_candidates=self.onchain.latest(3),
        )
        result["paper"] = self.paper.health()
        result["market_analysis"] = self.market_analysis.health()
        result["operations"] = self.operational_metrics()
        return result

    def operational_metrics(self) -> dict[str, Any]:
        providers = self.market_data.health()
        truth = {key: value.as_dict() for key, value in self.market_data.truth.items()}
        events = self.events.health()
        event_runtime = self.events.runtime_metrics()
        onchain = self.onchain.health()
        calibration: dict[str, Any] = {}
        degraded: list[str] = []
        disabled: list[str] = []
        for provider, value in providers.items():
            status = str(value.get("status", "unknown"))
            if status == "disabled":
                disabled.append(f"market:{provider}")
            elif status != "healthy":
                degraded.append(f"market:{provider}:{status}")
        for asset, value in truth.items():
            if value.get("status") != "healthy":
                degraded.append(f"market_truth:{asset}:{value.get('status', 'unknown')}")
        for source, value in events.items():
            status = str(value.get("status", "unknown"))
            if not value.get("enabled") or status in {"disabled", "disabled_missing_credentials"}:
                disabled.append(f"event:{source}")
            elif status != "healthy":
                degraded.append(f"event:{source}:{status}")
        for provider, value in onchain.items():
            status = str(value.get("status", "disabled"))
            if not value.get("enabled", False) or status == "disabled":
                disabled.append(f"onchain:{provider}")
            elif status != "healthy":
                degraded.append(f"onchain:{provider}:{status}")
        delivery = self.storage.telegram_delivery_status()
        if delivery["unknown"]:
            degraded.append(f"telegram_delivery:{delivery['unknown']}_unknown_after_crash")
        for coin in self.config["market"]["assets"]:
            summary = self.forecast_performance.summary(coin, limit=1000)
            calibration[coin] = {
                "samples": summary["samples"], "brier": summary["brier"], "ece": summary["ece"],
            }
        decisions = self.paper.latest(30)
        circuit_breakers = sorted({
            str(reason)
            for row in decisions
            for reason in row["recommendation"].get("risk_reasons", [])
        })
        return {
            "provider_lag_ms": {
                provider: value.get("lag_ms") for provider, value in providers.items()
            },
            "event_lag_ms": event_runtime["event_lag_ms"],
            "source_coverage": {
                "market": {
                    "healthy": sum(value.get("status") == "healthy" for value in providers.values()),
                    "enabled": sum(value.get("status") != "disabled" for value in providers.values()),
                },
                "events": {
                    "healthy": event_runtime["healthy_sources"],
                    "enabled": event_runtime["enabled_sources"],
                    "ratio": event_runtime["source_coverage"],
                },
            },
            "universe_size": len(self.market_data.membership),
            "deep_watch_size": sum(item.deep_watchlist for item in self.market_data.membership),
            "multi_asset_analysis": self.market_analysis.health(),
            "market_requests": self.market_data.request_metrics(),
            "event_ingestion": event_runtime,
            "model_versions": {
                f"{coin}/{horizon}": (
                    f"{artifact.model_version}@{artifact.trained_at}"
                ) for (coin, horizon), artifact in self.models.artifacts.items()
            },
            "calibration": calibration,
            "paper_performance": self.paper.performance(),
            "telegram_delivery": self.storage.telegram_delivery_status(),
            "circuit_breakers": circuit_breakers,
            "degraded_reasons": sorted(set(degraded)),
            "disabled_sources": sorted(set(disabled)),
        }

    async def close(self) -> None:
        await self.market_data.stop()
        await self.events.stop()
        await self.onchain.stop()
        self.raw_writer.flush_if_due(force=True)
        self.storage.close()
