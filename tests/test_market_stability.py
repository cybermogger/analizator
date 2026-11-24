import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from src.data.repository import MarketRepository
from src.data.storage import SQLiteStorage
from src.providers.coinbase import CoinbaseProvider
from src.providers.binance import BinanceProvider
from src.providers.http import PublicHTTPClient
from src.providers.base import ProviderError
from aiohttp import web
from aiohttp.test_utils import TestServer
from src.service.market_data import MultiVenueMarketService, _ProviderRequestGate
from src.service.market_analysis import MultiAssetAnalysisService
from tests.test_providers_market_truth import loaded, FIXTURES
from tests.test_multi_asset_analysis import make_instrument, make_market_data, MarketData, NOW
from tests.test_market_forecast import setup_engine
from tests.test_catalog_discovery_alerts import catalog_entry, scope
from src.providers.types import MarketCatalogScope
from src.providers.universe import UniverseMembership, UniverseBucket


@pytest.mark.asyncio
async def test_public_http_retries_a_transport_disconnect_but_not_http_errors():
    calls=[0]
    limited=[0]
    async def handler(request):
        calls[0]+=1
        if calls[0]==1:
            request.transport.close()
            return web.Response()
        return web.json_response({'ok':True})
    async def rate_limited(request):
        limited[0]+=1
        return web.json_response({'error':'rate limit'},status=429)
    app=web.Application();app.router.add_get('/',handler);app.router.add_get('/limited',rate_limited)
    server=TestServer(app);await server.start_server()
    client=PublicHTTPClient(str(server.make_url('/')),timeout_seconds=2)
    try:
        payload,_=await client.get_json('/')
        assert payload=={'ok':True} and calls[0]==2
        with pytest.raises(ProviderError,match='429'):
            await client.get_json('/limited')
        assert limited[0]==1
    finally:
        await client.close();await server.close()


@pytest.mark.asyncio
async def test_binance_unicode_symbol_does_not_poison_bulk_quote_request():
    config,instruments=loaded()
    instrument=make_instrument('binance','spot','币安人生','USDT','币安人生USDT')
    provider=BinanceProvider(config['providers']['binance'],instruments)
    class HTTP:
        base_url='https://api.binance.com'
        async def get_json(self,path,params=None):
            assert json.loads(params['symbols'])==[instrument.venue_symbol]
            assert '\\u' not in params['symbols']
            return [{'symbol':instrument.venue_symbol,'bidPrice':'1','askPrice':'1.001','bidQty':'100','askQty':'100'}],NOW
        async def close(self):pass
    await provider.spot.close();provider.spot=HTTP()
    result=await provider.refresh_bbo_batch([instrument])
    assert len(result)==1 and result[0].metadata.instrument_id==instrument.instrument_id
    await provider.close()


def test_deep_analysis_slots_remain_stable_but_admission_and_quotas_win():
    import time
    service=object.__new__(MultiVenueMarketService)
    service._deep_rebalance_at_ms=int(time.time()*1000)+300_000
    service.universe=SimpleNamespace(max_deep=2,deep_bucket_minimums={UniverseBucket.DERIVATIVES_MOVERS:1})
    def member(i,score,deep,bucket=UniverseBucket.LIQUID_SPOT_CANDIDATES):
        return UniverseMembership(i,i,bucket,score,deep)
    service.membership=(member('spot-A',.8,True),member('perp-A',.8,True,UniverseBucket.DERIVATIVES_MOVERS))
    incoming=(member('spot-B',.85,True),member('spot-A',.79,False),member('perp-A',.79,True,UniverseBucket.DERIVATIVES_MOVERS))
    result=service._stabilize_deep_watchlist(incoming)
    assert {m.instrument_id for m in result if m.deep_watchlist}=={'spot-A','perp-A'}
    result=service._stabilize_deep_watchlist(tuple(m for m in incoming if m.instrument_id!='spot-A'))
    assert {m.instrument_id for m in result if m.deep_watchlist}=={'spot-B','perp-A'}
    service._deep_rebalance_at_ms=0
    result=service._stabilize_deep_watchlist(incoming)
    assert {m.instrument_id for m in result if m.deep_watchlist}=={'spot-B','perp-A'}


def test_temporary_membership_loss_preserves_history_but_hides_card():
    instrument=make_instrument('binance','spot','ETH','USDT','ETHUSDT')
    data,_=make_market_data(instrument)
    analysis=MultiAssetAnalysisService(data)
    asyncio.run(analysis.refresh_once())
    history=analysis.candle_history(instrument.instrument_id,'5m')
    members=data.membership
    data.membership=()
    assert analysis.markets()==[]
    assert analysis.get(instrument.instrument_id) is None
    assert analysis.candle_history(instrument.instrument_id,'5m')==history
    data.membership=members
    asyncio.run(analysis.refresh_once())
    assert analysis.get(instrument.instrument_id) is not None


@pytest.mark.asyncio
async def test_five_minute_boundary_updates_multiple_assets_before_slow_intervals():
    pairs=[]
    for base in ('ETH','BTC'):
        instrument=make_instrument('binance','spot',base,'USDT',base+'USDT')
        data,provider=make_market_data(instrument)
        pairs.append((instrument,provider,data.truth_engine.latest_bbo(instrument.instrument_id),data.latest_l2[instrument.instrument_id]))
    analysis=MultiAssetAnalysisService(MarketData(pairs),{'instruments_per_cycle':2})
    analysis._now_ms=lambda:NOW
    await analysis.refresh_once()
    analysis._now_ms=lambda:NOW+300_000
    original=analysis._refresh_interval
    calls=[]
    async def record(instrument,interval,now):
        calls.append((instrument.instrument_id,interval))
        return await original(instrument,interval,now)
    analysis._refresh_interval=record
    await analysis.refresh_once()
    assert [interval for _,interval in calls[:2]]==['5m','5m']
    assert len({iid for iid,_ in calls[:2]})==2
    assert analysis._requests<=analysis.max_requests_per_cycle


