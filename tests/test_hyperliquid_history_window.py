from types import SimpleNamespace
import pytest
from src.providers.hyperliquid import HyperliquidProvider


@pytest.mark.asyncio
@pytest.mark.parametrize("interval,duration",[("5m",300000),("15m",900000),("1h",3600000),("4h",14400000)])
async def test_history_window_is_measured_in_requested_bars(interval,duration):
    calls=[]
    class Rest:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def candles(self,symbol,tf,start,end):
            calls.append((start,end,tf)); return []
    provider=HyperliquidProvider({"rest_url":"https://api.hyperliquid.xyz/info"},None)
    provider._rest=lambda:Rest()
    instrument=SimpleNamespace(provider="hyperliquid",venue_symbol="SOL")
    await provider.fetch_candles(instrument,interval,limit=300)
    start,end,tf=calls[0]
    assert tf==interval and end-start==301*duration
    calls.clear()
    await provider.fetch_candles(instrument,interval,start_ms=12345,limit=300)
    assert calls[0][0]==12345
