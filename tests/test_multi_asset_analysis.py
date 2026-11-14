from __future__ import annotations

import asyncio
from dataclasses import replace
import sqlite3
import time
from types import SimpleNamespace

import pytest

from src.alerts.commands import TelegramCommandService
from src.alerts.multi_asset import MultiAssetAlertService
from src.data.repository import MarketRepository
from src.data.storage import SQLiteStorage
from src.providers.base import DerivativesDataProvider, ProviderCapabilityError
from src.providers.instruments import InstrumentRegistry
from src.providers.types import (
    EventMetadata, FundingSnapshot, Instrument, MarketType, NormalizedBBO,
    NormalizedBookLevel, NormalizedCandle, NormalizedL2, NormalizedTrade,
    OpenInterestSnapshot, ProviderHealth, ProviderStatus, Side,
)
from src.providers.universe import UniverseBucket, UniverseMembership
from src.service.market_analysis import INTERVAL_MS, MultiAssetAnalysisService
from src.service.strategy_shadow import StrategyShadow
from src.service.scheduler import _start_market_analysis_worker


NOW = int(time.time() * 1000)


def make_instrument(provider: str, market: str, base: str, quote: str, symbol: str, market_type=MarketType.SPOT):
    return Instrument(
        instrument_id=f"{provider}:{market_type.value}:{base}-{quote}",
        canonical_asset_id=f"unresolved:{provider}:{base}:{quote}", provider=provider,
        venue=f"{provider}-{market}", market_type=market_type, base=base, quote=quote,
        venue_symbol=symbol, price_decimals=6, size_decimals=6,
    )


def candle(instrument: Instrument, interval: str, open_time: int, close: float,
           *, received: int = NOW, closed: bool = True) -> NormalizedCandle:
    return NormalizedCandle(
        EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                      open_time + INTERVAL_MS[interval] - 1, received, "fixture-candle",
                      event_id=f"{interval}:{open_time}"),
        interval, open_time, open_time + INTERVAL_MS[interval] - 1,
        close * .999, close * 1.002, close * .998, close, 10.0,
        trade_count=100, closed=closed,
    )


class FakeProvider(DerivativesDataProvider):
    provider_id = "binance"

    def __init__(self, instrument, candles_by_interval, *, fail=False):
        self.instrument = instrument
        self.provider_id = instrument.provider
        self.candles_by_interval = candles_by_interval
        self.fail = fail
        self.calls = []

    async def list_instruments(self):
        return [self.instrument]

    async def fetch_bbo(self, instrument):
        raise ProviderCapabilityError("unused")

    async def fetch_trades(self, instrument, limit=100):
        return []

    async def fetch_l2(self, instrument, depth=20):
        raise ProviderCapabilityError("unused")

    async def fetch_candles(self, instrument, interval, start_ms=None, limit=300):
        self.calls.append((instrument.instrument_id, interval))
        if self.fail:
            raise OSError("fixture provider offline")
        return list(self.candles_by_interval.get(interval, []))

    def health(self):
        return ProviderHealth("binance", ProviderStatus.HEALTHY, NOW)

    async def close(self):
        return None

    async def fetch_funding(self, instrument):
        meta = EventMetadata("binance", instrument.venue, instrument.instrument_id, NOW, NOW, "funding")
        return FundingSnapshot(meta, .0001, NOW + 8 * 3600_000)

    async def fetch_open_interest(self, instrument):
        meta = EventMetadata("binance", instrument.venue, instrument.instrument_id, NOW, NOW, "open-interest")
        return OpenInterestSnapshot(meta, 1234, 12_340_000)

    async def fetch_liquidations(self, instrument, limit=100):
        return []

    async def fetch_mark_index(self, instrument):
        raise ProviderCapabilityError("unused")


class Providers:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, provider):
        return self.mapping[provider]


class Truth:
    def __init__(self, bbo):
        self.bbo = bbo

    def latest_bbo(self, instrument_id):
        return self.bbo.get(instrument_id)


class MarketData:
    def __init__(self, pairs):
        self.instruments = InstrumentRegistry.from_config([item.to_dict() if hasattr(item, "to_dict") else {
            "instrument_id": item.instrument_id, "canonical_asset_id": item.canonical_asset_id,
            "provider": item.provider, "venue": item.venue, "market_type": item.market_type.value,
            "base": item.base, "quote": item.quote, "venue_symbol": item.venue_symbol,
            "price_decimals": item.price_decimals, "size_decimals": item.size_decimals,
        } for item, _, _, _ in pairs])
        self.providers = Providers({provider.provider_id: provider for _, provider, _, _ in pairs})
        self.membership = tuple(UniverseMembership(
            item.instrument_id, item.canonical_asset_id, UniverseBucket.DERIVATIVES_MOVERS,
            1.0, True,
        ) for item, _, _, _ in pairs)
        self.config = {"providers": {item.provider: {"request_timeout_seconds": 2} for item, _, _, _ in pairs}}
        self.truth_engine = Truth({item.instrument_id: bbo for item, _, bbo, _ in pairs if bbo is not None})
        self.latest_l2 = {item.instrument_id: book for item, _, _, book in pairs if book is not None}
        self.truth = {}

    async def _provider_request(self, provider_id, operation):
        return await operation()


def make_market_data(instrument, *, fail=False):
    candles = {}
    for interval in INTERVAL_MS:
        duration = INTERVAL_MS[interval]
        latest_open = (NOW // duration) * duration - duration
        candles[interval] = [
            candle(instrument, interval, latest_open - step * duration, 1.0 + (79 - step) * .001)
            for step in reversed(range(80))
        ]
    provider = FakeProvider(instrument, candles, fail=fail)
    meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id, NOW, NOW, "bbo")
    bbo = NormalizedBBO(meta, 1.0, 1.001, 300, 250)
    book = NormalizedL2(meta, (NormalizedBookLevel(1.0, 300),), (NormalizedBookLevel(1.001, 250),))
    return MarketData([(instrument, provider, bbo, book)]), provider


def test_nonlegacy_perpetual_gets_real_causal_multitimeframe_analysis():
    instrument = make_instrument("binance", "usdm", "1000PEPE", "USDT", "1000PEPEUSDT", MarketType.PERPETUAL)
    data, provider = make_market_data(instrument)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1, "minimum_history": 25})
    health = asyncio.run(service.refresh_once())
    card = service.get(instrument.instrument_id)
    assert card is not None and card["observation_status"] == "OBSERVED"
    assert card["base"] == "1000PEPE" and card["quote"] == "USDT" and card["venue_symbol"] == "1000PEPEUSDT"
    assert set(card["candles"]) == {"5m", "15m", "1h", "4h"}
    assert all(item["trend"] == "UP" for item in card["candles"].values())
    assert card["multi_timeframe_agreement"] == 1.0
    assert card["book"]["status"] == "FRESH" and card["book"]["top5_bid_depth_quote_units"] > 0
    assert card["funding"]["rate"] == .0001 and card["open_interest"]["base"] == 1234
    assert card["score_semantics"].startswith("descriptive")
    assert "probability" not in card and "direction" not in card
    assert health["analyzed_instruments"] == 1 and service._requests == 6