@pytest.mark.asyncio
async def test_quotes_have_reserved_capacity_while_depth_requests_are_blocked():
    gate=_ProviderRequestGate(4,0)
    blocked=asyncio.Event()
    started=asyncio.Event()
    async def slow():
        if gate.in_flight==3: started.set()
        await blocked.wait()
    tasks=[asyncio.create_task(gate.run(slow)) for _ in range(6)]
    try:
        await asyncio.wait_for(started.wait(),1)
        async def quote(): return 'fresh quote'
        assert await asyncio.wait_for(gate.run(quote,priority=True),.1)=='fresh quote'
        assert gate.maximum_in_flight<=4
    finally:
        blocked.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_catalog_retry_only_reloads_the_failed_venue(monkeypatch):
    import src.service.market_data as module
    clock=[1_800_000_000_000]
    monkeypatch.setattr(module.time,'time',lambda:clock[0]/1000)
    config,_=loaded()
    storage=SQLiteStorage(':memory:')
    service=MultiVenueMarketService(config,MarketRepository(storage))
    class Provider:
        def __init__(self,name):self.provider_id=name;self.calls=0
        async def discover_catalog(self):
            self.calls+=1
            if self.provider_id=='coinbase':
                return (MarketCatalogScope('coinbase','spot','official',clock[0],(),False,error='temporary timeout'),)
            return (scope(clock[0],catalog_entry('BTC',clock[0])),)
        async def close(self):pass
    healthy,failed=Provider('binance'),Provider('coinbase')
    original=service.providers._providers
    service.providers._providers={'binance':healthy,'coinbase':failed}
    await service._refresh_catalogs(clock[0])
    clock[0]+=61_000
    await service._refresh_catalogs(clock[0])
    assert healthy.calls==1 and failed.calls==2
    assert service._catalog_state['binance:spot']['status']=='fresh'
    service.providers._providers=original
    await service.stop()
    storage.close()


@pytest.mark.asyncio
async def test_catalog_and_depth_latency_do_not_block_quote_loop():
    config,_=loaded()
    storage=SQLiteStorage(':memory:')
    service=MultiVenueMarketService(config,MarketRepository(storage))
    blocked=asyncio.Event()
    catalog_started=asyncio.Event()
    quote_seen=asyncio.Event()
    async def catalog(now):
        catalog_started.set()
        await blocked.wait()
    async def depth(members):
        await blocked.wait()
    async def quote(**kwargs):
        assert kwargs['refresh_catalog'] is False
        await catalog_started.wait()
        quote_seen.set()
        service._stop.set()
    service._refresh_catalogs=catalog
    service._refresh_deep=depth
    service.refresh_once=quote
    await asyncio.wait_for(service.run_forever(),1)
    assert quote_seen.is_set()
    await service.stop()
    storage.close()


@pytest.mark.asyncio
async def test_overlapping_coinbase_pages_keep_unique_exact_products():
    config,instruments=loaded()
    provider=CoinbaseProvider(config['providers']['coinbase'],instruments)
    first=json.loads((FIXTURES/'coinbase_public_products_page_1.json').read_text())
    second=json.loads((FIXTURES/'coinbase_public_products_page_2.json').read_text())
    second['products'].insert(0,copy.deepcopy(first['products'][0]))
    class HTTP:
        base_url='https://api.coinbase.com'
        calls=0
        async def get_json(self,path,params=None):
            self.calls+=1
            return (first if self.calls==1 else second),1_700_000_000_000
        async def close(self): pass
    await provider.http.close()
    provider.http=HTTP()
    scopes=await provider.discover_catalog()
    assert scopes[0].complete and len(scopes[0].entries)==2
    await provider.close()


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_signal_levels_have_correct_sides_costs_and_expiry(candles,direction):
    engine,card,now=setup_engine(candles)
    anchor=max(bar.open_time_ms for bar in engine.analysis._candles.values())
    engine._cache[(card['instrument_id'],'5m',anchor)]={
        'probabilities':{'LONG':80. if direction=='LONG' else 10.,'SHORT':80. if direction=='SHORT' else 10.,'WAIT':10.},
        'sample_count':60,'atr_pct':.01,'historical_mean_return':.02 if direction=='LONG' else -.02,
        'signal_anchor_ms':anchor}
    result=engine.forecast(card,'5m',now)
    plan=result['trade_plan']
    assert result['eligible'] and plan['status']=='SIGNAL'
    assert plan['stop_loss']<plan['entry']<plan['take_profit'] if direction=='LONG' else plan['take_profit']<plan['entry']<plan['stop_loss']
    assert plan['reward_risk']==1 and 0<plan['reward_risk_after_costs']<1
    assert plan['expires_at_ms']<=card['bbo']['received_at_ms']+30000
    assert 'тейк-профит' in engine.format(result)
    card['bbo']['received_at_ms']=now-60000
    stale=engine.forecast(card,'5m',now)
    assert not stale['eligible'] and 'trade_plan' not in stale
    engine.storage.close()
