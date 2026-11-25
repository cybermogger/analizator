"""Entry and stop research on public candles, with frozen April cross-period check."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .signal_audit import SYMBOLS, portfolio, write_json
from .signal_policy import ProfitPolicy, first_exit
from .strategy_challenge import load_market, feature_frame

POLICY = ProfitPolicy()
METHODS = ("momentum", "reversal", "funding_reversal", "confirmed_reversal")


def diagnose(development, november, ledger):
    trades = pd.read_csv(ledger)
    details = []
    for row in trades.itertuples():
        a, b = load_market(development,row.symbol), load_market(november,row.symbol)
        data = {k:np.concatenate([a[k],b[k]]) for k in a}
        sign = 1 if row.direction=="LONG" else -1
        start = int(np.searchsorted(data["open_time"],row.entry_at_ms))
        end = min(start+24,len(data["open"]))
        stopped = int(np.searchsorted(data["close_time"],row.exit_at_ms))
        favorable = data["high"][start:end] if sign==1 else data["low"][start:end]
        adverse = data["low"][start:end] if sign==1 else data["high"][start:end]
        target_touches = sign*(favorable-row.take_profit)>=0
        details.append(dict(symbol=row.symbol,entry_at_ms=row.entry_at_ms,direction=row.direction,
            exit_reason=row.exit_reason,stop_percent=abs(row.stop_loss/row.entry-1)*100,
            minutes_to_exit=(row.exit_at_ms-row.entry_at_ms+1)/60000,
            maximum_favorable_percent=float(np.max(sign*(favorable/row.entry-1))*100),
            maximum_adverse_percent=float(-np.min(sign*(adverse/row.entry-1))*100),
            target_after_stop=bool(row.exit_reason.startswith("SL") and
                target_touches[max(0,stopped-start+1):].any()),
            horizon_close_net_percent=float((sign*(data["close"][end-1]/row.entry-1)-.0018)*100)))
    return details


def candidates(folder, config):
    btc = load_market(folder,"BTCUSDT")
    records = []
    for symbol in SYMBOLS:
        data = load_market(folder,symbol)
        f = feature_frame(data,btc)
        anchors = np.arange(288,len(f)-25,3)
        v = f.iloc[anchors]
        method = config["method"]
        strength = v.r12.to_numpy()
        if method in ("reversal","confirmed_reversal"):
            strength = -v.excess12.to_numpy()
        if method=="funding_reversal":
            strength = -v.excess12.to_numpy()
        side = np.where(strength>0,1,-1)
        mask = np.abs(strength)>=config["threshold"]
        if method=="funding_reversal":
            mask &= side*v.last_funding_bps.to_numpy()<0
        if method=="confirmed_reversal":
            mask &= side*v.body.to_numpy()>0
        mask &= v.volume_ratio.to_numpy()>=config["volume"]
        for i, sign in zip(anchors[mask],side[mask]):
            atr = float(f.atr.iloc[i])
            stop = max(config["stop"],config["atr"]*atr)
            loss = stop*(1-sign*POLICY.exit_fee)+POLICY.cost
            if loss*5>.05:
                continue
            target = (.006+POLICY.cost)/(1-sign*POLICY.exit_fee)
            j, price, reason = first_exit(data,i+1,int(sign),stop,target,24)
            entry = float(data["open"][i+1])
            ratio = price/entry
            start = data["open_time"][i+1]
            funding = float(sign*data["funding_rate"][
                (data["funding_time"]>=start)&(data["funding_time"]<=data["open_time"][j])].sum())
            fees = POLICY.entry_fee+POLICY.exit_fee*ratio
            records.append(dict(symbol=symbol,entry_at_ms=int(start),exit_at_ms=int(data["close_time"][j]),
                direction="LONG" if sign==1 else "SHORT",entry=entry,exit=price,
                stop_loss=entry*(1-sign*stop),take_profit=entry*(1+sign*target),exit_reason=reason,
                gross_return=sign*(ratio-1),fees=fees,spread=POLICY.spread,slippage=POLICY.slippage,
                funding=funding,net_return=sign*(ratio-1)-fees-POLICY.spread-POLICY.slippage-funding,
                planned_margin_risk=loss*5,leverage=5,
                liquidity_notional_cap=float(data["volume"][i]*data["close"][i]*.01)))
    # Selection uses only submitted direction and past closed positions.
    available, result = {}, []
    for row in sorted(records,key=lambda r:(r["entry_at_ms"],r["symbol"])):
        if row["entry_at_ms"]>available.get(row["symbol"],0):
            result.append(row)
            available[row["symbol"]] = row["exit_at_ms"]
    return result


def evaluate(rows, start, end):
    a,b = (pd.Timestamp(x,tz="UTC").value//1_000_000 for x in (start,end))
    return [r for r in rows if a<=r["entry_at_ms"] and r["exit_at_ms"]<b-7_200_000]


def select(folder, report):
    results = []
    for method in METHODS:
        for threshold in (1.,2.):
            for stop,atr in ((.003,1.6),(.006,0.),(.008,0.)):
                for volume in (0.,1.25):
                    cfg = dict(method=method,threshold=threshold,stop=stop,atr=atr,volume=volume)
                    rows = candidates(folder,cfg)
                    stats = [portfolio(evaluate(rows,a,b))[0] for a,b in
                        (("2026-05-01","2026-07-01"),("2026-07-01","2026-09-01"),
                         ("2026-09-01","2026-11-01"))]
                    results.append(dict(config=cfg,periods=stats))
                    print(len(results),flush=True)
    def rank(result):
        stats=result["periods"]
        return (min(s["trades"] for s in stats)>=40,
                min(s["net_equity_return_percent"] for s in stats),
                sum(s["net_equity_return_percent"] for s in stats))
    write_json(report,dict(selected=max(results,key=rank),candidates=results,
        source_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        selection="Maximize worst two-month return with >=40 trades per block. April reserved until selection."))


def test(folder,report):
    lock=json.loads(report.read_text())
    if lock["source_sha256"]!=hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError("Frozen source mismatch")
    rows=evaluate(candidates(folder,lock["selected"]["config"]),"2026-04-01","2026-05-01")
    stats,fills=portfolio(rows)
    result=dict(config=lock["selected"]["config"],stats=stats,double_costs=portfolio(rows,2)[0],
        target_100_dollars=bool(stats["final_equity"]>=10100),
        selection_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
        period="April 2026, retrospective independent regime check; predates development")
    write_json(report.with_suffix(".test.json"),result)
    pd.DataFrame(fills).to_csv(report.with_suffix(".fills.csv"),index=False)
    print(json.dumps(result),flush=True)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("select","test","diagnose"))
    parser.add_argument("--data",type=Path,required=True)
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--november",type=Path)
    parser.add_argument("--ledger",type=Path)
    args=parser.parse_args()
    if args.action=="select":
        select(args.data,args.report)
    elif args.action=="test":
        test(args.data,args.report)
    else:
        write_json(args.report,diagnose(args.data,args.november,args.ledger))