def test_stale_universe_member_does_not_abort_neighbor_analysis():
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, provider = make_market_data(instrument)
    missing = UniverseMembership("binance:spot:DELISTED-USDT", "unresolved:binance:DELISTED:USDT",
        UniverseBucket.DERIVATIVES_MOVERS, 1.0, True)
    data.membership = (missing, *data.membership, *data.membership)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 2})
    asyncio.run(service.refresh_once())
    assert service.get(instrument.instrument_id)["observation_status"] == "OBSERVED"
    assert len(provider.calls) == len(INTERVAL_MS)


def test_normal_nonlegacy_spot_card_creates_frozen_shadow_variants(tmp_path):
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, _ = make_market_data(instrument)
    db = SQLiteStorage(tmp_path / "bonk-shadow.db")
    shadow = StrategyShadow(db)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1})
    service._now_ms = lambda: NOW + 1000
    service.strategy_shadow = shadow
    asyncio.run(service.refresh_once())
    plans = shadow.latest_plans(instrument.instrument_id)
    assert len(plans) == 2
    assert {item["plan"]["variant"] for item in plans} == {"baseline", "disciplined"}
    assert all(item["plan"]["status"] == "PROPOSED" for item in plans)
    assert all(item["plan"]["instrument_id"] == instrument.instrument_id for item in plans)
    assert all(item["plan"]["signal_anchor_ms"] <= NOW for item in plans)
    assert shadow.summary()["baseline"]["status"] == "NOT_EVALUABLE"
    db.close()


def test_normal_spot_cycle_routes_one_bounded_alert_without_network(tmp_path):
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, _ = make_market_data(instrument)
    db = SQLiteStorage(tmp_path / "route.db")
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1})
    service._now_ms = lambda: NOW + 1000
    service.strategy_shadow = StrategyShadow(db)
    sent = []

    async def fake_send(key, message, at):
        sent.append(message)
        return True, "synthetic-id"

    service.on_plans = MultiAssetAlertService(db, {"enabled": True}, fake_send,
        excluded_bases=("SOL", "HYPE")).consider
    asyncio.run(service.refresh_once())
    assert len(sent) == 1
    assert "BONK" in sent[0] and "Экспериментальный" in sent[0]
    asyncio.run(service.refresh_once())
    assert len(sent) == 1
    db.close()


