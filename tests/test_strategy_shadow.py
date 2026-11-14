import asyncio
import json
from types import SimpleNamespace

from src.service.strategy_shadow import StrategyShadow
from src.service.market_analysis import MultiAssetAnalysisService
from src.data.storage import SQLiteStorage
from src.providers.types import EventMetadata, Instrument, MarketType, NormalizedBBO
from src.alerts.commands import TelegramCommandService


def valid_card(now=1_800_000_000_000, iid="binance:spot:SOL-USDT"):
    return {"instrument_id": iid, "provider": "binance", "venue": "binance-spot",
        "market_type": "spot", "quote": "USDT", "observed_at_ms": now,
        "candles": {tf: {"trend": "UP", "realized_return_volatility_pct_per_bar": .4,
            "latest_exchange_time_ms": now-1000, "source_provider": "binance", "source_venue": "binance-spot"} for tf in ("15m", "1h", "4h")},
        "candle_quality": {tf: {"status": "FRESH", "received_at_ms": now-1000} for tf in ("15m", "1h", "4h")},
        "bbo": {"source_provider": "binance", "source_venue": "binance-spot",
            "received_at_ms": now-1000, "exchange_time_kind": "RECEIVE_TIME_FALLBACK",
            "bid": 99.9, "ask": 100.1}}


def later_quote(now, bid=100, ask=100.1):
    return {"instrument_id": "binance:spot:SOL-USDT", "provider": "binance", "venue": "binance-spot",
        "quote": "USDT", "market_type": "spot", "received_at_ms": now,
        "exchange_time_kind": "RECEIVE_TIME_FALLBACK", "bid": bid, "ask": ask}


def test_shadow_variants_persist_exact_identity_and_do_not_claim_probability(tmp_path):
    db = SQLiteStorage(tmp_path / "shadow.db")
    svc = StrategyShadow(db)
    card = {
        "instrument_id": "binance:spot:SOL-USDT", "provider": "binance", "venue": "binance-spot", "market_type": "spot", "quote": "USDT",
        "observed_at_ms": 1_800_000_000_000,
        "candles": {tf: {"trend": "UP", "realized_return_volatility_pct_per_bar": .4, "latest_exchange_time_ms": 1_799_999_999_000, "source_provider": "binance", "source_venue": "binance-spot"}
                    for tf in ("15m", "1h", "4h")},
        "candle_quality": {tf: {"status": "FRESH", "received_at_ms": 1_799_999_999_000} for tf in ("15m", "1h", "4h")},
        "bbo": {"source_provider": "binance", "source_venue": "binance-spot", "received_at_ms": 1_799_999_999_000,
                "exchange_time_kind": "RECEIVE_TIME_FALLBACK", "bid": 99.9, "ask": 100.1},
    }
    plans = svc.evaluate(card)
    assert [p.variant for p in plans] == ["baseline", "disciplined"]
    assert all(p.instrument_id == card["instrument_id"] and p.direction == "LONG" for p in plans)
    assert all(p.semantics.startswith("heuristic") and p.status == "PROPOSED" for p in plans)
    assert svc.evaluate(card) == plans
    assert svc.summary()["baseline"]["plans"] == 1
    db.close()


