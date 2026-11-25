import asyncio
import copy
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer

from src.data.storage import SQLiteStorage
from src.dashboard.server import create_dashboard_app
from src.service.market_forecast import MarketForecastService, HORIZONS
from src.service.strategy_shadow import StrategyShadow
from tests.test_strategy_shadow import valid_card


def setup_engine(candles):
    iid="binance:perpetual:ETH-USDT"
    source=candles.iloc[-300:]
    now=int(source.open_time.iloc[-1])+300_001
    bars={}
    for r in source.itertuples():
        metadata=SimpleNamespace(received_at_ms=now-1)
        bar=SimpleNamespace(open=r.open,high=r.high,low=r.low,close=r.close,volume=r.volume,
            open_time_ms=int(r.open_time),close_time_ms=int(r.open_time)+299_999,
            metadata=metadata,closed=True)
        bars[(iid,"5m",int(r.open_time))]=bar
    card={"instrument_id":iid,"base":"ETH","venue":"binance-usdm","provider":"binance",
        "quote":"USDT","market_type":"perpetual","observation_status":"OBSERVED",
        "candle_quality":{"5m":{"status":"FRESH","gaps":0}},
        "bbo":{"bid":100.,"ask":100.01,"received_at_ms":now-1,
            "spread_bps":1.,"source_provider":"binance","source_venue":"binance-usdm"}}
    analysis=SimpleNamespace(_candles=bars,_now_ms=lambda:now,markets=lambda n:[card])
    storage=SQLiteStorage(":memory:")
    return MarketForecastService(analysis,storage),card,now


@pytest.mark.parametrize("horizon",HORIZONS)
def test_forecasts_use_real_history_and_keep_probability_semantics(candles,horizon):
    engine,card,now=setup_engine(candles)
    f=engine.forecast(card,horizon,now)
    assert f["status"]=="ESTIMATE"
    assert sum(f["probabilities"].values())==pytest.approx(100)
    assert all(0<=x<=100 for x in f["probabilities"].values())
    assert f["sample_count"]==60 and not f["calibrated"]
    assert "не калиброванная" in f["semantics"]
    engine.storage.close()


def test_future_candles_cannot_change_forecast(candles):
    engine,card,now=setup_engine(candles)
    first=engine.forecast(card,"15m",now)["probabilities"]
    bar=copy.deepcopy(next(iter(engine.analysis._candles.values())))
    bar.open_time_ms=now+300_000; bar.close_time_ms=now+599_999; bar.close=1e9
    engine.analysis._candles[(card["instrument_id"],"5m",bar.open_time_ms)]=bar
    engine._cache.clear()
    assert engine.forecast(card,"15m",now)["probabilities"]==first
    engine.storage.close()


def test_stale_quote_keeps_estimate_but_blocks_alert(candles):
    engine,card,now=setup_engine(candles)
    card["bbo"]["received_at_ms"]=now-60_000
    f=engine.forecast(card,"5m",now)
    assert f["probabilities"] is not None and not f["eligible"]
    assert any("котировки" in r for r in f["reasons"])
    engine.storage.close()


def test_gaps_and_insufficient_history_do_not_fabricate_probabilities(candles):
    engine,card,now=setup_engine(candles)
    # A recent actual gap must block estimates; old gaps before a sufficiently
    # long contiguous suffix must not discard otherwise usable history.
    removed_key=list(engine.analysis._candles)[-50]
    removed=engine.analysis._candles.pop(removed_key)
    assert engine.forecast(card,"5m",now)["probabilities"] is None
    engine.analysis._candles[removed_key]=removed
    engine.analysis._candles=dict(list(engine.analysis._candles.items())[-30:])
    assert engine.forecast(card,"5m",now)["probabilities"] is None
    engine.storage.close()


def test_old_gap_does_not_block_a_complete_recent_history(candles):
    engine,card,now=setup_engine(candles)
    card["candle_quality"]["5m"].update(status="FRESH_WITH_GAPS",gaps=1)
    engine.analysis._candles.pop(list(engine.analysis._candles)[10])
    result=engine.forecast(card,"5m",now)
    assert result["probabilities"] is not None
    assert result["history_count"]>=160 and len(result["sparkline"])==36
    engine.storage.close()