def test_future_open_duplicate_and_out_of_order_candles_are_handled_by_exact_bar_identity():
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data)
    duration = INTERVAL_MS["5m"]
    open_time = (NOW // duration - 2) * duration
    old = candle(instrument, "5m", open_time, 1, received=NOW - 1000)
    fresh_revision = candle(instrument, "5m", open_time, 2, received=NOW)
    future = candle(instrument, "5m", (NOW // duration + 1) * duration, 3, received=NOW)
    open_bar = candle(instrument, "5m", (NOW // duration) * duration, 4, received=NOW, closed=False)
    _, dropped, _ = service._insert_candles(instrument, "5m", [fresh_revision, old, future, open_bar], NOW)
    assert dropped == 2
    assert service._candles[(instrument.instrument_id, "5m", open_time)].close == 2
    assert len([key for key in service._candles if key == (instrument.instrument_id, "5m", open_time)]) == 1


def test_gaps_insufficient_history_and_stale_receive_are_explicit():
    instrument = make_instrument("binance", "spot", "WIF", "USDT", "WIFUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data, {"minimum_history": 25, "candle_stale_seconds": 60})
    duration = INTERVAL_MS["5m"]
    base = (NOW // duration - 1) * duration
    gapped = [candle(instrument, "5m", base - i * duration, 1 + i * .001) for i in list(range(0, 10)) + list(range(16, 30))]
    service._insert_candles(instrument, "5m", gapped, NOW)
    metrics, quality = service._interval_metrics(instrument, "5m", NOW)
    assert not metrics and quality.status == "INSUFFICIENT_HISTORY" and quality.gaps == 1
    service._candles.clear()
    fresh_snapshot = [candle(instrument, "5m", base - i * duration, 1 + i * .001, received=NOW) for i in range(30)]
    service._insert_candles(instrument, "5m", fresh_snapshot, NOW)
    _, stale_quality = service._interval_metrics(instrument, "5m", NOW + 120_000)
    assert stale_quality.status == "STALE" and stale_quality.reason == "candle_snapshot_stale"


def test_recently_received_but_old_last_closed_interval_is_stale():
    instrument = make_instrument("binance", "spot", "MOG", "USDT", "MOGUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data, {"minimum_history": 25})
    duration = INTERVAL_MS["5m"]
    latest_expected = (NOW // duration) * duration - duration
    stale_latest = latest_expected - duration
    bars = [candle(instrument, "5m", stale_latest - i * duration, 1 + i * .001, received=NOW) for i in range(40)]
    service._insert_candles(instrument, "5m", bars, NOW)
    _, quality = service._interval_metrics(instrument, "5m", NOW)
    assert quality.status == "STALE"
    assert quality.reason == "latest_closed_candle_interval_is_old"
    assert quality.candle_event_age_seconds is not None


def test_one_provider_failure_does_not_abort_other_asset_analysis():
    failed = make_instrument("hyperliquid", "perpetual", "BONK", "USDC", "BONK", MarketType.PERPETUAL)
    healthy = make_instrument("kraken", "spot", "PEPE", "USD", "PEPE/USD")
    failed_data, failed_provider = make_market_data(failed, fail=True)
    healthy_data, healthy_provider = make_market_data(healthy)
    # Combine two otherwise independent instruments/providers as the real deep universe does.
    pairs = []
    for data, provider, instrument in ((failed_data, failed_provider, failed), (healthy_data, healthy_provider, healthy)):
        pairs.append((instrument, provider, data.truth_engine.latest_bbo(instrument.instrument_id), data.latest_l2[instrument.instrument_id]))
    combined = MarketData(pairs)
    service = MultiAssetAnalysisService(combined, {"instruments_per_cycle": 2})
    asyncio.run(service.refresh_once())
    assert service.get(failed.instrument_id)["observation_status"] == "DEGRADED"
    assert service.get(healthy.instrument_id)["observation_status"] == "OBSERVED"


def test_normal_scheduler_starts_analysis_worker_when_enabled_and_respects_disable():
    called = []
    class Worker:
        async def run_forever(self):
            called.append("started")
    service = SimpleNamespace(config={"market_analysis": {"enabled": True}}, market_analysis=Worker())
    async def enabled_case():
        task = _start_market_analysis_worker(service)
        assert task is not None and task.get_name() == "multi-asset-descriptive-analysis"
        await asyncio.wait_for(task, timeout=1)
    asyncio.run(enabled_case())
    assert called == ["started"]
    service.config = {"market_analysis": {"enabled": False}}
    assert _start_market_analysis_worker(service) is None


def test_market_commands_use_cycle_created_real_descriptive_cards():
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, _ = make_market_data(instrument)
    analysis = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1})
    asyncio.run(analysis.refresh_once())
    analyzer = SimpleNamespace(
        telegram=object(), repository=SimpleNamespace(current_universe=lambda: []),
        market_analysis=analysis, market_data=data,
    )
    command = TelegramCommandService(analyzer)
    markets = asyncio.run(command.handle("/markets"))[0]
    detail = asyncio.run(command.handle(f"/analyze {instrument.instrument_id}"))[0]
    ambiguous = asyncio.run(command.handle("/analyze BONK"))[0]
    assert instrument.instrument_id in markets and "не являются win probability" in markets
    assert "Наблюдение" in detail and "обновлено" not in detail
    assert "Обновлено" in detail and "не вероятность успеха" in detail
    assert "неоднозначен" in ambiguous


def test_analysis_cache_is_bounded_and_candle_cap_is_enforced():
    instrument = make_instrument("binance", "spot", "FLOKI", "USDT", "FLOKIUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data, {"maximum_cached_candles": 30, "history_candles": 30})
    for interval, duration in INTERVAL_MS.items():
        latest_open = (NOW // duration - 1) * duration
        service._insert_candles(instrument, interval, [candle(instrument, interval, latest_open-i*duration, 1+i*.001) for i in range(20)], NOW)
    assert len(service._candles) <= 30


def test_spot_derivatives_are_explicitly_unavailable_and_stale_book_is_not_used():
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data)
    data.latest_l2[instrument.instrument_id] = NormalizedL2(
        EventMetadata("binance", instrument.venue, instrument.instrument_id, NOW - 120_000, NOW - 120_000, "old-l2"),
        (NormalizedBookLevel(1.0, 100),), (NormalizedBookLevel(1.001, 100),),
    )
    card = asyncio.run(service.analyze_instrument(instrument.instrument_id))
    assert card["funding"]["status"] == "NOT_AVAILABLE"
    assert card["open_interest"]["status"] == "NOT_AVAILABLE"
    assert card["book"]["status"] == "STALE"


def test_worker_runs_repeated_bounded_cycles_and_stops_cleanly():
    instrument = make_instrument("binance", "spot", "POPCAT", "USDT", "POPCATUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1, "maximum_requests_per_cycle": 4})
    async def exercise():
        worker = asyncio.create_task(service.run_forever())
        for _ in range(100):
            if service.health()["cycles"]:
                break
            await asyncio.sleep(.001)
        await service.stop()
        await asyncio.wait_for(worker, timeout=1)
    asyncio.run(exercise())
    assert service.health()["cycles"] >= 1
    assert service._requests <= 4


def test_cached_command_view_transitions_to_stale_without_a_new_provider_call():
    instrument = make_instrument("binance", "spot", "TURBO", "USDT", "TURBOUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1})
    asyncio.run(service.refresh_once())
    assert service.get(instrument.instrument_id)["observation_status"] == "OBSERVED"
    original_now = service._now_ms
    service._now_ms = lambda: original_now() + 2 * 60 * 60 * 1000
    stale = service.get(instrument.instrument_id)
    assert stale["observation_status"] == "DEGRADED"
    assert all(item["status"] == "STALE" for item in stale["candle_quality"].values())
    assert stale["bbo"] is None and "Fresh BBO unavailable." in stale["reasons"]


def test_funding_oi_and_health_age_on_read_without_provider_requests():
    instrument = make_instrument("binance", "usdm", "1000PEPE", "USDT", "1000PEPEUSDT", MarketType.PERPETUAL)
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1})
    asyncio.run(service.refresh_once())
    previous_requests = service._requests
    real_now = service._now_ms
    service._now_ms = lambda: real_now() + 2 * service.derivatives_ttl_ms
    view = service.get(instrument.instrument_id)
    assert view["funding"]["status"] == "STALE" and "rate" not in view["funding"]
    assert view["open_interest"]["status"] == "STALE" and "base" not in view["open_interest"]
    assert service.health()["fresh_derivative_instruments"] == 0
    assert service._requests == previous_requests


def test_derivatives_refresh_failure_drops_old_value_and_independent_oi_retry():
    instrument = make_instrument("binance", "usdm", "1000PEPE", "USDT", "1000PEPEUSDT", MarketType.PERPETUAL)
    data, provider = make_market_data(instrument)
    service = MultiAssetAnalysisService(data)
    asyncio.run(service.refresh_once())
    assert instrument.instrument_id in service._funding and instrument.instrument_id in service._open_interest
    async def fail_funding(_instrument):
        raise OSError("funding unavailable")
    async def fresh_oi(_instrument):
        meta = EventMetadata("binance", instrument.venue, instrument.instrument_id, start, start, "open-interest")
        return OpenInterestSnapshot(meta, 2345, 23_450_000)
    provider.fetch_funding = fail_funding
    provider.fetch_open_interest = fresh_oi
    start = service._now_ms() + service.derivatives_ttl_ms + 1
    service._now_ms = lambda: start
    service._derivative_attempts[(instrument.instrument_id, "open_interest")] = start - 2 * service.derivatives_ttl_ms
    asyncio.run(service._refresh_derivatives(instrument, start, [2]))
    assert instrument.instrument_id not in service._funding
    assert service._open_interest[instrument.instrument_id][0].open_interest_base == 2345
    assert service.get(instrument.instrument_id)["funding"]["status"] == "DEGRADED"
    assert service.get(instrument.instrument_id)["open_interest"]["status"] == "FRESH"


def test_future_and_mismatched_lineage_candles_fail_closed():
    instrument = make_instrument("binance", "spot", "MOG", "USDT", "MOGUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data)
    bar = candle(instrument, "5m", (NOW // INTERVAL_MS["5m"] - 1) * INTERVAL_MS["5m"], 1.0)
    future = replace(bar, metadata=replace(bar.metadata, received_at_ms=NOW + 86_400_000))
    wrong_source = replace(bar, metadata=replace(bar.metadata, provider="kraken"))
    wrong_close = replace(bar, close_time_ms=bar.close_time_ms - 1)
    _, dropped, _ = service._insert_candles(instrument, "5m", [future, wrong_source, wrong_close], NOW)
    assert dropped == 3 and not service._candles
    future_book_meta = replace(bar.metadata, received_at_ms=NOW + 86_400_000, exchange_timestamp_ms=NOW + 86_400_000)
    data.latest_l2[instrument.instrument_id] = NormalizedL2(
        future_book_meta, (NormalizedBookLevel(1.0, 100),), (NormalizedBookLevel(1.001, 100),)
    )
    assert service._book_metrics(instrument, NOW)["status"] == "STALE"


def test_removed_instrument_is_pruned_from_all_ephemeral_caches_through_churn():
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, _ = make_market_data(instrument)
    service = MultiAssetAnalysisService(data)
    asyncio.run(service.refresh_once())
    service._funding[instrument.instrument_id] = (None, NOW)
    service._open_interest[instrument.instrument_id] = (None, NOW)
    service._fetch_status[(instrument.instrument_id, "5m")] = {"status": "FRESH"}
    current = instrument
    for index in range(5):
        data.instruments.upsert(replace(current, active=False))
        replacement = make_instrument("binance", "spot", f"COIN{index}", "USDT", f"COIN{index}USDT")
        data.instruments.upsert(replacement)
        data.membership = (UniverseMembership(
            replacement.instrument_id, replacement.canonical_asset_id,
            UniverseBucket.LIQUID_SPOT_CANDIDATES, 1.0, True,
        ),)
        assert service.markets() == []
        assert service.get(current.instrument_id) is None
        assert not service._candles and not service._cards and not service._funding and not service._open_interest
        assert not service._fetch_status
        current = replacement


def test_instrument_compute_exception_is_reported_without_stopping_neighbor():
    broken = make_instrument("hyperliquid", "perpetual", "FAIL", "USDC", "FAIL", MarketType.PERPETUAL)
    healthy = make_instrument("kraken", "spot", "PEPE", "USD", "PEPE/USD")
    d1, p1 = make_market_data(broken)
    d2, p2 = make_market_data(healthy)
    pairs = [(inst, prov, d.truth_engine.latest_bbo(inst.instrument_id), d.latest_l2[inst.instrument_id])
             for d, prov, inst in ((d1, p1, broken), (d2, p2, healthy))]
    data = MarketData(pairs)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 2})
    original = service.analyze_instrument
    async def injected(instrument_id):
        if instrument_id == broken.instrument_id:
            raise RuntimeError("injected compute failure")
        return await original(instrument_id)
    service.analyze_instrument = injected
    health = asyncio.run(service.refresh_once())
    assert service.get(healthy.instrument_id)["observation_status"] == "OBSERVED"
    assert service.get(broken.instrument_id)["observation_status"] == "DEGRADED"
    assert health["failures"] >= 1 and health["status"] == "DEGRADED"


def test_causal_observations_are_immutable_deduplicated_and_survive_restart(tmp_path):
    instrument = make_instrument("binance", "spot", "BONK", "USDT", "BONKUSDT")
    data, _ = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "observations.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1}, repository)
    service._now_ms = lambda: NOW
    asyncio.run(service.refresh_once())
    assert repository.analysis_performance(NOW)["observations"] == 1, (service.health(), service._instrument_errors)
    first = storage.query("SELECT * FROM analysis_observations")[0]
    assert first["reference_time_semantics"] == "FRESH_BBO_MID_EXCHANGE_AND_RECEIVE"
    assert first["instrument_id"] == instrument.instrument_id
    assert (first["provider_id"], first["venue"], first["market_type"], first["base"], first["quote"], first["venue_symbol"]) == (
        instrument.provider, instrument.venue, instrument.market_type.value, instrument.base, instrument.quote, instrument.venue_symbol
    )
    assert first["chain"] is None and first["contract_address"] is None
    assert first["reference_price"] == pytest.approx(1.0005)
    assert first["baseline_direction"] == 1
    import json
    assert json.loads(first["context_json"])["status"] == "NO_VERIFIED_CONTEXT"
    lineage = json.loads(first["source_lineage_json"])
    assert lineage["bbo"]["available_at_ms"] == NOW
    assert all(item["available_at_ms"] <= NOW for item in lineage["candles"].values())
    assert storage.query("SELECT COUNT(*) AS n FROM analysis_outcomes")[0]["n"] == 3
    row = dict(first)
    row["features_json"] = "{\"tampered\":true}"
    row["costs"] = service.costs
    assert repository.save_analysis_observation(row) is False
    assert storage.query("SELECT features_json FROM analysis_observations")[0]["features_json"] == first["features_json"]
    storage.close()
    reopened = SQLiteStorage(tmp_path / "observations.db")
    assert MarketRepository(reopened).analysis_performance(NOW)["observations"] == 1
    assert reopened.health()["integrity"] == "ok"
    reopened.close()


def test_future_bbo_receive_and_event_times_are_excluded_from_snapshot(tmp_path):
    instrument = make_instrument("binance", "spot", "WIF", "USDT", "WIFUSDT")
    data, _ = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "causal.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {}, repository)
    service._now_ms = lambda: NOW
    card = asyncio.run(service.analyze_instrument(instrument.instrument_id))
    meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id, NOW + 1, NOW + 1, "future")
    data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(meta, 1.0, 1.001, 1, 1)
    assert service._save_observation(instrument, card, NOW)
    saved = storage.query("SELECT reference_price,reference_received_at_ms FROM analysis_observations")[0]
    assert saved["reference_price"] is None and saved["reference_received_at_ms"] is None
    storage.close()


def test_receive_only_reference_is_explicit_and_future_features_are_excluded(tmp_path):
    instrument = make_instrument("coinbase", "spot", "BONK", "USD", "BONK-USD")
    data, _ = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "receive-only.db")
    service = MultiAssetAnalysisService(data, {}, MarketRepository(storage))
    service._now_ms = lambda: NOW
    card = asyncio.run(service.analyze_instrument(instrument.instrument_id))

    # A provider with no exchange event clock is admissible only as explicitly
    # receive-time-only; an old timestamp value is not promoted to event time.
    fallback_meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
        NOW - 200_000, NOW, "public-ticker", exchange_time_kind="RECEIVE_TIME_FALLBACK")
    data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(fallback_meta, 1.0, 1.001, 1, 1)
    assert service._save_observation(instrument, card, NOW)
    saved = storage.query("SELECT * FROM analysis_observations")[0]
    assert saved["reference_time_semantics"] == "FRESH_BBO_MID_RECEIVE_TIME_ONLY"
    assert saved["reference_exchange_timestamp_ms"] is None

    stale_meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
        NOW - 200_000, NOW, "stale-event", exchange_time_kind="EXCHANGE_EVENT")
    data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(stale_meta, 1.0, 1.001, 1, 1)
    assert service._save_observation(instrument, card, NOW + service.observation_cadence_ms) is True
    second = storage.query("SELECT reference_price,reference_time_semantics FROM analysis_observations ORDER BY observed_at_ms DESC LIMIT 1")[0]
    assert second["reference_price"] is None
    assert second["reference_time_semantics"] == "UNAVAILABLE_NO_CAUSAL_FRESH_BBO"

    for kind in ("book", "funding", "open_interest"):
        assert service._causal_feature_view({"status": "FRESH", "exchange_time_kind": "EXCHANGE_EVENT",
            "exchange_timestamp_ms": NOW + 1, "received_at_ms": NOW, "value": 1}, NOW) is None, kind
    storage.close()


def test_persisted_causal_views_keep_features_lineage_and_quality_consistent(tmp_path):
    instrument = make_instrument("binance", "perpetual", "SYN", "USDT", "SYNUSDT", MarketType.PERPETUAL)
    data, _ = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "causal-views.db")
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1}, MarketRepository(storage))
    current = [NOW]
    service._now_ms = lambda: current[0]
    asyncio.run(service.refresh_once())
    card = dict(service._cards[instrument.instrument_id])
    assert all(card[kind]["status"] == "FRESH" for kind in ("book", "funding", "open_interest"))

    # Exchange timestamps at +1 ms are rejected consistently across features,
    # lineage and quality in the persisted SQLite snapshot.
    current[0] = NOW + service.observation_cadence_ms
    ref = data.truth_engine.latest_bbo(instrument.instrument_id)
    meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
        current[0], current[0], "snapshot-bbo")
    data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(meta, ref.bid, ref.ask, ref.bid_size, ref.ask_size)
    for kind in ("book", "funding", "open_interest"):
        card[kind] = {**card[kind], "exchange_time_kind": "EXCHANGE_EVENT",
            "exchange_timestamp_ms": current[0] + 1, "received_at_ms": current[0]}
    card["candles"] = {key: {**value, "latest_exchange_time_ms": current[0] + 1}
        for key, value in card["candles"].items()}
    card["candle_quality"] = {key: {**value, "received_at_ms": current[0]}
        for key, value in card["candle_quality"].items()}
    assert service._save_observation(instrument, card, current[0])
    future_row = storage.query("SELECT features_json,source_lineage_json,quality_json FROM analysis_observations WHERE observed_at_ms=?", (current[0],))[0]
    import json
    features, lineage, quality = (json.loads(future_row[key]) for key in ("features_json", "source_lineage_json", "quality_json"))
    future_ms = str(current[0] + 1)
    for kind in ("book", "funding", "open_interest"):
        assert features[kind] is None and lineage[kind] is None
        assert quality["feature_quality"][kind]["status"] == "CAUSALITY_REJECTED"
        assert "future_exchange_timestamp" in quality["feature_quality"][kind]["reason"]
    assert features["candles"] == {}
    assert all(item["status"] == "CAUSALITY_REJECTED" for item in quality["candle_quality"].values())
    assert all("future_candle_exchange_timestamp" in item["reason"] for item in quality["candle_quality"].values())
    assert future_ms not in future_row["features_json"] + future_row["source_lineage_json"] + future_row["quality_json"]
    assert quality["observation_status"] == "DEGRADED"

    # Receive-only data remains usable with explicit provenance and a NULL
    # exchange timestamp in both feature and lineage records.
    current[0] += service.observation_cadence_ms
    meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
        current[0] - 200_000, current[0], "snapshot-bbo", exchange_time_kind="RECEIVE_TIME_FALLBACK")
    data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(meta, ref.bid, ref.ask, ref.bid_size, ref.ask_size)
    fallback = {**card, "candles": {}}
    for kind in ("book", "funding", "open_interest"):
        fallback[kind] = {**card[kind], "exchange_time_kind": "RECEIVE_TIME_FALLBACK",
            "exchange_timestamp_ms": current[0] - 200_000, "received_at_ms": current[0]}
    assert service._save_observation(instrument, fallback, current[0])
    fallback_row = storage.query("SELECT features_json,source_lineage_json,quality_json FROM analysis_observations WHERE observed_at_ms=?", (current[0],))[0]
    ff, fl, fq = (json.loads(fallback_row[key]) for key in ("features_json", "source_lineage_json", "quality_json"))
    for kind in ("book", "funding", "open_interest"):
        assert ff[kind]["exchange_timestamp_ms"] is None
        assert fl[kind]["exchange_timestamp_ms"] is None
        assert fq["feature_quality"][kind]["status"] == "FRESH"
        assert fq["feature_quality"][kind]["exchange_time_kind"] == "RECEIVE_TIME_FALLBACK"
    storage.close()


