import numpy as np
import pandas as pd
import pytest

from src.research.pullback_orders import limit_outcomes, order_book
from src.research.strategy_challenge import POLICY, feature_frame, partition, simulate


def bars(n):
    price = np.linspace(100., 101., n)
    starts = np.arange(n)*300_000
    return dict(open=price.copy(), close=price.copy(), high=price+.01, low=price-.01,
                volume=np.full(n,10000.),open_time=starts,close_time=starts+299_999,
                funding_time=np.array([0]),funding_rate=np.array([.0001]))


def test_future_candles_and_unpublished_funding_cannot_change_features():
    source = bars(500)
    original = feature_frame(source,source)
    source["close"][300:] *= 2
    source.update(funding_time=np.array([0,301*300_000]),funding_rate=np.array([.0001,-.06]))
    pd.testing.assert_frame_equal(original.iloc[:300],feature_frame(source,source).iloc[:300])


@pytest.mark.parametrize("side",[1,-1])
def test_limit_fill_bar_cannot_book_a_prior_target_wick(side):
    data = bars(30)
    limit = 100*(1-side*.006)
    data["low"][1] = limit*.999 if side==1 else 97
    data["high"][1] = 103 if side==1 else limit*1.001
    data["close"][1:] = limit
    data["open"][2:] = limit
    data["high"][2:],data["low"][2:] = limit+.01,limit-.01
    row = limit_outcomes(data,np.array([0]),side,np.array([.001]),.006,POLICY,24).iloc[0]
    assert row.exit_reason=="TIME"
    assert row.net_return<0
    assert row.entry_at_ms==data["open_time"][1]


def test_limit_order_expires_after_three_bars_with_zero_cost():
    data = bars(30)
    data["low"][4] = 95
    row = limit_outcomes(data,np.array([0]),1,np.array([.001]),.012,POLICY,24).iloc[0]
    assert row.exit_reason=="EXPIRED"
    assert row.exit_at_ms==data["close_time"][3]
    assert row.net_return==0


def test_pending_orders_reserve_slots_before_any_fill():
    data = bars(30)
    expired = limit_outcomes(data,np.array([0]),1,np.array([.001]),.012,POLICY,24).iloc[0].to_dict()
    orders = [dict(expired,symbol=symbol,score=1.) for symbol in "ABCD"]
    later = dict(expired,submit_at_ms=600_000,symbol="E",score=100.,exit_reason="TP",net_return=.01)
    stats,fills = order_book([*orders,later])
    assert stats["orders"]==4 and stats["unfilled"]==4 and not fills
    assert stats["net_equity_return_percent"]==0 and stats["skipped_capacity"]==1


def test_market_ambiguous_and_cost_accounting():
    data = bars(30)
    data["high"][1],data["low"][1] = 103,97
    row = simulate(data,np.array([0]),1,np.array([.001])).iloc[0]
    assert row.exit_reason=="SL_AMBIGUOUS"
    assert row.net_return==pytest.approx(row.gross_return-row.fees-row.slippage-row.spread-row.funding)


def test_partition_uses_submission_and_purges_unresolved_horizons():
    start = pd.Timestamp("2026-11-01",tz="UTC").value//1_000_000
    rows = pd.DataFrame(dict(submit_at_ms=[start-1,start,start],entry_at_ms=[start,-1,start],
                             label_deadline=[start+1,start+1,start+86_400_000]))
    assert list(partition(rows,"2026-11-01","2026-11-02").index)==[1]