def test_spot_short_is_visible_but_not_actionable(candles):
    engine,card,now=setup_engine(candles)
    card["market_type"]="spot"
    anchor=max(b.open_time_ms for b in engine.analysis._candles.values())
    engine._cache[(card["instrument_id"],"5m",anchor)]={
        "probabilities":{"LONG":10.,"SHORT":80.,"WAIT":10.},"sample_count":60,
        "atr_pct":.01,"historical_mean_return":-.02,"signal_anchor_ms":anchor}
    f=engine.forecast(card,"5m",now)
    assert f["direction"]=="SHORT" and not f["eligible"]
    assert "SHORT доступен только на фьючерсах" in f["reasons"]
    engine.storage.close()


def test_recovered_quote_rearms_same_bar_only_once():
    db=SQLiteStorage(":memory:"); svc=StrategyShadow(db)
    card=valid_card(); card["bbo"]["received_at_ms"]-=60_000
    assert all(p.status=="NO_TRADE" for p in svc.evaluate(card))
    card["observed_at_ms"]+=1000; card["bbo"]["received_at_ms"]=card["observed_at_ms"]-1
    recovered=svc.evaluate(card)
    assert all(p.status=="PROPOSED" for p in recovered)
    card["observed_at_ms"]+=1000; card["bbo"]["received_at_ms"]=card["observed_at_ms"]-1
    assert svc.evaluate(card)==recovered
    assert db.query("SELECT COUNT(*) n FROM strategy_shadow_plans")[0]["n"]==4
    db.close()


def test_forward_observations_are_persisted_and_late_quotes_expire(candles):
    engine,card,now=setup_engine(candles)
    asyncio.run(engine.process(card))
    assert engine.performance()["snapshots"]==len(HORIZONS)
    card["bbo"]["received_at_ms"]=now+300_000
    engine.resolve(card,now+300_000)
    assert engine.performance()["outcomes"]["OBSERVED"]==1
    engine.resolve(card,now+10_000_000)
    assert engine.performance()["outcomes"]["UNAVAILABLE"]==len(HORIZONS)-1
    engine.storage.close()


def test_notifications_and_digest_are_idempotent_across_restart(candles):
    engine,card,now=setup_engine(candles); sent=[]
    async def send(key,message,timestamp):
        sent.append(key); return True,"1"
    engine.send_once=send
    anchor=max(b.open_time_ms for b in engine.analysis._candles.values())
    for horizon in HORIZONS:
        engine._cache[(card["instrument_id"],horizon,anchor)]={
            "probabilities":{"LONG":80.,"SHORT":10.,"WAIT":10.},"sample_count":60,
            "atr_pct":.01,"historical_mean_return":.02,"signal_anchor_ms":anchor}
    asyncio.run(engine.process(card)); asyncio.run(engine.process(card))
    assert len([key for key in sent if key.startswith("signal:")])==1
    assert len([key for key in sent if key.startswith("digest:")])==1
    restarted=MarketForecastService(engine.analysis,engine.storage,send_once=send)
    restarted._cache=engine._cache
    asyncio.run(restarted.process(card))
    assert len(sent)==2
    engine.storage.close()


@pytest.mark.asyncio
async def test_website_horizon_selection_and_removed_miniapp(candles):
    engine,card,now=setup_engine(candles)
    service=SimpleNamespace(market_forecasts=engine)
    async with TestServer(create_dashboard_app(service)) as server:
        async with TestClient(server) as client:
            response=await client.get("/api/forecasts?horizon=5m")
            assert response.status==200
            row=(await response.json())["forecasts"][0]
            assert row["horizon"]=="5m" and row["probabilities"]
            assert (await client.get("/api/forecasts?horizon=2h")).status==200
            assert (await client.get("/api/forecasts?horizon=7h")).status==400
            assert (await client.get("/app/")).status==404
            assert (await client.get("/api/v1/snapshot")).status==404
    engine.storage.close()