def test_restart_empty_registry_resolves_exact_identity_and_isolates_failed_neighbor(tmp_path):
    bad = make_instrument("binance", "spot", "BAD", "USDT", "BADUSDT")
    good = make_instrument("binance", "spot", "GOOD", "USDT", "GOODUSDT")
    data_bad, provider = make_market_data(bad)
    data_good, _ = make_market_data(good)
    pairs = [(item, provider, data_bad.truth_engine.latest_bbo(item.instrument_id), data_bad.latest_l2[item.instrument_id])
             for item in (bad,)] + [
             (good, provider, data_good.truth_engine.latest_bbo(good.instrument_id), data_good.latest_l2[good.instrument_id])]
    data = MarketData(pairs)
    path = tmp_path / "restart-outcomes.db"
    storage = SQLiteStorage(path)
    repository = MarketRepository(storage)
    initial = MultiAssetAnalysisService(data, {"instruments_per_cycle": 2}, repository)
    initial._now_ms = lambda: NOW
    asyncio.run(initial.refresh_once())
    storage.close()

    reopened = SQLiteStorage(path)
    repository = MarketRepository(reopened)
    data.instruments = InstrumentRegistry()  # simulate a cold registry after restart
    current = [NOW + 900_010]
    initial._now_ms = lambda: current[0]
    calls = []

    async def target_bbo(instrument):
        calls.append(instrument.instrument_id)
        if instrument.base == "BAD":
            raise OSError("one exact market unavailable")
        target = NOW + 900_000
        meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
            target, target + 5, "target-bbo")
        return NormalizedBBO(meta, 1.01, 1.011, 1, 1)

    provider.fetch_bbo = target_bbo
    restored = MultiAssetAnalysisService(data, {"instruments_per_cycle": 2}, repository)
    restored._now_ms = lambda: current[0]
    asyncio.run(restored._process_due_outcomes(current[0]))
    statuses = {row["base"]: row["status"] for row in reopened.query(
        "SELECT a.base,o.status FROM analysis_outcomes o JOIN analysis_observations a USING(observation_id) WHERE o.horizon='15m'")}
    assert set(calls) == {bad.instrument_id, good.instrument_id}
    assert statuses == {"BAD": "PENDING", "GOOD": "OBSERVED"}
    current[0] = NOW + 900_000 + restored.outcome_max_latency_ms + 1
    asyncio.run(restored._process_due_outcomes(current[0]))
    statuses = {row["base"]: row["status"] for row in reopened.query(
        "SELECT a.base,o.status FROM analysis_outcomes o JOIN analysis_observations a USING(observation_id) WHERE o.horizon='15m'")}
    assert statuses == {"BAD": "UNAVAILABLE", "GOOD": "OBSERVED"}
    assert reopened.health()["integrity"] == "ok"
    reopened.close()


