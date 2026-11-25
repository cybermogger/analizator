import numpy as np
import pytest

from src.research.signal_policy import first_exit
from src.research.trend_search import exits, rank


@pytest.mark.parametrize("side",[1,-1])
@pytest.mark.parametrize("target",[.008,.012,.02])
def test_vectorized_levels_and_exits_match_scalar_reference(side,target):
    rng=np.random.default_rng(15)
    close=100*np.exp(np.cumsum(rng.normal(0,.002,100)))
    opening=np.r_[100.,close[:-1]]
    data=dict(open=opening,close=close,high=np.maximum(opening,close)+.1,
              low=np.minimum(opening,close)-.1,volume=np.full(100,1000.),
              open_time=np.arange(100)*300_000,close_time=np.arange(100)*300_000+299_999,
              funding_time=np.array([0,3_600_000]),funding_rate=np.array([.0001,-.0002]))
    anchors=np.arange(0,45,3)
    rows=exits(data,anchors,side,.008,target,48)
    for anchor,row in zip(anchors,rows.itertuples()):
        end,price,reason=first_exit(data,anchor+1,side,.008,target,48)
        assert row.exit==pytest.approx(price)
        assert row.exit_reason==reason
        assert row.exit_at_ms==data["close_time"][end]
        assert row.net_return==pytest.approx(row.gross_return-row.fees-row.spread-row.slippage-row.funding)


def test_clustered_trades_fail_active_day_requirement():
    clustered=dict(periods=[dict(trades=100,active_days=3,net_equity_return_percent=10)]*3)
    distributed=dict(periods=[dict(trades=100,active_days=25,net_equity_return_percent=1)]*3)
    assert rank(distributed)>rank(clustered)