def test_frozen_full_notional_fees_and_no_backward_close(tmp_path):
    db = SQLiteStorage(tmp_path / "fees.db")
    now = 1_800_000_000_000
    svc = StrategyShadow(db, {"fee_bps_per_side": 4, "slippage_bps_per_side": 2})
    plans = svc.evaluate(valid_card(now))
    wrong_quote = later_quote(now+500)
    wrong_quote["quote"] = "USD"
    assert svc.advance(wrong_quote, now+500)["open_positions"] == []
    svc.advance(later_quote(now+1000), now+1000)
    positions = svc.open_positions()
    assert len(positions) == 2
    assert all(abs(x["entry"]["fees"]-x["plan"]["notional"]*4/10000) < 1e-10 for x in positions)
    svc.advance(later_quote(now+1500), now+1500)
    assert len(svc.advance(later_quote(now+1400, bid=102, ask=102.1), now+1500)["open_positions"]) == 2
    quote = later_quote(now+2000, bid=102, ask=102.1)
    quote.update(exchange_time_kind="EXCHANGE_EVENT", exchange_timestamp_ms=now+500)
    assert StrategyShadow(db, {"fee_bps_per_side": 40, "slippage_bps_per_side": 20}).advance(quote, now+2000)["open_positions"]
    quote["exchange_timestamp_ms"] = now+2000
    StrategyShadow(db, {"fee_bps_per_side": 40, "slippage_bps_per_side": 20}).advance(quote, now+2000)
    assert not svc.open_positions()
    closed = [json.loads(r["payload_json"]) for r in db.query("SELECT payload_json FROM strategy_shadow_events WHERE event_type='CLOSED'")]
    assert len(closed) == 2
    assert all(c["fees"] < .11 and c["net_pnl"] > 0 for c in closed)
    assert all(p.fee_bps_per_side == 4 for p in plans)
    db.execute("DELETE FROM strategy_shadow_state")
    assert StrategyShadow(db).summary(now+3000)["baseline"]["closed"] == 1
    db.close()


def test_no_trade_never_fills_or_expires_and_risk_budget_rejects(tmp_path):
    db = SQLiteStorage(tmp_path / "gates.db")
    now = 1_800_000_000_000
    svc = StrategyShadow(db)
    bad = valid_card(now)
    bad["quote"] = "BTC"
    assert all(p.status == "NO_TRADE" for p in svc.evaluate(bad))
    svc.advance(later_quote(now+120000), now+120000)
    assert db.query("SELECT event_type FROM strategy_shadow_events") == []
    future = valid_card(now+3_600_000)
    future["candle_quality"]["1h"]["received_at_ms"] = now+3_600_001
    assert all(p.status == "NO_TRADE" for p in StrategyShadow(db).evaluate(future))
    fresh = valid_card(now+7_200_000)
    budgeted = StrategyShadow(db, {"initial_equity": 1000, "max_total_risk_fraction": .0001})
    assert all(p.status == "PROPOSED" for p in budgeted.evaluate(fresh))
    budgeted.advance(later_quote(now+7_201_000), now+7_201_000)
    assert budgeted.summary()["baseline"]["rejected"] == 1
    db.close()


def test_closed_bar_rearm_and_cooldown_are_persisted(tmp_path):
    db = SQLiteStorage(tmp_path / "cooldown.db")
    now = 1_800_000_000_000
    svc = StrategyShadow(db, {"cooldown_seconds": 7200})
    first = svc.evaluate(valid_card(now))
    svc.advance(later_quote(now+1000), now+1000)
    svc.advance(later_quote(now+2000, 102, 102.1), now+2000)
    assert [p.plan_id for p in svc.evaluate(valid_card(now))] == [p.plan_id for p in first]
    later = valid_card(now+3_600_000)
    result = StrategyShadow(db, {"cooldown_seconds": 0}).evaluate(later)
    assert result[1].status == "NO_TRADE" and "cooldown" in result[1].reasons[-1]
    db.close()


def test_normal_refresh_monitors_removed_deep_instrument(tmp_path):
    db = SQLiteStorage(tmp_path / "worker.db")
    now = 1_800_000_000_000
    strategy = StrategyShadow(db)
    strategy.evaluate(valid_card(now))
    instrument = Instrument("binance:spot:SOL-USDT", "unresolved:binance:SOL:USDT", "binance",
        "binance-spot", MarketType.SPOT, "SOL", "USDT", "SOLUSDT", 2, 3)
    class Provider:
        async def fetch_bbo(self, _instrument):
            meta = EventMetadata("binance", "binance-spot", instrument.instrument_id,
                now+1000, now+1000, "synthetic")
            return NormalizedBBO(meta, 100, 100.1, 1, 1)
    class Registry:
        def get(self, _iid): return instrument
    class Providers:
        def get(self, _provider): return Provider()
    class MarketData:
        membership = ()  # The instrument has left the deep universe.
        instruments = Registry()
        providers = Providers()
        config = {"providers": {"binance": {"request_timeout_seconds": 1}}}
        async def _provider_request(self, _provider, operation): return await operation()
    analysis = MultiAssetAnalysisService(MarketData(), {"maximum_requests_per_cycle": 4})
    analysis.strategy_shadow = strategy
    analysis._now_ms = lambda: now+1000
    asyncio.run(analysis.refresh_once())
    assert len(strategy.open_positions()) == 2
    assert analysis._requests == 1
    db.close()