def test_thirty_market_four_hour_catchup_is_bounded_and_groups_horizons(tmp_path):
    instruments = [make_instrument("binance", "spot", f"M{i}", "USDT", f"M{i}USDT") for i in range(30)]
    pairs = []
    for instrument in instruments:
        data, provider = make_market_data(instrument)
        pairs.append((instrument, provider, data.truth_engine.latest_bbo(instrument.instrument_id),
                      data.latest_l2[instrument.instrument_id]))
    data = MarketData(pairs)
    storage = SQLiteStorage(tmp_path / "four-hour-catchup.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {}, repository)
    target = NOW
    card = {"candles": {}, "candle_quality": {}, "observation_status": "DEGRADED"}
    for instrument in instruments:
        for age in (14_400_000, 3_600_000, 900_000):
            observed = target - age
            service._now_ms = lambda observed=observed: observed
            meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                observed, observed, "snapshot-bbo")
            data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(meta, 1.0, 1.001, 1, 1)
            assert service._save_observation(instrument, card, observed)

    current = [target + 10]
    service._now_ms = lambda: current[0]
    provider = data.providers.get("binance")
    calls = []

    async def target_bbo(instrument):
        calls.append(instrument.instrument_id)
        meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
            target, target + 5, "target-bbo")
        return NormalizedBBO(meta, 1.0, 1.001, 1, 1)

    provider.fetch_bbo = target_bbo
    due = repository.due_analysis_outcomes(target + 10, 2_000)
    assert len(due) == 90  # three horizons per exact instrument, collapsed to 30 quote groups
    assert len({(row["instrument_id"], row["quote"], row["target_at_ms"]) for row in due}) == 30
    for _ in range(4):
        asyncio.run(service._process_due_outcomes(current[0]))
    assert len(calls) == 30 and set(calls) == {item.instrument_id for item in instruments}
    result = storage.query("SELECT status,COUNT(*) AS n FROM analysis_outcomes GROUP BY status")
    observed = next(row["n"] for row in result if row["status"] == "OBSERVED")
    unavailable = next(row["n"] for row in result if row["status"] == "UNAVAILABLE")
    assert observed == 90 and unavailable == 90

    health = service.health()
    assert health["outcome_request_demand_per_minute"] == pytest.approx(18.0)
    assert health["outcome_request_capacity_per_minute"] == pytest.approx(32.0)
    assert health["outcome_capacity_status"] == "ADEQUATE"
    assert storage.health()["integrity"] == "ok"
    storage.close()


