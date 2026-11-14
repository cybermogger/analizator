from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import asdict, dataclass
import hashlib
import json
import math
import statistics
import time
from typing import Any, Mapping, Sequence

from src.providers.base import DerivativesDataProvider, ProviderCapabilityError
from src.providers.types import (
    FundingSnapshot, Instrument, MarketType, NormalizedCandle, NormalizedL2,
    OpenInterestSnapshot,
)


INTERVAL_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}


@dataclass(frozen=True, slots=True)
class CandleQuality:
    status: str
    reason: str | None
    count: int
    gaps: int
    open_or_future_dropped: int
    received_at_ms: int | None
    candle_event_age_seconds: float | None = None


class MultiAssetAnalysisService:
    """Bounded descriptive analysis for exact venue instruments; never a trading model."""

    def __init__(self, market_data: Any, settings: Mapping[str, Any] | None = None, repository: Any | None = None):
        self.market_data = market_data
        self.repository = repository or getattr(market_data, "repository", None)
        self.settings = dict(settings or {})
        self.intervals = tuple(
            item for item in self.settings.get("intervals", ("5m", "15m", "1h", "4h"))
            if item in INTERVAL_MS
        )
        if not self.intervals:
            raise ValueError("market_analysis requires a supported candle interval")
        self.poll_seconds = max(5.0, min(900.0, float(self.settings.get("poll_interval_seconds", 15))))
        self.rotation_size = max(1, min(30, int(self.settings.get("instruments_per_cycle", 5))))
        self.history_limit = max(30, min(300, int(self.settings.get("history_candles", 80))))
        self.minimum_history = max(20, min(self.history_limit, int(self.settings.get("minimum_history", 25))))
        self.cache_limit = max(self.history_limit, min(100_000, int(self.settings.get("maximum_cached_candles", 10_000))))
        self.candle_stale_ms = max(60_000, int(float(self.settings.get("candle_stale_seconds", 1800)) * 1000))
        self.book_stale_ms = max(5_000, int(float(self.settings.get("book_stale_seconds", 60)) * 1000))
        self.derivatives_ttl_ms = max(30_000, int(float(self.settings.get("derivatives_ttl_seconds", 60)) * 1000))
        self.max_requests_per_cycle = max(4, min(100, int(self.settings.get("maximum_requests_per_cycle", 30))))
        self.observation_cadence_ms = max(60_000, min(900_000, int(float(self.settings.get("observation_cadence_seconds", 300)) * 1000)))
        self.outcome_max_latency_ms = max(5_000, min(300_000, int(float(self.settings.get("outcome_max_latency_seconds", 60)) * 1000)))
        self.outcome_requests_per_cycle = max(1, min(20, int(self.settings.get("outcome_requests_per_cycle", 8))))
        self.shadow_monitor_requests_per_cycle = max(0, min(4, int(self.settings.get("shadow_monitor_requests_per_cycle", 2))))
        self.costs = {
            "fee_bps_per_side": max(0.0, float(self.settings.get("fee_bps_per_side", 4.0))),
            "slippage_bps_per_side": max(0.0, float(self.settings.get("slippage_bps_per_side", 2.0))),
            "spread_method": "half_entry_plus_half_exit_quoted_spread_once",
            "funding": "excluded_unavailable_for_spot_and_not_forecast",
        }
        self.cost_policy_version = str(self.settings.get("cost_policy_version", "observational-cost-v1"))
        self.feature_version = str(self.settings.get("feature_version", "descriptive-v1"))
        hash_settings = {key: self.settings.get(key) for key in (
            "intervals", "minimum_history", "candle_stale_seconds", "book_stale_seconds",
            "fee_bps_per_side", "slippage_bps_per_side", "observation_cadence_seconds",
            "outcome_max_latency_seconds"
        )} | {"cost_policy_version": self.cost_policy_version}
        self.feature_config_hash = hashlib.sha256(json.dumps(hash_settings, sort_keys=True).encode()).hexdigest()
        self._candles: OrderedDict[tuple[str, str, int], NormalizedCandle] = OrderedDict()
        self._fetch_status: dict[tuple[str, str], dict[str, Any]] = {}
        self._funding: dict[str, tuple[FundingSnapshot, int]] = {}
        self._open_interest: dict[str, tuple[OpenInterestSnapshot, int]] = {}
        self._derivative_attempts: dict[tuple[str, str], int] = {}
        self._instrument_errors: dict[str, str] = {}
        self._cards: dict[str, dict[str, Any]] = {}
        self._cursor = 0
        self._shadow_open_cursor = 0
        self._shadow_pending_cursor = 0
        self._stop = asyncio.Event()
        self._cycles = 0
        self._requests = 0
        self._cycle_request_limit = self.max_requests_per_cycle
        self._failures = 0
        self._last_cycle_at_ms: int | None = None
        self._last_cycle_duration_seconds = 0.0
        self._last_cycle_error: str | None = None
        self._observations_saved = 0
        self._outcomes_completed = 0
        self._outcomes_unavailable = 0

    @staticmethod
    def _now_ms() -> int:
        return int(time.time() * 1000)

    @staticmethod
    def _metadata_issue(metadata: Any, instrument: Instrument, now_ms: int, max_age_ms: int, *, check_exchange_age: bool = True) -> str | None:
        if (metadata.instrument_id != instrument.instrument_id or metadata.provider != instrument.provider
                or metadata.venue != instrument.venue):
            return "source_identity_mismatch"
        exchange_is_known = metadata.exchange_time_kind in {"EXCHANGE_EVENT", "DERIVED_INTERVAL_CLOSE"}
        if metadata.received_at_ms > now_ms or (exchange_is_known and metadata.exchange_timestamp_ms > now_ms):
            return "future_source_timestamp"
        if metadata.received_at_ms <= 0 or (exchange_is_known and metadata.exchange_timestamp_ms <= 0):
            return "invalid_source_timestamp"
        if now_ms - metadata.received_at_ms > max_age_ms:
            return "received_snapshot_stale"
        if check_exchange_age and exchange_is_known and now_ms - metadata.exchange_timestamp_ms > max_age_ms:
            return "exchange_event_stale"
        return None

    def _prune_universe(self, instruments: Sequence[Instrument]) -> None:
        active_ids = {item.instrument_id for item in instruments}
        self._candles = OrderedDict((k, v) for k, v in self._candles.items() if k[0] in active_ids)
        for mapping in (self._cards, self._funding, self._open_interest, self._instrument_errors):
            for instrument_id in tuple(mapping):
                if instrument_id not in active_ids:
                    del mapping[instrument_id]
        for key in tuple(self._fetch_status):
            if key[0] not in active_ids:
                del self._fetch_status[key]
        for key in tuple(self._derivative_attempts):
            if key[0] not in active_ids:
                del self._derivative_attempts[key]

    def _eligible_members(self) -> list[Instrument]:
        items: list[Instrument] = []
        seen: set[str] = set()
        for row in self.market_data.membership:
            if not row.deep_watchlist or row.instrument_id in seen:
                continue
            try:
                item = self.market_data.instruments.get(row.instrument_id)
            except KeyError:
                # Registry and universe membership can change between refreshes.
                # A vanished market must not prevent healthy neighbors from
                # receiving their scheduled analysis.
                continue
            if item.active:
                seen.add(item.instrument_id)
                items.append(item)
                if len(items) == 30:
                    break
        return items

    def _provider_timeout(self, instrument: Instrument) -> float:
        settings = self.market_data.config.get("providers", {}).get(instrument.provider, {})
        return max(0.1, min(10.0, float(settings.get("request_timeout_seconds", 8))))

    async def _request(self, instrument: Instrument, operation: Any) -> Any:
        if self._requests >= self._cycle_request_limit:
            raise ProviderCapabilityError("market analysis request budget exhausted")
        provider = self.market_data.providers.get(instrument.provider)
        timeout = self._provider_timeout(instrument)
        self._requests += 1
        return await asyncio.wait_for(
            self.market_data._provider_request(
                instrument.provider, lambda: asyncio.wait_for(operation(provider), timeout=timeout)
            ), timeout=timeout + 1.0,
        )

    def _insert_candles(self, instrument: Instrument, interval: str, candles: Sequence[NormalizedCandle], now_ms: int) -> tuple[int, int, int]:
        dropped = accepted = 0
        duration = INTERVAL_MS[interval]
        for candle in candles:
            if candle.interval != interval or candle.metadata.instrument_id != instrument.instrument_id:
                dropped += 1
                continue
            if self._metadata_issue(candle.metadata, instrument, now_ms, self.candle_stale_ms, check_exchange_age=False):
                dropped += 1
                continue
            if candle.close_time_ms != candle.open_time_ms + duration - 1 or candle.metadata.exchange_timestamp_ms != candle.close_time_ms:
                dropped += 1
                continue
            # Exchange-open bars and future bars are never used in closed-candle analysis.
            if not candle.closed or candle.close_time_ms >= now_ms or candle.open_time_ms > now_ms:
                dropped += 1
                continue
            if candle.open_time_ms % duration != 0:
                dropped += 1
                continue
            key = (instrument.instrument_id, interval, candle.open_time_ms)
            previous = self._candles.get(key)
            if previous is None or candle.metadata.received_at_ms >= previous.metadata.received_at_ms:
                self._candles[key] = candle
                self._candles.move_to_end(key)
                accepted += 1
        while len(self._candles) > self.cache_limit:
            self._candles.popitem(last=False)
        return accepted, dropped, duration

    def _interval_metrics(self, instrument: Instrument, interval: str, now_ms: int) -> tuple[dict[str, Any], CandleQuality]:
        rows = sorted(
            (value for (iid, tf, _), value in self._candles.items() if iid == instrument.instrument_id and tf == interval),
            key=lambda item: item.open_time_ms,
        )
        if not rows:
            state = self._fetch_status.get((instrument.instrument_id, interval), {})
            fetch_state = state.get("status")
            status = ("DEGRADED" if fetch_state == "DEGRADED" else "NOT_AVAILABLE" if fetch_state == "NOT_AVAILABLE"
                      else "STALE" if fetch_state == "STALE" else "UNAVAILABLE")
            quality = CandleQuality(status, state.get("reason", "no_closed_candles"), 0, 0, int(state.get("dropped", 0)), state.get("received_at_ms"))
            return {}, quality
        # Keep only the trailing contiguous run so no return/volatility spans a missing bar.
        contiguous = [rows[-1]]
        duration = INTERVAL_MS[interval]
        gaps = sum(
            right.open_time_ms - left.open_time_ms != duration
            for left, right in zip(rows, rows[1:])
        )
        for candle in reversed(rows[:-1]):
            if contiguous[0].open_time_ms - candle.open_time_ms != duration:
                break
            contiguous.insert(0, candle)
        latest_receive = max(row.metadata.received_at_ms for row in contiguous)
        receive_age = max(0, now_ms - latest_receive)
        latest_event_age = max(0, now_ms - (contiguous[-1].open_time_ms + duration))
        latest_expected_open = (now_ms // duration) * duration - duration
        state = self._fetch_status.get((instrument.instrument_id, interval), {})
        dropped = int(state.get("dropped", 0))
        if receive_age > self.candle_stale_ms:
            return {}, CandleQuality("STALE", "candle_snapshot_stale", len(contiguous), gaps, dropped, latest_receive, latest_event_age / 1000)
        if contiguous[-1].open_time_ms < latest_expected_open:
            return {}, CandleQuality("STALE", "latest_closed_candle_interval_is_old", len(contiguous), gaps, dropped, latest_receive, latest_event_age / 1000)
        if len(contiguous) < self.minimum_history:
            return {}, CandleQuality("INSUFFICIENT_HISTORY", "insufficient_contiguous_closed_candles", len(contiguous), gaps, dropped, latest_receive, latest_event_age / 1000)
        closes = [item.close for item in contiguous]
        volumes = [item.volume for item in contiguous]
        returns = [math.log(right / left) for left, right in zip(closes, closes[1:])]
        baseline_volume = statistics.median(volumes[-21:-1]) if len(volumes) >= 21 else 0.0
        if baseline_volume <= 0:
            relative_volume = None
        else:
            relative_volume = volumes[-1] / baseline_volume
        momentum = closes[-1] / closes[-5] - 1.0 if len(closes) >= 5 else None
        trend_return = closes[-1] / closes[-20] - 1.0
        trend = "UP" if trend_return > 0 else "DOWN" if trend_return < 0 else "FLAT"
        volatility = statistics.stdev(returns[-20:]) if len(returns) >= 2 else None
        metrics = {
            "last_close": closes[-1], "momentum_4_bar_pct": momentum * 100 if momentum is not None else None,
            "trend_20_bar_pct": trend_return * 100, "trend": trend,
            "realized_return_volatility_pct_per_bar": volatility * 100 if volatility is not None else None,
            "relative_volume": relative_volume, "closed_candle_count": len(contiguous),
            "candle_receive_age_seconds": receive_age / 1000,
            "candle_event_age_seconds": latest_event_age / 1000,
            "source_provider": contiguous[-1].metadata.provider,
            "source_venue": contiguous[-1].metadata.venue,
            "latest_exchange_time_ms": contiguous[-1].metadata.exchange_timestamp_ms,
        }
        return metrics, CandleQuality("FRESH" if not gaps else "FRESH_WITH_GAPS", "gap_detected_before_contiguous_window" if gaps else None, len(contiguous), gaps, dropped, latest_receive, latest_event_age / 1000)

    def _book_metrics(self, instrument: Instrument, now_ms: int) -> dict[str, Any]:
        book: NormalizedL2 | None = getattr(self.market_data, "latest_l2", {}).get(instrument.instrument_id)
        bbo = self.market_data.truth_engine.latest_bbo(instrument.instrument_id)
        if book is None:
            return {"status": "UNAVAILABLE", "reason": "no_l2_snapshot"}
        age = max(0, now_ms - book.metadata.received_at_ms)
        issue = self._metadata_issue(book.metadata, instrument, now_ms, self.book_stale_ms)
        if age > self.book_stale_ms or issue:
            return {"status": "STALE", "reason": issue or "l2_snapshot_stale", "receive_age_seconds": age / 1000}
        mid = bbo.mid if bbo is not None and now_ms - bbo.metadata.received_at_ms <= self.book_stale_ms else (
            (book.bids[0].price + book.asks[0].price) / 2 if book.bids and book.asks else None
        )
        if not book.bids or not book.asks or mid is None:
            return {"status": "UNAVAILABLE", "reason": "incomplete_order_book", "receive_age_seconds": age / 1000}
        bid_quote = sum(level.price * level.size for level in book.bids[:5])
        ask_quote = sum(level.price * level.size for level in book.asks[:5])
        top_bid, top_ask = book.bids[0].price, book.asks[0].price
        spread_bps = (top_ask - top_bid) / ((top_ask + top_bid) / 2) * 10_000
        return {
            "status": "FRESH", "source_provider": book.metadata.provider, "source_venue": book.metadata.venue,
            "receive_age_seconds": age / 1000, "spread_bps": spread_bps,
            "exchange_time_kind": book.metadata.exchange_time_kind,
            "exchange_timestamp_ms": book.metadata.exchange_timestamp_ms if book.metadata.exchange_time_kind == "EXCHANGE_EVENT" else None,
            "received_at_ms": book.metadata.received_at_ms,
            "top5_bid_depth_quote_units": bid_quote, "top5_ask_depth_quote_units": ask_quote,
            "top5_imbalance": (bid_quote - ask_quote) / (bid_quote + ask_quote) if bid_quote + ask_quote > 0 else None,
            "note": "Observed displayed depth; not executable capacity or a fill guarantee.",
        }

    def _derivative_view(self, instrument: Instrument, label: str, now_ms: int) -> dict[str, Any]:
        cached = (self._funding if label == "funding" else self._open_interest).get(instrument.instrument_id)
        try:
            provider = self.market_data.providers.get(instrument.provider)
        except KeyError:
            provider = None
        if instrument.market_type is not MarketType.PERPETUAL or not isinstance(provider, DerivativesDataProvider):
            return {"status": "NOT_AVAILABLE"}
        if cached is None:
            attempt = self._fetch_status.get((instrument.instrument_id, label), {})
            return {"status": attempt.get("status", "NOT_SAMPLED"), "reason": attempt.get("reason")}
        value = cached[0]
        issue = self._metadata_issue(value.metadata, instrument, now_ms, self.derivatives_ttl_ms)
        if issue:
            return {"status": "STALE", "reason": issue,
                    "receive_age_seconds": max(0, now_ms - value.metadata.received_at_ms) / 1000}
        if label == "funding":
            return {"status": "FRESH", "rate": value.rate,
                    "next_funding_time_ms": value.next_funding_time_ms, "source": value.metadata.source,
                    "exchange_time_kind": value.metadata.exchange_time_kind,
                    "exchange_timestamp_ms": value.metadata.exchange_timestamp_ms if value.metadata.exchange_time_kind == "EXCHANGE_EVENT" else None,
                    "received_at_ms": value.metadata.received_at_ms,
                    "receive_age_seconds": max(0, now_ms-value.metadata.received_at_ms)/1000}
        return {"status": "FRESH", "base": value.open_interest_base, "quote": value.open_interest_quote,
                "source": value.metadata.source, "exchange_time_kind": value.metadata.exchange_time_kind,
                "exchange_timestamp_ms": value.metadata.exchange_timestamp_ms if value.metadata.exchange_time_kind == "EXCHANGE_EVENT" else None,
                "received_at_ms": value.metadata.received_at_ms,
                "receive_age_seconds": max(0, now_ms-value.metadata.received_at_ms)/1000}

    async def _refresh_interval(self, instrument: Instrument, interval: str, now_ms: int) -> None:
        key = (instrument.instrument_id, interval)
        old = self._fetch_status.get(key, {})
        # Poll at a fraction of the interval; do not backfill all markets every loop.
        refresh_ms = min(max(60_000, INTERVAL_MS[interval] // 5), 900_000)
        if now_ms - int(old.get("attempted_at_ms", 0)) < refresh_ms:
            return
        state = {**old, "attempted_at_ms": now_ms, "status": "FETCHING", "reason": None}
        self._fetch_status[key] = state
        try:
            result = await self._request(instrument, lambda provider: provider.fetch_candles(
                instrument, interval, limit=self.history_limit
            ))
            received = self._now_ms()
            accepted, dropped, _ = self._insert_candles(instrument, interval, result, received)
            state.update(
                status="FRESH" if accepted else "STALE" if result else "UNAVAILABLE",
                reason=None if accepted else "all_candles_rejected_by_time_or_identity" if result else "empty_candle_response",
                received_at_ms=received, dropped=dropped,
            )
        except (ProviderCapabilityError, NotImplementedError) as exc:
            self._failures += 1
            state.update(status="NOT_AVAILABLE", reason=f"unsupported_interval_or_provider:{type(exc).__name__}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._failures += 1
            state.update(status="DEGRADED", reason=f"{type(exc).__name__}: {exc}"[:160])

    async def _refresh_derivatives(self, instrument: Instrument, now_ms: int, budget: list[int]) -> None:
        if instrument.market_type is not MarketType.PERPETUAL or budget[0] < 2:
            return
        provider = self.market_data.providers.get(instrument.provider)
        if not isinstance(provider, DerivativesDataProvider):
            return
        for label, operation, cache in (
            ("funding", lambda: provider.fetch_funding(instrument), self._funding),
            ("open_interest", lambda: provider.fetch_open_interest(instrument), self._open_interest),
        ):
            attempt_key = (instrument.instrument_id, label)
            if now_ms - self._derivative_attempts.get(attempt_key, 0) < self.derivatives_ttl_ms:
                continue
            self._derivative_attempts[attempt_key] = now_ms
            budget[0] -= 1
            try:
                value = await self._request(instrument, lambda _provider, op=operation: op())
                received_now = self._now_ms()
                issue = self._metadata_issue(value.metadata, instrument, received_now, self.derivatives_ttl_ms)
                if issue:
                    cache.pop(instrument.instrument_id, None)
                    self._fetch_status[attempt_key] = {"status": "DEGRADED", "reason": issue, "attempted_at_ms": now_ms}
                else:
                    cache[instrument.instrument_id] = (value, received_now)
                    self._fetch_status[attempt_key] = {"status": "FRESH", "attempted_at_ms": now_ms, "received_at_ms": received_now}
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                cache.pop(instrument.instrument_id, None)
                self._failures += 1
                self._fetch_status[attempt_key] = {"status": "DEGRADED", "reason": f"{type(exc).__name__}: {exc}"[:160], "attempted_at_ms": now_ms}

    async def analyze_instrument(self, instrument_id: str) -> dict[str, Any] | None:
        try:
            instrument = self.market_data.instruments.get(instrument_id)
        except KeyError:
            return None
        now = self._now_ms()
        rows = {interval: self._interval_metrics(instrument, interval, now) for interval in self.intervals}
        tf_metrics = {interval: value[0] for interval, value in rows.items() if value[0]}
        qualities = {interval: asdict(value[1]) for interval, value in rows.items()}
        directions = [value["trend"] for value in tf_metrics.values() if value.get("trend") in {"UP", "DOWN"}]
        agreement = (max(directions.count("UP"), directions.count("DOWN")) / len(directions)) if len(directions) >= 2 else None
        bbo = self.market_data.truth_engine.latest_bbo(instrument_id)
        bbo_age = max(0, now - bbo.metadata.received_at_ms) if bbo else None
        bbo_data = None
        bbo_issue = self._metadata_issue(bbo.metadata, instrument, now, self.book_stale_ms) if bbo else "no_bbo"
        if bbo and bbo_age is not None and bbo_age <= self.book_stale_ms and not bbo_issue:
            bbo_data = {"bid": bbo.bid, "ask": bbo.ask, "mid": bbo.mid, "spread_bps": bbo.spread_bps,
                        "receive_age_seconds": bbo_age / 1000, "source_provider": bbo.metadata.provider,
                        "source_venue": bbo.metadata.venue,
                        "exchange_time_kind": bbo.metadata.exchange_time_kind,
                        "exchange_timestamp_ms": bbo.metadata.exchange_timestamp_ms if bbo.metadata.exchange_time_kind == "EXCHANGE_EVENT" else None,
                        "received_at_ms": bbo.metadata.received_at_ms}
        try:
            provider = self.market_data.providers.get(instrument.provider)
        except KeyError:
            provider = None
        market_truth = self.market_data.truth.get(instrument.canonical_asset_id)
        source_note = "Описательное наблюдение одной площадки; независимого ценового quorum нет." if market_truth is None or market_truth.canonical_price is None else f"Статус multi-venue market truth: {market_truth.status.value}; карточка относится к этому venue-инструменту."
        reasons = []
        if not tf_metrics:
            reasons.append("No interval has enough fresh contiguous closed candles.")
        if len(directions) < 2:
            reasons.append("Multi-timeframe agreement unavailable: fewer than two usable intervals.")
        elif agreement is not None and agreement < 0.75:
            reasons.append("Timeframe trends disagree; descriptive bias is mixed.")
        if bbo_data is None:
            reasons.append("Fresh BBO unavailable.")
        card = {
            "instrument_id": instrument.instrument_id, "canonical_asset_id": instrument.canonical_asset_id,
            "provider": instrument.provider, "venue": instrument.venue, "market_type": instrument.market_type.value,
            "base": instrument.base, "quote": instrument.quote, "venue_symbol": instrument.venue_symbol,
            "observed_at_ms": now, "observation_status": "OBSERVED" if tf_metrics else "DEGRADED",
            "observation_score": round(100 * (len(tf_metrics) / max(1, len(self.intervals))) * (agreement or 0.5), 1),
            "score_semantics": "descriptive coverage/agreement score; not probability, expected return, or recommendation",
            "source_note": source_note, "candles": tf_metrics, "candle_quality": qualities,
            "multi_timeframe_agreement": agreement, "bbo": bbo_data,
            "book": self._book_metrics(instrument, now),
            "funding": self._derivative_view(instrument, "funding", now),
            "open_interest": self._derivative_view(instrument, "open_interest", now),
            "reasons": reasons, "limitations": ["Только наблюдение: валидированной модели, вероятности успеха, торгового сигнала и исполнимой ёмкости нет.", "Валюта котировки и множитель контракта сохранены как у площадки; тикер сам по себе не объединяет инструменты."],
        }
        self._cards[instrument_id] = card
        return card

    def _save_observation(self, instrument: Instrument, card: Mapping[str, Any], now_ms: int) -> bool:
        if self.repository is None or not hasattr(self.repository, "save_analysis_observation"):
            return False
        cadence_bucket = now_ms // self.observation_cadence_ms
        bbo = self.market_data.truth_engine.latest_bbo(instrument.instrument_id)
        bbo_kind = bbo.metadata.exchange_time_kind if bbo else ""
        bbo_receive_fresh = bool(bbo and 0 < bbo.metadata.received_at_ms <= now_ms
            and now_ms - bbo.metadata.received_at_ms <= self.book_stale_ms)
        bbo_exchange_fresh = bool(bbo and bbo_kind == "EXCHANGE_EVENT"
            and 0 < bbo.metadata.exchange_timestamp_ms <= now_ms
            and now_ms - bbo.metadata.exchange_timestamp_ms <= self.book_stale_ms)
        reference_ok = bool(bbo and bbo.metadata.instrument_id == instrument.instrument_id
            and bbo.metadata.provider == instrument.provider and bbo.metadata.venue == instrument.venue
            and bbo_receive_fresh and (bbo_exchange_fresh if bbo_kind == "EXCHANGE_EVENT" else bbo_kind == "RECEIVE_TIME_FALLBACK"))
        causal_candles: dict[str, Any] = {}
        candle_lineage: dict[str, Any] = {}
        causal_quality: dict[str, Any] = {}
        for interval, values in card.get("candles", {}).items():
            quality = card.get("candle_quality", {}).get(interval, {})
            received = quality.get("received_at_ms")
            exchange = values.get("latest_exchange_time_ms")
            rejection = None
            if quality.get("status") not in {"FRESH", "FRESH_WITH_GAPS"}:
                rejection = quality.get("reason") or f"candle_quality_{quality.get('status', 'unavailable').lower()}"
            elif received is None or int(received) <= 0:
                rejection = "missing_candle_receive_timestamp"
            elif int(received) > now_ms:
                rejection = "future_candle_receive_timestamp"
            elif exchange is None or int(exchange) <= 0:
                rejection = "missing_candle_exchange_timestamp"
            elif int(exchange) > now_ms:
                rejection = "future_candle_exchange_timestamp"
            # Exclude an entire feature group if its latest constituent was not
            # available by the decision timestamp. Never repair it from a later fetch.
            if rejection is not None:
                safe_quality = {key: value for key, value in quality.items()
                    if key not in {"received_at_ms", "candle_event_age_seconds", "exchange_timestamp_ms", "available_at_ms"}}
                safe_quality["status"] = "CAUSALITY_REJECTED" if rejection.startswith("future_") or rejection.startswith("missing_candle_") else quality.get("status", "UNAVAILABLE")
                safe_quality["reason"] = str(rejection)[:160]
                causal_quality[interval] = safe_quality
                continue
            causal_candles[interval] = dict(values)
            causal_quality[interval] = dict(quality)
            candle_lineage[interval] = {"provider": values.get("source_provider"), "venue": values.get("source_venue"),
                "received_at_ms": int(received), "exchange_timestamp_ms": int(exchange),
                "exchange_time_kind": "DERIVED_INTERVAL_CLOSE", "available_at_ms": int(received)}
        for interval, quality in card.get("candle_quality", {}).items():
            if interval not in causal_quality:
                safe_quality = {key: value for key, value in quality.items()
                    if key not in {"received_at_ms", "candle_event_age_seconds", "exchange_timestamp_ms", "available_at_ms"}}
                if safe_quality.get("status") in {"FRESH", "FRESH_WITH_GAPS"}:
                    safe_quality["status"] = "CAUSALITY_REJECTED"
                    safe_quality["reason"] = "missing_candle_feature_snapshot"
                causal_quality[interval] = safe_quality
        if not reference_ok:
            bbo = None
        membership = next((row for row in self.market_data.membership if row.instrument_id == instrument.instrument_id), None)
        universe_source = getattr(self.market_data, "universe_snapshot", {}) or {}
        universe_rows = list(universe_source.get("candidates", []))
        if not universe_rows:
            universe_rows = []
            for item in self.market_data.membership:
                try:
                    member_instrument = self.market_data.instruments.get(item.instrument_id)
                except KeyError:
                    continue
                universe_rows.append({"instrument_id": item.instrument_id, "canonical_asset_id": item.canonical_asset_id,
                    "provider": member_instrument.provider, "venue": member_instrument.venue,
                    "market_type": member_instrument.market_type.value, "base": member_instrument.base,
                    "quote": member_instrument.quote, "venue_symbol": member_instrument.venue_symbol,
                    "chain": member_instrument.chain, "contract_address": member_instrument.contract_address,
                    "selected": True, "deep_watch": bool(item.deep_watchlist), "score": item.score,
                    "bucket": item.bucket.value, "reasons": list(item.reasons), "source": "universe_membership"})
        universe_rows.sort(key=lambda item: str(item.get("instrument_id", "")))
        universe_time = int(universe_source.get("observed_at_ms") or now_ms)
        if universe_time > now_ms:
            universe_rows = []
            universe_time = now_ms
        universe_canonical = json.dumps(universe_rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        universe_hash = hashlib.sha256(universe_canonical.encode()).hexdigest()
        cadence_bucket = now_ms // self.observation_cadence_ms
        universe_snapshot_id = hashlib.sha256(f"{universe_hash}|{cadence_bucket}".encode()).hexdigest()
        selection_lineage = {"catalog_scopes": {
            key: {field: value for field, value in details.items()
                  if field not in {"received_at_ms", "checked_at_ms"} or value is None or int(value) <= now_ms}
            for key, details in universe_source.get("catalog_scopes", {}).items()
        }, "provider_error_scopes": list(universe_source.get("provider_error_scopes", []))}
        if hasattr(self.repository, "save_analysis_universe_snapshot"):
            self.repository.save_analysis_universe_snapshot({
            "snapshot_id": universe_snapshot_id, "observed_at_ms": universe_time,
            "cadence_bucket": cadence_bucket, "universe_hash": universe_hash,
            "candidate_count": len(universe_rows),
            "deep_count": sum(bool(item.get("deep_watch")) for item in universe_rows),
            "members": universe_rows, "selection_lineage": selection_lineage,
            "recorded_at_ms": self._now_ms(),
            })
        selected_row = next((item for item in universe_rows if item.get("instrument_id") == instrument.instrument_id), {})
        trends = [value.get("trend") for value in causal_candles.values() if value.get("trend") in {"UP", "DOWN"}]
        if trends and trends.count("UP") != trends.count("DOWN"):
            baseline = 1 if trends.count("UP") > trends.count("DOWN") else -1
        else:
            baseline = 0
        bbo_meta = bbo.metadata if bbo else None
        causal_observation_status = "OBSERVED" if causal_candles and card.get("observation_status") == "OBSERVED" else "DEGRADED"
        feature_views = {kind: self._causal_feature_view(card.get(kind), now_ms)
            for kind in ("book", "funding", "open_interest")}
        feature_quality: dict[str, dict[str, Any]] = {}
        feature_lineage: dict[str, dict[str, Any] | None] = {}
        for kind, view in feature_views.items():
            raw = card.get(kind) or {}
            if view is not None:
                feature_quality[kind] = {"status": "FRESH", "reason": None,
                    "exchange_time_kind": view["exchange_time_kind"], "available_at_ms": view["available_at_ms"]}
                feature_lineage[kind] = {"provider": view.get("provider", instrument.provider),
                    "venue": view.get("venue", instrument.venue),
                    "exchange_timestamp_ms": view.get("exchange_timestamp_ms"),
                    "exchange_time_kind": view.get("exchange_time_kind"),
                    "received_at_ms": view.get("received_at_ms"),
                    "available_at_ms": view.get("available_at_ms")}
            else:
                rejection = self._causal_feature_rejection(raw, now_ms)
                status = raw.get("status", "UNAVAILABLE")
                if status == "FRESH":
                    status = "CAUSALITY_REJECTED"
                feature_quality[kind] = {"status": status, "reason": rejection}
                feature_lineage[kind] = None
        membership_bucket = getattr(getattr(membership, "bucket", None), "value", None)
        row = {
            "observation_id": hashlib.sha256(f"{instrument.instrument_id}|{self.feature_version}|{self.feature_config_hash}|{cadence_bucket}".encode()).hexdigest(),
            "instrument_id": instrument.instrument_id, "provider_id": instrument.provider, "venue": instrument.venue,
            "market_type": instrument.market_type.value, "base": instrument.base, "quote": instrument.quote,
            "venue_symbol": instrument.venue_symbol, "canonical_asset_id": instrument.canonical_asset_id,
            "chain": instrument.chain, "contract_address": instrument.contract_address,
            "price_decimals": instrument.price_decimals, "size_decimals": instrument.size_decimals,
            "observed_at_ms": now_ms, "feature_version": self.feature_version, "feature_config_hash": self.feature_config_hash,
            "reference_price": bbo.mid if bbo else None, "reference_spread_bps": bbo.spread_bps if bbo else None,
            "reference_received_at_ms": bbo_meta.received_at_ms if bbo_meta else None,
            "reference_exchange_timestamp_ms": bbo_meta.exchange_timestamp_ms if bbo_meta and bbo_meta.exchange_time_kind == "EXCHANGE_EVENT" else None,
            "reference_time_semantics": ("FRESH_BBO_MID_EXCHANGE_AND_RECEIVE" if bbo_meta and bbo_meta.exchange_time_kind == "EXCHANGE_EVENT"
                else "FRESH_BBO_MID_RECEIVE_TIME_ONLY" if bbo_meta else "UNAVAILABLE_NO_CAUSAL_FRESH_BBO"),
            "baseline_direction": baseline, "universe_snapshot_id": universe_snapshot_id,
            "universe_member": int(membership is not None and membership.deep_watchlist),
            "universe_bucket": membership_bucket, "universe_score": float(membership.score) if membership else selected_row.get("score"),
            "universe_reasons_json": self.repository.storage.json(list(getattr(membership, "reasons", selected_row.get("reasons", ())))),
            "features_json": self.repository.storage.json({
                "candles": causal_candles,
                **feature_views,
            }),
            "quality_json": self.repository.storage.json({"observation_status": causal_observation_status, "candle_quality": causal_quality,
                "reference_status": "FRESH" if bbo else "UNAVAILABLE",
                "reference_time_semantics": ("EXCHANGE_AND_RECEIVE" if bbo_meta and bbo_meta.exchange_time_kind == "EXCHANGE_EVENT"
                    else "RECEIVE_ONLY" if bbo_meta else "UNAVAILABLE"),
                "reference_reason": None if bbo else "no_fresh_causal_bbo",
                "feature_quality": feature_quality}),
            "source_lineage_json": self.repository.storage.json({
                "bbo": ({"provider": bbo_meta.provider, "venue": bbo_meta.venue,
                    "exchange_timestamp_ms": bbo_meta.exchange_timestamp_ms if bbo_meta.exchange_time_kind == "EXCHANGE_EVENT" else None,
                    "exchange_time_kind": bbo_meta.exchange_time_kind,
                    "received_at_ms": bbo_meta.received_at_ms,
                    "available_at_ms": bbo_meta.received_at_ms} if bbo_meta else None),
                "candles": candle_lineage,
                **feature_lineage,
            }),
            "context_json": self.repository.storage.json({"status": "NO_VERIFIED_CONTEXT", "reason": "no exact canonical event/on-chain causal join configured"}),
            "recorded_at_ms": self._now_ms(),
            "cost_policy": {**self.costs, "policy_version": self.cost_policy_version,
                "observation_cadence_ms": self.observation_cadence_ms,
                "outcome_max_latency_ms": self.outcome_max_latency_ms},
        }
        return self.repository.save_analysis_observation(row)

    @staticmethod
    def _causal_feature_rejection(value: Mapping[str, Any] | None, cutoff_ms: int) -> str | None:
        if not value:
            return "source_snapshot_missing"
        if value.get("status") != "FRESH":
            return str(value.get("reason") or f"source_status_{value.get('status', 'unavailable')}")[:160]
        received = value.get("received_at_ms")
        if received is None or int(received) <= 0:
            return "missing_receive_timestamp"
        if int(received) > cutoff_ms:
            return "future_receive_timestamp"
        kind = value.get("exchange_time_kind")
        if kind == "EXCHANGE_EVENT":
            exchange = value.get("exchange_timestamp_ms")
            if exchange is None or int(exchange) <= 0:
                return "missing_exchange_timestamp"
            if int(exchange) > cutoff_ms:
                return "future_exchange_timestamp"
        elif kind != "RECEIVE_TIME_FALLBACK":
            return "unsupported_timestamp_provenance"
        return None

    @staticmethod
    def _causal_feature_view(value: Mapping[str, Any] | None, cutoff_ms: int) -> dict[str, Any] | None:
        if MultiAssetAnalysisService._causal_feature_rejection(value, cutoff_ms) is not None:
            return None
        assert value is not None
        received = int(value["received_at_ms"])
        kind = value["exchange_time_kind"]
        result = dict(value)
        if kind != "EXCHANGE_EVENT":
            result["exchange_timestamp_ms"] = None
        result["available_at_ms"] = int(received)
        return result

    async def _process_due_outcomes(self, now_ms: int) -> None:
        if self.repository is None or not hasattr(self.repository, "due_analysis_outcomes"):
            return
        expired = self.repository.expired_analysis_outcomes(now_ms, 2_000)
        if expired:
            changed = self.repository.finish_analysis_outcomes([
                (row["observation_id"], row["horizon"], {
                    "status": "UNAVAILABLE", "reason": "target_quote_deadline_missed", "updated_at_ms": now_ms,
                }) for row in expired
            ])
            self._outcomes_unavailable += changed
        rows = self.repository.due_analysis_outcomes(now_ms, 2_000)
        live_groups: dict[tuple[str, str], list[tuple[dict[str, Any], dict[str, Any], Instrument]]] = {}
        terminal_updates: list[tuple[str, str, dict[str, Any]]] = []
        def unavailable(row: dict[str, Any], reason: str, at_ms: int) -> None:
            terminal_updates.append((row["observation_id"], row["horizon"], {
                "status": "UNAVAILABLE", "reason": reason, "updated_at_ms": at_ms,
            }))
        for row in rows:
            try:
                policy = json.loads(row["cost_assumptions_json"])
                latency_ms = max(5_000, min(300_000, int(policy["outcome_max_latency_ms"])))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                unavailable(row, "invalid_frozen_observation_policy", self._now_ms())
                continue
            target = int(row["target_at_ms"])
            deadline = target + latency_ms
            if not row.get("reference_price"):
                reason = "missing_fresh_reference_bbo"
            else:
                reason = None
            try:
                instrument = self.market_data.instruments.get(row["instrument_id"])
                if (instrument.provider, instrument.venue, instrument.market_type.value, instrument.base,
                    instrument.quote, instrument.venue_symbol) != (row["provider_id"], row["venue"], row["market_type"],
                    row["base"], row["quote"], row["venue_symbol"]):
                    reason = "instrument_registry_identity_mismatch"
            except KeyError:
                # Restore strictly from the immutable observation identity. Never
                # look up by base/ticker, and let the configured provider fail closed.
                try:
                    instrument = Instrument(
                        instrument_id=row["instrument_id"], canonical_asset_id=row["canonical_asset_id"],
                        provider=row["provider_id"], venue=row["venue"], market_type=MarketType(row["market_type"]),
                        base=row["base"], quote=row["quote"], venue_symbol=row["venue_symbol"],
                        chain=row.get("chain"), contract_address=row.get("contract_address"),
                        price_decimals=int(row["price_decimals"]), size_decimals=int(row["size_decimals"]),
                        metadata={"restored_exact_observation_identity": True},
                    )
                except (KeyError, TypeError, ValueError):
                    instrument = None
                    reason = "instrument_identity_unavailable_after_restart"
            if instrument is not None and not instrument.active:
                reason = "instrument_inactive_or_delisted"
            if reason is not None:
                unavailable(row, reason, self._now_ms())
                continue
            if self._now_ms() > deadline:
                unavailable(row, "target_quote_deadline_missed", self._now_ms())
                continue
            # One current BBO may resolve several horizon rows for this exact
            # venue instrument and quote. Each row is still validated against
            # its own immutable target/deadline below.
            key = (row["instrument_id"], row["quote"])
            live_groups.setdefault(key, []).append((row, policy, instrument))

        if terminal_updates:
            changed = self.repository.finish_analysis_outcomes(terminal_updates)
            self._outcomes_unavailable += changed
        terminal_updates = []

        # Finalize every already-expired/missing/invalid row above without using the
        # network request quota. Live quote work is grouped and separately bounded.
        active = list(live_groups.items())[:self.outcome_requests_per_cycle]

        async def fetch_group(key: tuple[str, str], members: list[tuple[dict[str, Any], dict[str, Any], Instrument]]):
            row0, _, instrument = members[0]
            try:
                quote = await self._request(instrument, lambda provider: provider.fetch_bbo(instrument))
                return key, quote, None, self._now_ms()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return key, None, type(exc).__name__, self._now_ms()

        self._outcome_groups_attempted = getattr(self, "_outcome_groups_attempted", 0)
        self._outcome_groups_attempted += len(active)
        results = await asyncio.gather(*(fetch_group(key, group) for key, group in active), return_exceptions=True)
        results_by_key: dict[tuple[str, str], tuple[Any, str | None, int]] = {}
        for result in results:
            if isinstance(result, BaseException):
                continue
            key, quote, error, completed_at_ms = result
            results_by_key[key] = (quote, error, completed_at_ms)

        for key, members in active:
            quote, error, completed_at_ms = results_by_key.get(key, (None, "request_cancelled_or_failed", self._now_ms()))
            for row, policy, instrument in members:
                target = int(row["target_at_ms"])
                latency_ms = int(policy["outcome_max_latency_ms"])
                deadline = target + latency_ms
                if completed_at_ms > deadline:
                    error = "target_quote_deadline_missed"
                if quote is None:
                    if error == "target_quote_deadline_missed":
                        unavailable(row, error, completed_at_ms)
                    elif error:
                        self._failures += 1
                    continue
                meta = quote.metadata
                exchange_known = meta.exchange_time_kind == "EXCHANGE_EVENT"
                valid = (meta.instrument_id == instrument.instrument_id and meta.provider == instrument.provider
                    and meta.venue == instrument.venue and row["quote"] == instrument.quote
                    and target <= meta.received_at_ms <= min(deadline, completed_at_ms)
                    and (target <= meta.exchange_timestamp_ms <= min(deadline, completed_at_ms) if exchange_known
                         else meta.exchange_time_kind == "RECEIVE_TIME_FALLBACK"))
                if not valid:
                    if completed_at_ms >= deadline:
                        unavailable(row, "target_bbo_identity_or_time_invalid_at_deadline", completed_at_ms)
                    continue
                gross = quote.mid / float(row["reference_price"]) - 1.0
                spread_cost_bps = (float(row["reference_spread_bps"] or 0.0) + quote.spread_bps) / 2.0
                fee_bps = max(0.0, float(policy["fee_bps_per_side"]))
                slippage_bps = max(0.0, float(policy["slippage_bps_per_side"]))
                total_cost_bps = 2 * fee_bps + 2 * slippage_bps + spread_cost_bps
                cost_fraction = total_cost_bps / 10_000
                direction = int(row["baseline_direction"])
                baseline_gross = direction * gross
                baseline_net = baseline_gross - cost_fraction if direction != 0 else None
                long_reference_net = gross - cost_fraction
                values = {"status": "OBSERVED", "target_mid": quote.mid, "target_spread_bps": quote.spread_bps,
                    "target_exchange_timestamp_ms": meta.exchange_timestamp_ms if exchange_known else None,
                    "target_received_at_ms": meta.received_at_ms,
                    "target_time_semantics": "EXCHANGE_AND_RECEIVE" if exchange_known else "RECEIVE_TIME_ONLY",
                    "gross_reference_return": gross, "long_reference_net_return": long_reference_net,
                    "costs_adjusted_observational_return": long_reference_net,
                    "baseline_gross_return": baseline_gross, "baseline_costs_adjusted_return": baseline_net,
                    "observed_costs": {"roundtrip_spread_bps": spread_cost_bps,
                        "roundtrip_fee_bps": 2 * fee_bps, "roundtrip_slippage_bps": 2 * slippage_bps,
                        "total_roundtrip_cost_bps": total_cost_bps, "funding_included": False},
                    "updated_at_ms": completed_at_ms}
                terminal_updates.append((row["observation_id"], row["horizon"], values))
        if terminal_updates:
            self._outcomes_completed += self.repository.finish_analysis_outcomes(terminal_updates)
            self._outcomes_unavailable += sum(item[2]["status"] == "UNAVAILABLE" for item in terminal_updates)

    async def refresh_once(self) -> dict[str, Any]:
        cycle_started = time.monotonic()
        now = self._now_ms()
        self._requests = 0
        # Matured labels have a strict target deadline. Service them before
        # optional candle/derivative refresh work so market-data latency cannot
        # consume the quote window for outcomes already due at cycle start.
        self._cycle_request_limit = self.max_requests_per_cycle
        await self._process_due_outcomes(now)
        now = self._now_ms()
        instruments = self._eligible_members()
        self._prune_universe(instruments)
        if instruments:
            start = self._cursor % len(instruments)
            per_instrument_budget = max(1, self.max_requests_per_cycle // len(self.intervals))
            cycle_rotation_size = min(self.rotation_size, per_instrument_budget)
            rotated = (instruments[start:] + instruments[:start])[:cycle_rotation_size]
            self._cursor = (start + len(rotated)) % len(instruments)
        else:
            rotated = []
        strategy = getattr(self, "strategy_shadow", None)
        if strategy is not None:
            strategy.expire_pending(self._now_ms())
            monitor_budget = min(self.shadow_monitor_requests_per_cycle,
                max(0, self.max_requests_per_cycle - self._requests - len(rotated) * len(self.intervals)))
            open_ids = strategy.monitored_instrument_ids(self._now_ms(), "OPEN")
            pending_ids = strategy.monitored_instrument_ids(self._now_ms(), "PENDING")
            selected: list[str] = []

            def take_monitors(ids: list[str], cursor_name: str, limit: int) -> None:
                if not ids or limit <= 0 or len(selected) >= limit:
                    return
                start = getattr(self, cursor_name) % len(ids)
                scanned = 0
                for instrument_id in ids[start:] + ids[:start]:
                    scanned += 1
                    if instrument_id not in selected:
                        selected.append(instrument_id)
                    if len(selected) >= limit:
                        break
                setattr(self, cursor_name, (start + scanned) % len(ids))

            # A pending entry can expire without a quote; an open position
            # cannot. Reserve an open slot first, and one pending slot when
            # both groups exist and the request budget permits it.
            open_quota = monitor_budget - 1 if open_ids and pending_ids and monitor_budget > 1 else monitor_budget
            take_monitors(open_ids, "_shadow_open_cursor", open_quota)
            take_monitors(pending_ids, "_shadow_pending_cursor", monitor_budget)
            take_monitors(open_ids, "_shadow_open_cursor", monitor_budget)
            if selected:
                for instrument_id in selected:
                    now_monitor = self._now_ms()
                    try:
                        monitored = self.market_data.instruments.get(instrument_id)
                        if not monitored.active:
                            strategy.advance({"instrument_id": instrument_id}, now_monitor)
                            continue
                        bbo = await self._request(monitored, lambda provider: provider.fetch_bbo(monitored))
                        meta = bbo.metadata
                        strategy.advance({"instrument_id": meta.instrument_id, "provider": meta.provider,
                            "venue": meta.venue, "quote": monitored.quote,
                            "market_type": monitored.market_type.value,
                            "received_at_ms": meta.received_at_ms,
                            "exchange_time_kind": meta.exchange_time_kind,
                            "exchange_timestamp_ms": meta.exchange_timestamp_ms,
                            "bid": bbo.bid, "ask": bbo.ask}, self._now_ms())
                    except asyncio.CancelledError:
                        raise
                    except KeyError:
                        strategy.advance({"instrument_id": instrument_id}, self._now_ms())
                    except Exception as exc:
                        self._failures += 1
                        self._last_cycle_error = f"shadow monitor {instrument_id}: {type(exc).__name__}: {exc}"[:200]
                        strategy.advance({"instrument_id": instrument_id}, self._now_ms())
        # Outcome requests have already consumed part of the 30-request cycle
        # cap; leave slots for every selected instrument's candle calls before
        # admitting optional funding/OI refreshes.
        request_budget = [max(0, self.max_requests_per_cycle - self._requests - len(rotated) * len(self.intervals))]
        async def analyze(instrument: Instrument) -> None:
            try:
                for interval in self.intervals:
                    await self._refresh_interval(instrument, interval, now)
                await self._refresh_derivatives(instrument, now, request_budget)
                card = await self.analyze_instrument(instrument.instrument_id)
                if card is not None:
                    if self._save_observation(instrument, card, int(card["observed_at_ms"])):
                        self._observations_saved += 1
                    strategy = getattr(self, "strategy_shadow", None)
                    if strategy is not None:
                        plans = strategy.evaluate(card)
                        on_plans = getattr(self, "on_plans", None)
                        if on_plans is not None:
                            try:
                                await on_plans(plans, card, self._now_ms())
                            except asyncio.CancelledError:
                                raise
                            except Exception as exc:
                                # Delivery is separate from market computation.
                                self._failures += 1
                                self._last_cycle_error = f"multi-asset alert: {type(exc).__name__}"
                    self._instrument_errors.pop(instrument.instrument_id, None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._failures += 1
                message = f"{type(exc).__name__}: {exc}"[:200]
                self._instrument_errors[instrument.instrument_id] = message
                self._last_cycle_error = f"{instrument.instrument_id}: {message}"
                self._cards[instrument.instrument_id] = {
                    "instrument_id": instrument.instrument_id, "canonical_asset_id": instrument.canonical_asset_id,
                    "provider": instrument.provider, "venue": instrument.venue,
                    "market_type": instrument.market_type.value, "base": instrument.base,
                    "quote": instrument.quote, "venue_symbol": instrument.venue_symbol,
                    "observed_at_ms": self._now_ms(), "observation_status": "DEGRADED",
                    "observation_score": 0.0, "score_semantics": "incomplete observation after compute error",
                    "source_note": "Observation failed; exact source identity is retained.", "candles": {},
                    "candle_quality": {}, "multi_timeframe_agreement": None, "bbo": None,
                    "book": {"status": "UNAVAILABLE", "reason": "analysis_failed"},
                    "funding": {"status": "NOT_SAMPLED"}, "open_interest": {"status": "NOT_SAMPLED"},
                    "reasons": [message], "limitations": ["Incomplete observation; not a forecast or recommendation."],
                }
        await asyncio.gather(*(analyze(item) for item in rotated), return_exceptions=False)
        self._cycle_request_limit = self.max_requests_per_cycle
        self._cycles += 1
        self._last_cycle_duration_seconds = max(0.0, time.monotonic() - cycle_started)
        self._last_cycle_at_ms = self._now_ms()
        if not self._instrument_errors and not any(v.get("status") == "DEGRADED" for v in self._fetch_status.values()):
            self._last_cycle_error = None
        return self.health()

    async def run_forever(self) -> None:
        self._stop.clear()
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_cycle_error = f"{type(exc).__name__}: {exc}"[:200]
                self._failures += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.1, self.poll_seconds - (time.monotonic() - started)))
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    def get(self, instrument_id: str) -> dict[str, Any] | None:
        saved = self._cards.get(instrument_id)
        if saved is None:
            return None
        eligible_ids = {item.instrument_id for item in self._eligible_members()}
        if instrument_id not in eligible_ids:
            self._prune_universe(self._eligible_members())
            return None
        card = dict(saved)
        instrument = self.market_data.instruments.get(instrument_id)
        now = self._now_ms()
        rows = {interval: self._interval_metrics(instrument, interval, now) for interval in self.intervals}
        card["candles"] = {interval: value[0] for interval, value in rows.items() if value[0]}
        card["candle_quality"] = {interval: asdict(value[1]) for interval, value in rows.items()}
        directions = [value["trend"] for value in card["candles"].values() if value.get("trend") in {"UP", "DOWN"}]
        card["multi_timeframe_agreement"] = (
            max(directions.count("UP"), directions.count("DOWN")) / len(directions) if len(directions) >= 2 else None
        )
        card["observation_status"] = "OBSERVED" if card["candles"] else "DEGRADED"
        card["observation_score"] = round(
            100 * (len(card["candles"]) / max(1, len(self.intervals))) * (card["multi_timeframe_agreement"] or 0.5), 1
        )
        reasons: list[str] = []
        if not card["candles"]:
            reasons.append("No interval has enough fresh contiguous closed candles.")
        for interval, quality in card["candle_quality"].items():
            if quality["status"] not in {"FRESH", "FRESH_WITH_GAPS"} or quality["gaps"]:
                reasons.append(f"{interval}: {quality.get('reason') or quality['status']}.")
        if card["multi_timeframe_agreement"] is None:
            reasons.append("Multi-timeframe agreement unavailable: fewer than two usable intervals.")
        elif card["multi_timeframe_agreement"] < 0.75:
            reasons.append("Timeframe trends disagree; descriptive bias is mixed.")
        bbo = self.market_data.truth_engine.latest_bbo(instrument_id)
        bbo_age = max(0, now - bbo.metadata.received_at_ms) if bbo else None
        bbo_issue = self._metadata_issue(bbo.metadata, instrument, now, self.book_stale_ms) if bbo else "no_bbo"
        card["bbo"] = ({
            "bid": bbo.bid, "ask": bbo.ask, "mid": bbo.mid, "spread_bps": bbo.spread_bps,
            "receive_age_seconds": bbo_age / 1000, "source_provider": bbo.metadata.provider,
            "source_venue": bbo.metadata.venue,
        } if bbo and bbo_age is not None and bbo_age <= self.book_stale_ms and not bbo_issue else None)
        if card["bbo"] is None:
            reasons.append("Fresh BBO unavailable.")
        card["book"] = self._book_metrics(instrument, now)
        card["funding"] = self._derivative_view(instrument, "funding", now)
        card["open_interest"] = self._derivative_view(instrument, "open_interest", now)
        card["reasons"] = reasons
        if instrument_id in self._instrument_errors:
            card["observation_status"] = "DEGRADED"
            card["reasons"].append(self._instrument_errors[instrument_id])
        return card

    def markets(self, limit: int = 30) -> list[dict[str, Any]]:
        limit = max(1, min(30, int(limit)))
        self._prune_universe(self._eligible_members())
        cards = [card for instrument_id in self._cards if (card := self.get(instrument_id)) is not None]
        return sorted(cards, key=lambda card: (-card["observation_score"], card["instrument_id"]))[:limit]

    def health(self) -> dict[str, Any]:
        now = self._now_ms()
        self._prune_universe(self._eligible_members())
        latest_receives = [value.get("received_at_ms") for value in self._fetch_status.values() if value.get("received_at_ms")]
        current_cards = [self.get(instrument_id) for instrument_id in tuple(self._cards)]
        current_cards = [card for card in current_cards if card is not None]
        fresh_candle_instruments = sum(any(
            quality.get("status") in {"FRESH", "FRESH_WITH_GAPS"}
            and quality.get("received_at_ms") is not None
            and now - int(quality["received_at_ms"]) <= self.candle_stale_ms
            for quality in card.get("candle_quality", {}).values()
        ) for card in current_cards)
        fresh_book_instruments = sum(card.get("book", {}).get("status") == "FRESH" for card in current_cards)
        fresh_derivatives = sum(
            card.get("funding", {}).get("status") == "FRESH" and card.get("open_interest", {}).get("status") == "FRESH"
            for card in current_cards if card.get("market_type") == MarketType.PERPETUAL.value
        )
        def degraded(card: dict[str, Any]) -> bool:
            if card.get("observation_status") != "OBSERVED" or card.get("book", {}).get("status") != "FRESH":
                return True
            if card.get("market_type") == MarketType.PERPETUAL.value:
                return any(card.get(name, {}).get("status") in {"STALE", "DEGRADED"} for name in ("funding", "open_interest"))
            return False
        degraded_instruments = sum(degraded(card) for card in current_cards)
        max_deep = min(30, int(getattr(getattr(self.market_data, "universe", None), "max_deep", 30)))
        estimated_demand_per_minute = max_deep * 3 * 60 / max(1, self.observation_cadence_ms / 1000)
        effective_cycle_seconds = max(self.poll_seconds, self._last_cycle_duration_seconds)
        request_capacity_per_minute = self.outcome_requests_per_cycle * 60 / max(1.0, effective_cycle_seconds)
        capacity_ok = request_capacity_per_minute >= estimated_demand_per_minute
        status = "HEALTHY" if fresh_candle_instruments and not degraded_instruments and self._last_cycle_error is None and capacity_ok else "STARTING" if not current_cards else "DEGRADED"
        return {
            "status": status,
            "deep_watch_size": len(self._eligible_members()), "analyzed_instruments": len(self._cards),
            "fresh_candle_instruments": fresh_candle_instruments, "fresh_book_instruments": fresh_book_instruments,
            "fresh_derivative_instruments": fresh_derivatives, "degraded_instruments": degraded_instruments,
            "intervals": list(self.intervals), "cache_candles": len(self._candles),
            "maximum_cached_candles": self.cache_limit, "rotation_size": self.rotation_size,
            "maximum_requests_per_cycle": self.max_requests_per_cycle, "last_cycle_at_ms": self._last_cycle_at_ms,
            "cycles": self._cycles, "failures": self._failures,
            "observation_cadence_seconds": self.observation_cadence_ms / 1000,
            "observations_saved": self._observations_saved,
            "outcomes_completed": self._outcomes_completed, "outcomes_unavailable": self._outcomes_unavailable,
            "outcome_max_latency_seconds": self.outcome_max_latency_ms / 1000,
            "outcome_request_capacity_per_minute": round(request_capacity_per_minute, 2),
            "outcome_request_demand_per_minute": round(estimated_demand_per_minute, 2),
            "outcome_capacity_margin": round(request_capacity_per_minute / estimated_demand_per_minute, 3) if estimated_demand_per_minute else None,
            "outcome_capacity_status": "ADEQUATE" if capacity_ok else "DEGRADED",
            "outcome_groups_attempted": getattr(self, "_outcome_groups_attempted", 0),
            "last_cycle_duration_seconds": round(self._last_cycle_duration_seconds, 3),
            "latest_candle_receive_age_seconds": max(0, now - max(latest_receives)) / 1000 if latest_receives else None,
            "last_error": self._last_cycle_error,
            "note": "Descriptive market observations only; no validated forecasts or trade recommendations.",
        }
