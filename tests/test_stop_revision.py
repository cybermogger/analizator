import pandas as pd

from src.research.stop_revision import evaluate
from src.research.signal_policy import first_exit


def test_wider_stop_can_increase_loss_when_direction_remains_wrong():
    bars = dict(open=[100.,99.5],high=[100.1,99.6],low=[99.4,98.9],close=[99.5,99.])
    narrow = first_exit(bars,0,1,.005,.008,2)
    wide = first_exit(bars,0,1,.008,.008,2)
    assert narrow[1]==99.5
    assert wide[1]==99.2
    assert narrow[0]==0 and wide[0]==1


def test_period_boundaries_exclude_incomplete_trade_and_preperiod_entries():
    start=pd.Timestamp("2026-04-01",tz="UTC").value//1_000_000
    end=pd.Timestamp("2026-05-01",tz="UTC").value//1_000_000
    rows=[dict(entry_at_ms=start-1,exit_at_ms=start+1),
          dict(entry_at_ms=start,exit_at_ms=start+300_000),
          dict(entry_at_ms=end-8_000_000,exit_at_ms=end-1)]
    assert evaluate(rows,"2026-04-01","2026-05-01")==[rows[1]]