def test_normal_refresh_keeps_thirty_market_outcomes_through_four_hours(tmp_path):
    class FakeClock:
        def __init__(self, value):
            self.value = value

        def now(self):
            return self.value

        def advance(self, milliseconds):
            self.value += milliseconds
            return self.value

    class AdvancingProvider(DerivativesDataProvider):
        provider_id = "binance"

        def __init__(self, clock):
            self.clock = clock
            self.candle_calls = 0
            self.funding_calls = 0
            self.oi_calls = 0
            self.bbo_calls = 0

        async def list_instruments(self):
            return instruments

        async def fetch_candles(self, instrument, interval, start_ms=None, limit=300):
            self.candle_calls += 1
            event_now = self.clock.now()
            await asyncio.sleep(0)
            received = self.clock.advance(2)
            if instrument.base == "BAD":
                raise OSError("synthetic one-market candle failure")
            duration = INTERVAL_MS[interval]
            last_open = (event_now // duration) * duration - duration
            return [candle(instrument, interval, last_open - offset * duration,
                100 + (79 - offset) * .01, received=received) for offset in reversed(range(80))]

        async def fetch_bbo(self, instrument):
            self.bbo_calls += 1
            event_time = self.clock.now()
            await asyncio.sleep(0)
            received = self.clock.advance(2)
            meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                event_time, received, "synthetic-target-bbo")
            return NormalizedBBO(meta, 100.0, 100.1, 20.0, 21.0)

        async def fetch_trades(self, instrument, limit=100):
            return []

        async def fetch_l2(self, instrument, depth=20):
            raise ProviderCapabilityError("not requested in this worker")

        async def fetch_funding(self, instrument):
            self.funding_calls += 1
            event_time = self.clock.now()
            await asyncio.sleep(0)
            received = self.clock.advance(2)
            meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                event_time, received, "synthetic-funding")
            return FundingSnapshot(meta, 0.0001, event_time + 8 * 3_600_000)

        async def fetch_open_interest(self, instrument):
            self.oi_calls += 1
            event_time = self.clock.now()
            await asyncio.sleep(0)
            received = self.clock.advance(2)
            meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                event_time, received, "synthetic-open-interest")
            return OpenInterestSnapshot(meta, 100.0, 10_000.0)

        async def fetch_liquidations(self, instrument, limit=100):
            return []

        async def fetch_mark_index(self, instrument):
            raise ProviderCapabilityError("not requested in this worker")

        def health(self):
            return ProviderHealth("binance", ProviderStatus.HEALTHY, self.clock.now())

        async def close(self):
            return None

    start = NOW
    clock = FakeClock(start)
    instruments = [make_instrument("binance", "usdm", "BAD" if i == 0 else f"PERP{i}",
        "USDT", "BADUSDT" if i == 0 else f"PERP{i}USDT", MarketType.PERPETUAL) for i in range(30)]
    provider = AdvancingProvider(clock)
    pairs = []
    for instrument in instruments:
        meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
            start, start, "synthetic-live-bbo")
        bbo = NormalizedBBO(meta, 100.0, 100.1, 20.0, 21.0)
        book = NormalizedL2(meta, (NormalizedBookLevel(100.0, 20.0),), (NormalizedBookLevel(100.1, 21.0),))
        pairs.append((instrument, provider, bbo, book))
    data = MarketData(pairs)
    storage = SQLiteStorage(tmp_path / "normal-four-hour.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {}, repository)
    service._now_ms = clock.now
    full_health = service.health
    # This stress test measures the normal refresh/request path. The cached
    # full-card health projection is asserted once after fake-time catch-up.
    service.health = lambda: {"status": "SYNTHETIC_TEST"}
    assert service.poll_seconds == 15
    assert service.observation_cadence_ms == 300_000
    assert service.max_requests_per_cycle == 30 and service.outcome_requests_per_cycle == 8

    async def run_fake_service():
        cycles = 0
        overdue_samples = []
        target_duration = 4 * 3_600_000 + 180_000  # >4h plus the full 60s outcome window
        while clock.now() - start <= target_duration:
            sampled = clock.now()
            for instrument in instruments:
                meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                    sampled, sampled, "synthetic-live-feed")
                data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(meta, 100.0, 100.1, 20.0, 21.0)
                data.latest_l2[instrument.instrument_id] = NormalizedL2(
                    meta, (NormalizedBookLevel(100.0, 20.0),), (NormalizedBookLevel(100.1, 21.0),))
            await service.refresh_once()
            assert service._requests <= 30
            cycles += 1
            if cycles % 60 == 0:
                now = clock.now()
                matured_pending = storage.query(
                    "SELECT COUNT(*) AS n FROM analysis_outcomes WHERE status='PENDING' AND target_at_ms<=? AND deadline_at_ms<?",
                    (now, now))[0]["n"]
                overdue_samples.append(matured_pending)
                assert matured_pending == 0
            clock.advance(15_000)
        return cycles, overdue_samples

    cycles, overdue_samples = asyncio.run(run_fake_service())
    assert cycles > 4 * 3600 / 15
    assert overdue_samples and max(overdue_samples) == 0
    assert provider.candle_calls > 0 and provider.funding_calls > 0 and provider.oi_calls > 0
    assert provider.bbo_calls > 0  # target quotes were fetched after await and advanced fake time

    counts = {row["horizon"]: row["n"] for row in storage.query(
        "SELECT horizon,COUNT(*) AS n FROM analysis_outcomes WHERE status='OBSERVED' GROUP BY horizon")}
    assert all(counts.get(horizon, 0) > 0 for horizon in ("15m", "1h", "4h"))
    observations = storage.query("SELECT COUNT(*) AS n FROM analysis_observations")[0]["n"]
    outcomes = storage.query("SELECT COUNT(*) AS n FROM analysis_outcomes")[0]["n"]
    assert observations > 30 and outcomes == observations * 3
    async def drain_currently_matured():
        # The duration loop can end just after its final 15s tick. Drain only
        # outcomes already mature at that cutoff through normal refresh cycles.
        for _ in range(12):
            now = clock.now()
            matured = storage.query(
                "SELECT COUNT(*) AS n FROM analysis_outcomes WHERE status='PENDING' AND target_at_ms<=?", (now,))[0]["n"]
            if not matured:
                return
            sampled = clock.now()
            for instrument in instruments:
                meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                    sampled, sampled, "synthetic-live-feed")
                data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(meta, 100.0, 100.1, 20.0, 21.0)
                data.latest_l2[instrument.instrument_id] = NormalizedL2(
                    meta, (NormalizedBookLevel(100.0, 20.0),), (NormalizedBookLevel(100.1, 21.0),))
            await service.refresh_once()
            assert service._requests <= 30
            clock.advance(15_000)
        raise AssertionError("matured outcome queue did not drain within 12 normal cycles")

    asyncio.run(drain_currently_matured())
    now = clock.now()
    matured_pending = storage.query(
        "SELECT COUNT(*) AS n FROM analysis_outcomes WHERE status='PENDING' AND target_at_ms<=?", (now,))[0]["n"]
    pending_rows = storage.query("SELECT a.base,o.horizon,o.target_at_ms,o.deadline_at_ms FROM analysis_outcomes o JOIN analysis_observations a USING(observation_id) WHERE o.status='PENDING' AND o.target_at_ms<=? ORDER BY o.deadline_at_ms LIMIT 40", (now,))
    assert matured_pending == 0, pending_rows
    rejected_deadline = storage.query(
        "SELECT COUNT(*) AS n FROM analysis_outcomes WHERE status='UNAVAILABLE' AND reason LIKE '%deadline%' ")[0]["n"]
    deadline_rows = storage.query("SELECT a.base,o.horizon,o.target_at_ms,o.deadline_at_ms,o.updated_at_ms,o.reason FROM analysis_outcomes o JOIN analysis_observations a USING(observation_id) WHERE o.status='UNAVAILABLE' AND o.reason LIKE '%deadline%' ORDER BY o.target_at_ms LIMIT 30")
    deadline_summary = storage.query("SELECT o.horizon,COUNT(*) AS n,MIN(o.target_at_ms) AS first_target,MAX(o.target_at_ms) AS last_target,MAX(o.updated_at_ms) AS finalized_at FROM analysis_outcomes o WHERE o.status='UNAVAILABLE' AND o.reason LIKE '%deadline%' GROUP BY o.horizon")
    assert rejected_deadline == 0, {"rows": deadline_rows, "summary": deadline_summary,
        "requests_per_cycle_limit": service.max_requests_per_cycle, "outcome_groups_attempted": getattr(service, "_outcome_groups_attempted", 0)}
    bad_analysis = storage.query("SELECT COUNT(*) AS n FROM analysis_observations WHERE base='BAD'")[0]["n"]
    good_assets = storage.query("SELECT COUNT(DISTINCT base) AS n FROM analysis_observations WHERE base<>'BAD'")[0]["n"]
    good_analysis = storage.query("SELECT COUNT(*) AS n FROM analysis_observations WHERE base<>'BAD'")[0]["n"]
    assert bad_analysis > 0 and good_analysis > bad_analysis
    assert good_assets == 29
    assert storage.health()["integrity"] == "ok"
    service.health = full_health
    assert service.health()["outcome_capacity_status"] == "ADEQUATE"
    storage.close()


