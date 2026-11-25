import numpy as np
import pandas as pd
import pytest

from src.research.model_search import exits, partition, replay, rank


def history():
    return dict(open=np.full(60,100.),high=np.full(60,100.1),low=np.full(60,99.9),
                close=np.full(60,100.),open_time=np.arange(60)*300_000,
                close_time=np.arange(60)*300_000+299_999,
                volume=np.full(60,10000.),funding_time=np.array([300_000]),
                funding_rate=np.array([.0001]))


@pytest.mark.parametrize("side",[1,-1])
def test_target_covers_costs_and_stop_has_priority(side):
    bars=history()
    bars["high"][1],bars["low"][1]=102.,98.
    row=exits(bars,np.array([0]),side,12,.004).iloc[0]
    assert row.exit_reason=="SL_AMBIGUOUS"
    assert row.net_return<0
    target_ratio=row.take_profit/row.entry
    expected=side*(target_ratio-1)-.0005*(1+target_ratio)-.0006-.0002
    assert expected*5>=.03-1e-10


def test_gap_stop_charges_worse_open_and_actual_funding():
    bars=history()
    bars["open"][2],bars["high"][2],bars["low"][2]=98.,98.2,97.9
    row=exits(bars,np.array([0]),1,12,.004).iloc[0]
    assert row.exit_reason=="SL_GAP" and row.exit==98
    assert row.funding==pytest.approx(.0001)
    assert row.net_return==pytest.approx(row.gross_return-row.fees-row.spread-row.slippage-row.funding)


def test_partition_purges_whole_horizon_even_if_trade_exited_early():
    start=pd.Timestamp("2026-09-01",tz="UTC").value//1_000_000
    end=pd.Timestamp("2026-10-01",tz="UTC").value//1_000_000
    rows=pd.DataFrame(dict(entry_at_ms=[start,start],deadline=[start+1,end+1]))
    assert list(partition(rows,"2026-09-01","2026-10-01").index)==[0]


def test_selection_prefers_minimum_frequency_before_return():
    frequent=dict(periods=[dict(trades=70,net_equity_return_percent=1)]*3)
    rare=dict(periods=[dict(trades=1,net_equity_return_percent=20)]*3)
    assert rank(frequent)>rank(rare)