def test_normal_refresh_reserves_open_and_pending_monitor_slots(tmp_path):
    db = SQLiteStorage(tmp_path / "monitor_fairness.db")
    now = 1_800_000_000_000
    strategy = StrategyShadow(db)
    open_ids = [f"binance:spot:OPEN{i}-USDT" for i in range(3)]
    pending_ids = [f"binance:spot:PENDING{i}-USDT" for i in range(3)]
    for iid in open_ids + pending_ids:
        strategy.evaluate(valid_card(now, iid))
    for iid in open_ids:
        quote = later_quote(now + 1000)
        quote["instrument_id"] = iid
        strategy.advance(quote, now + 1000)

    fetched = []
    class Provider:
        async def fetch_bbo(self, instrument):
            fetched.append(instrument.instrument_id)
            meta = EventMetadata("binance", "binance-spot", instrument.instrument_id,
                now + 2000, now + 2000, "synthetic")
            return NormalizedBBO(meta, 100, 100.1, 1, 1)
    class Registry:
        def get(self, iid):
            symbol = iid.split(":")[-1].split("-")[0]
            return Instrument(iid, f"unresolved:binance:{symbol}:USDT", "binance",
                "binance-spot", MarketType.SPOT, symbol, "USDT", symbol + "USDT", 2, 3)
    class Providers:
        def get(self, _provider): return Provider()
    class MarketData:
        membership = ()
        instruments = Registry()
        providers = Providers()
        config = {"providers": {"binance": {"request_timeout_seconds": 1}}}
        async def _provider_request(self, _provider, operation): return await operation()
    analysis = MultiAssetAnalysisService(MarketData(), {"maximum_requests_per_cycle": 4,
        "shadow_monitor_requests_per_cycle": 2})
    analysis.strategy_shadow = strategy
    analysis._now_ms = lambda: now + 2000
    asyncio.run(analysis.refresh_once())
    assert len(fetched) == 2
    assert len(set(fetched) & set(open_ids)) == 1
    assert len(set(fetched) & set(pending_ids)) == 1
    assert analysis._requests == 2
    db.close()


def test_shadow_report_exposes_quote_sampling_gap(tmp_path):
    db = SQLiteStorage(tmp_path / "sampling.db")
    now = 1_800_000_000_000
    strategy = StrategyShadow(db)
    strategy.evaluate(valid_card(now))
    strategy.advance(later_quote(now + 1000), now + 1000)
    report = strategy.summary(now + 90_000)
    assert report["baseline"]["unknown_marks"] == 1
    assert report["baseline"]["by_quote"]["USDT"]["mark_count"] == 0
    assert report["baseline"]["by_quote"]["USDT"]["max_quote_gap_seconds"] == 89
    db.close()