def test_outcomes_use_exact_horizon_quote_and_separate_gross_from_cost_adjusted_return(tmp_path):
    instrument = make_instrument("binance", "spot", "PEPE", "USDT", "PEPEUSDT")
    data, provider = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "outcomes.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1}, repository)
    current = [NOW]
    service._now_ms = lambda: current[0]
    asyncio.run(service.refresh_once())
    # Simulate a runtime config change after immutable snapshot creation.
    service.costs = {**service.costs, "fee_bps_per_side": 40.0}
    for horizon, delta in (("15m", 900_000), ("1h", 3_600_000), ("4h", 14_400_000)):
        target = NOW + delta
        async def target_quote(_instrument, target=target):
            await asyncio.sleep(0.01)
            current[0] = target + 50
            target_meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
                target, target + 50, "target-bbo")
            return NormalizedBBO(target_meta, 1.01, 1.011, 1, 1)
        provider.fetch_bbo = target_quote
        current[0] = target
        asyncio.run(service._process_due_outcomes(target))
        completed = storage.query("SELECT status,target_received_at_ms FROM analysis_outcomes WHERE horizon=?", (horizon,))[0]
        assert completed["status"] == "OBSERVED" and completed["target_received_at_ms"] == target + 50
    result = storage.query("SELECT * FROM analysis_outcomes WHERE horizon='15m'")[0]
    assert result["status"] == "OBSERVED"
    assert result["target_exchange_timestamp_ms"] == NOW + 900_000
    assert result["gross_reference_return"] > 0
    assert result["costs_adjusted_observational_return"] < result["gross_reference_return"]
    cost_row_raw = storage.query("SELECT cost_assumptions_json,observed_costs_json FROM analysis_outcomes WHERE horizon='15m'")[0]
    costs = cost_row_raw["cost_assumptions_json"]
    assert "half_entry_plus_half_exit_quoted_spread_once" in costs
    import json
    frozen_policy = json.loads(costs)
    cost_row = json.loads(cost_row_raw["observed_costs_json"])
    expected_roundtrip = (storage.query("SELECT reference_spread_bps FROM analysis_observations")[0]["reference_spread_bps"] + 10000 * (.001 / 1.0105)) / 2
    assert cost_row["roundtrip_spread_bps"] == pytest.approx(expected_roundtrip)
    assert cost_row["roundtrip_fee_bps"] == pytest.approx(8)
    assert cost_row["total_roundtrip_cost_bps"] == pytest.approx(expected_roundtrip + 2 * 4 + 2 * 2)
    assert frozen_policy["fee_bps_per_side"] == 4
    assert storage.query("SELECT status FROM analysis_outcomes WHERE horizon='1h'")[0]["status"] == "OBSERVED"
    storage.close()


def test_zero_gross_still_pays_costs_and_no_position_is_not_a_zero_return_trade(tmp_path):
    instrument = make_instrument("binance", "spot", "ZERO", "USDT", "ZEROUSDT")
    data, provider = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "zero-outcome.db")
    repository = MarketRepository(storage)
    current = [NOW]
    service = MultiAssetAnalysisService(data, {"instruments_per_cycle": 1}, repository)
    service._now_ms = lambda: current[0]
    asyncio.run(service.refresh_once())
    snapshot = storage.query("SELECT * FROM analysis_observations")[0]
    assert snapshot["baseline_direction"] == 1

    # A later tied-trend snapshot has no baseline position. It must remain a
    # no-position cohort, not be counted as a zero-return trade.
    second_time = NOW + service.observation_cadence_ms
    current[0] = second_time
    old_bbo = data.truth_engine.latest_bbo(instrument.instrument_id)
    meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
        second_time, second_time, "snapshot-bbo")
    data.truth_engine.bbo[instrument.instrument_id] = NormalizedBBO(meta, old_bbo.bid, old_bbo.ask, 1, 1)
    card = dict(service._cards[instrument.instrument_id])
    trends = list(card["candles"].values())
    assert len(trends) >= 2
    card["candles"] = {"a": {**trends[0], "trend": "UP"}, "b": {**trends[1], "trend": "DOWN"}}
    assert service._save_observation(instrument, card, second_time)
    second = storage.query("SELECT observation_id,baseline_direction FROM analysis_observations ORDER BY observed_at_ms DESC LIMIT 1")[0]
    assert second["baseline_direction"] == 0

    async def unchanged_quote(_instrument):
        target = NOW + 900_000
        received = current[0]
        quote_meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
            target, received, "target-bbo")
        return NormalizedBBO(quote_meta, 1.0, 1.001, 1, 1)

    provider.fetch_bbo = unchanged_quote
    current[0] = NOW + 900_010
    asyncio.run(service._process_due_outcomes(current[0]))
    first = storage.query("SELECT * FROM analysis_outcomes WHERE observation_id=? AND horizon='15m'", (snapshot["observation_id"],))[0]
    assert first["gross_reference_return"] == pytest.approx(0.0)
    assert first["baseline_gross_return"] == pytest.approx(0.0)
    assert first["long_reference_net_return"] < 0
    assert first["baseline_costs_adjusted_return"] < 0

    # The second snapshot's BBO retains its own exact causal reference.
    second_target = second_time + 900_000
    current[0] = second_target + 10
    async def second_quote(_instrument):
        quote_meta = EventMetadata(instrument.provider, instrument.venue, instrument.instrument_id,
            second_target, current[0], "target-bbo")
        return NormalizedBBO(quote_meta, 1.0, 1.001, 1, 1)
    provider.fetch_bbo = second_quote
    asyncio.run(service._process_due_outcomes(current[0]))
    no_position = storage.query("SELECT * FROM analysis_outcomes WHERE observation_id=? AND horizon='15m'", (second["observation_id"],))[0]
    assert no_position["baseline_direction"] == 0
    assert no_position["baseline_gross_return"] == pytest.approx(0.0)
    assert no_position["baseline_costs_adjusted_return"] is None
    cohort = repository.analysis_performance(current[0])["cohorts"]
    assert cohort[0]["baseline_position_n"] == 1 and cohort[0]["baseline_no_position_n"] == 1
    assert cohort[0]["zero_return_n"] == 2
    storage.close()


def test_late_target_quote_is_terminal_unavailable_and_telegram_reports_observation_status(tmp_path):
    instrument = make_instrument("kraken", "spot", "MOG", "USD", "MOG/USD")
    data, _ = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "late.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {}, repository)
    service._now_ms = lambda: NOW
    asyncio.run(service.refresh_once())
    due = NOW + 900_000 + service.outcome_max_latency_ms + 1
    asyncio.run(service._process_due_outcomes(due))
    row = storage.query("SELECT status,reason FROM analysis_outcomes WHERE horizon='15m'")[0]
    assert row["status"] == "UNAVAILABLE" and row["reason"] == "target_quote_deadline_missed"
    analyzer = SimpleNamespace(telegram=object(), repository=repository, market_analysis=service)
    command = TelegramCommandService(analyzer)
    status = asyncio.run(command.handle("/analysis_status"))[0]
    performance = asyncio.run(command.handle("/analysis_performance"))[0]
    assert "snapshots: 1" in status and "NOT_EVALUABLE" in status
    # No outcome was observed, so this cohort must expose zero metric samples
    # instead of printing an average or implying evidence exists.
    assert "NOT_EVALUABLE" in performance and "sample n gross/long-net=0/0" in performance
    assert repository.finish_analysis_outcome(
        storage.query("SELECT observation_id FROM analysis_outcomes WHERE horizon='15m'")[0]["observation_id"],
        "15m", {"status": "OBSERVED", "updated_at_ms": due, "costs": {}},
    ) is False
    storage.close()


def test_pending_outcome_survives_universe_removal_and_delisting_is_explicit(tmp_path):
    instrument = make_instrument("kraken", "spot", "TURBO", "USD", "TURBO/USD")
    data, provider = make_market_data(instrument)
    storage = SQLiteStorage(tmp_path / "delisted.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {}, repository)
    service._now_ms = lambda: NOW
    asyncio.run(service.refresh_once())
    # Membership removal alone does not erase pending cohort rows.
    data.membership = ()
    assert repository.due_analysis_outcomes(NOW + 900_000, 5)
    data.instruments.upsert(replace(instrument, active=False))
    asyncio.run(service._process_due_outcomes(NOW + 900_000))
    outcome = storage.query("SELECT status,reason FROM analysis_outcomes WHERE horizon='15m'")[0]
    assert outcome == {"status": "UNAVAILABLE", "reason": "instrument_inactive_or_delisted"}
    cohort = repository.analysis_performance(NOW + 900_000)["cohorts"]
    assert cohort[0]["denominator"] == 1 and cohort[0]["delisted"] == 1
    assert provider.calls  # candle data had been obtained before removal
    storage.close()


def test_missing_reference_bbo_is_unavailable_without_market_request(tmp_path):
    instrument = make_instrument("binance", "spot", "CAT", "USDT", "CATUSDT")
    data, provider = make_market_data(instrument)
    data.truth_engine.bbo.clear()
    storage = SQLiteStorage(tmp_path / "missing-reference.db")
    repository = MarketRepository(storage)
    service = MultiAssetAnalysisService(data, {}, repository)
    service._now_ms = lambda: NOW
    card = asyncio.run(service.analyze_instrument(instrument.instrument_id))
    assert service._save_observation(instrument, card, NOW)
    asyncio.run(service._process_due_outcomes(NOW + 900_000))
    outcome = storage.query("SELECT status,reason FROM analysis_outcomes WHERE horizon='15m'")[0]
    assert outcome == {"status": "UNAVAILABLE", "reason": "missing_fresh_reference_bbo"}
    assert not provider.calls
    storage.close()


def test_v12_migration_adds_observation_tables_without_rewriting_legacy_rows(tmp_path):
    path = tmp_path / "v12.db"
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at INTEGER NOT NULL);
        INSERT INTO schema_migrations VALUES(12, 1700000000000);
        CREATE TABLE signals(id INTEGER PRIMARY KEY, coin TEXT NOT NULL, timestamp INTEGER NOT NULL,
          primary_horizon TEXT NOT NULL, direction TEXT NOT NULL, probability REAL NOT NULL,
          payload_json TEXT NOT NULL, model_status TEXT NOT NULL);
        INSERT INTO signals VALUES(1,'SOL',1700000000000,'15m','WAIT',50,'{}','MODEL_WARMING_UP');
    """)
    connection.commit()
    connection.close()
    storage = SQLiteStorage(path)
    assert storage.health()["schema_version"] == 13
    assert storage.query("SELECT id,coin,direction FROM signals") == [{"id": 1, "coin": "SOL", "direction": "WAIT"}]
    assert storage.query("SELECT COUNT(*) AS n FROM analysis_observations")[0]["n"] == 0
    assert storage.health()["integrity"] == "ok"
    storage.close()