def test_paired_comparison_keeps_abstentions_out_of_trade_delta(tmp_path):
    db = SQLiteStorage(tmp_path / "paired.db")
    now = 1_800_000_000_000
    strategy = StrategyShadow(db)
    strategy.evaluate(valid_card(now))
    strategy.advance(later_quote(now + 1000), now + 1000)
    strategy.advance(later_quote(now + 2000, bid=102, ask=102.1), now + 2000)
    second = valid_card(now + 3_600_000)
    second["candles"]["15m"]["trend"] = "DOWN"
    decisions = strategy.evaluate(second)
    assert [plan.status for plan in decisions] == ["PROPOSED", "NO_TRADE"]
    strategy.advance(later_quote(now + 3_601_000), now + 3_601_000)
    strategy.advance(later_quote(now + 3_602_000, bid=102, ask=102.1), now + 3_602_000)
    metrics = strategy.paired_comparison()["by_quote"]["USDT"]
    assert metrics["paired_decisions"] == 2
    assert metrics["both_proposed"] == metrics["both_closed"] == 1
    assert metrics["paired_disciplined_minus_baseline_net"] == 0
    assert metrics["baseline_only"] == metrics["baseline_only_closed"] == 1
    assert metrics["disciplined_only"] == 0
    assert metrics["baseline_only_closed_net"] > 0
    assert len(metrics["policy_ids"]) == 1
    assert strategy.paired_comparison()["status"] == "NOT_EVALUABLE"
    db.close()


def test_read_only_commands_show_exact_plans_and_separate_quote_accounts(tmp_path):
    db = SQLiteStorage(tmp_path / "commands.db")
    strategy = StrategyShadow(db)
    strategy.evaluate(valid_card())
    command = TelegramCommandService(SimpleNamespace(telegram=object(), repository=None,
        strategy_shadow=strategy))
    plan = asyncio.run(command.handle("/trade_plan binance:spot:SOL-USDT"))[0]
    report = asyncio.run(command.handle("/strategy_performance"))[0]
    assert "baseline" in plan and "disciplined" in plan and "USDT" in plan
    assert "NOT_EVALUABLE" in report and "USDT и USD не суммируются" in report
    assert "Пары USDT" in report and "версий правил=1" in report
    assert "вероятность" in plan and "Виртуальный" in report
    db.close()


def test_incomplete_data_is_explicit_no_trade(tmp_path):
    db = SQLiteStorage(tmp_path / "shadow.db")
    plans = StrategyShadow(db).evaluate({"instrument_id": "coinbase:spot:SOL-USD", "observed_at_ms": 10})
    assert all(p.status == "NO_TRADE" and p.notional == 0 for p in plans)
    db.close()


def test_later_exact_bbo_fills_then_closes_and_restart_replays_events(tmp_path):
    db = SQLiteStorage(tmp_path / "lifecycle.db")
    svc = StrategyShadow(db, {"fee_bps_per_side": 4, "slippage_bps_per_side": 2})
    now = 1_800_000_000_000
    card = {"instrument_id":"binance:spot:SOL-USDT","provider":"binance","venue":"binance-spot","market_type":"spot","quote":"USDT","observed_at_ms":now,
            "candles":{tf:{"trend":"UP","realized_return_volatility_pct_per_bar":.4,"latest_exchange_time_ms":now-1000,"source_provider":"binance","source_venue":"binance-spot"} for tf in ("15m","1h","4h")},
            "candle_quality":{tf:{"status":"FRESH","received_at_ms":now-1000} for tf in ("15m","1h","4h")},
            "bbo":{"source_provider":"binance","source_venue":"binance-spot","received_at_ms":now-1000,"bid":99.9,"ask":100.1,"exchange_time_kind":"RECEIVE_TIME_FALLBACK"}}
    plan=svc.evaluate(card)[0]
    q={"instrument_id":plan.instrument_id,"provider":"binance","venue":"binance-spot","quote":"USDT","market_type":"spot","received_at_ms":now+1000,"bid":100,"ask":100.1,"exchange_time_kind":"RECEIVE_TIME_FALLBACK"}
    assert svc.advance(q,now)["open_positions"]==[]
    assert len(svc.advance(q,now+1000)["open_positions"])==2
    restart=StrategyShadow(db)
    assert len(restart.open_positions())==2
    q.update(received_at_ms=now+3000,bid=102,ask=102.1)
    restart.advance(q,now+3000)
    assert restart.open_positions()==[]
    closed=db.query("SELECT payload_json FROM strategy_shadow_events WHERE event_type='CLOSED'")
    assert len(closed)==2 and all('net_pnl' in r["payload_json"] for r in closed)
    db.close()
