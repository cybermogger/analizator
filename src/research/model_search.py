"""Pooled causal models with several exits and a frozen independent March replay."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .signal_audit import SYMBOLS, portfolio, write_json
from .strategy_challenge import feature_frame, load_flow, load_market, ms

HORIZONS = (12,24,48)
STOPS = (.004,.008)
QUANTILES = (.90,.95,.975,.99)
COST = .0018
FEATURES = ["atr","body","wick_balance","location","expansion","volume_ratio","dispersion",
            "channel","last_funding_bps","r3","r12","r48","r144","btc12","excess12","excess48",
            "hour_sin","hour_cos","weekend","flow1","flow3","flow12","side","asset"]
ORIENTED = ["body","wick_balance","location","channel","last_funding_bps","r3","r12","r48",
            "r144","btc12","excess12","excess48","flow1","flow3","flow12"]


def joined(folder,extra,symbol):
    data=load_market(folder,symbol)
    if extra:
        extension=load_market(extra,symbol)
        return {k:np.concatenate([data[k],extension[k]]) for k in data}
    return data


def prepare(folder,extra=None):
    btc=joined(folder,extra,"BTCUSDT")
    result=[]
    for asset,symbol in enumerate(SYMBOLS):
        data=joined(folder,extra,symbol)
        f=feature_frame(data,btc)
        buy=load_flow(folder,symbol,load_market(folder,symbol)["open_time"])
        if extra:
            buy=np.r_[buy,load_flow(extra,symbol,load_market(extra,symbol)["open_time"])]
        buy=pd.Series(buy)
        volume=pd.Series(data["volume"])
        for k in (1,3,12):
            f[f"flow{k}"]=2*buy.rolling(k).sum()/volume.rolling(k).sum().replace(0,np.nan)-1
        anchors=np.arange(288,len(f)-49,3)
        valid=np.isfinite(f.iloc[anchors]).all(axis=1).to_numpy()
        anchors=anchors[valid]
        result.append((symbol,asset,data,f,anchors))
    return result


def exits(data,idx,side,steps,stop):
    entry=data["open"][idx+1]
    sl=entry*(1-side*stop)
    target=.0078/(1-side*.0005)
    tp=entry*(1+side*target)
    end=idx+steps
    price=data["close"][end].copy()
    reason=np.full(len(idx),"TIME",dtype="<U12")
    active=np.ones(len(idx),dtype=bool)
    for offset in range(1,steps+1):
        j=idx+offset
        op,hi,lo=(data[k][j] for k in ("open","high","low"))
        gap=active & (side*(op-sl)<=0)
        hit_sl=active & ((lo<=sl) if side==1 else (hi>=sl))
        hit_tp=active & ((hi>=tp) if side==1 else (lo<=tp))
        done=hit_sl|hit_tp|gap
        end[done]=j[done]
        price[done]=np.where(gap,op,np.where(hit_sl,sl,tp))[done]
        reason[hit_tp]="TP"
        reason[hit_sl]="SL"
        reason[hit_sl&hit_tp]="SL_AMBIGUOUS"
        reason[gap]="SL_GAP"
        active &= ~done
    start=data["open_time"][idx+1].astype(np.int64)
    funding_times=data["funding_time"]
    cumulative=np.r_[0.,np.cumsum(data["funding_rate"])]
    funding=side*(cumulative[np.searchsorted(funding_times,data["open_time"][end],"right")]
                  -cumulative[np.searchsorted(funding_times,start,"left")])
    ratio=price/entry
    fees=.0005*(1+ratio)
    gross=side*(ratio-1)
    return pd.DataFrame(dict(entry_at_ms=start,exit_at_ms=data["close_time"][end].astype(np.int64),
        deadline=data["close_time"][idx+steps].astype(np.int64),entry=entry,exit=price,
        stop_loss=sl,take_profit=tp,exit_reason=reason,gross_return=gross,fees=fees,spread=.0002,
        slippage=.0004,funding=funding,net_return=gross-fees-.0006-funding,leverage=5,
        planned_margin_risk=(stop*(1-side*.0005)+COST)*5,
        liquidity_notional_cap=data["volume"][idx]*data["close"][idx]*.01,
        direction="LONG" if side==1 else "SHORT"))


def dataset(prepared,steps,stop):
    parts=[]
    for symbol,asset,data,f,idx in prepared:
        for side in (1,-1):
            features=f.iloc[idx].reset_index(drop=True).copy()
            features[ORIENTED]*=side
            features["side"],features["asset"]=side,asset
            trades=exits(data,idx,side,steps,stop)
            rows=pd.concat([features,trades],axis=1)
            rows["symbol"]=symbol
            parts.append(rows)
    return pd.concat(parts,ignore_index=True)


def partition(rows,start,end):
    return rows[(rows.entry_at_ms>=ms(start))&(rows.deadline<ms(end))]


def models():
    return {
        "ridge":make_pipeline(StandardScaler(),Ridge(alpha=5000)),
        "boost":HistGradientBoostingRegressor(max_iter=150,max_leaf_nodes=15,
                    min_samples_leaf=200,l2_regularization=30,learning_rate=.05,
                    early_stopping=False,random_state=57),
        "extra_trees":ExtraTreesRegressor(n_estimators=80,max_depth=9,min_samples_leaf=100,
                                          max_features=.8,n_jobs=2,random_state=57),
    }


def replay(rows,scores,threshold,direction="both"):
    selected=rows.assign(score=scores)
    selected=selected[selected.score>threshold]
    if direction!="both":
        selected=selected[selected.direction==direction]
    keys=["entry_at_ms","symbol"]
    selected=selected.sort_values(keys+["score"],ascending=[True,True,False])
    selected=selected[~selected.duplicated(keys+["score"],keep=False)].drop_duplicates(keys)
    allowed,records={},[]
    for row in selected.drop(columns=FEATURES).to_dict("records"):
        if row["entry_at_ms"]>allowed.get(row["symbol"],0):
            records.append(row)
            allowed[row["symbol"]]=row["exit_at_ms"]
    return records


def hashes(folder):
    return {s:hashlib.sha256((folder/(s+".npz")).read_bytes()).hexdigest() for s in SYMBOLS}


def source_hash():
    return hashlib.sha256(Path(__file__).read_text(encoding="utf-8").encode()).hexdigest()


def head(folder,extra,steps,stop,output):
    prepared=prepare(folder,extra)
    data=dataset(prepared,steps,stop)
    train=partition(data,"2026-05-01","2026-08-01")
    cal=partition(data,"2026-08-01","2026-09-01")
    blocks=[partition(data,a,b) for a,b in (("2026-09-01","2026-10-01"),
             ("2026-10-01","2026-11-01"),("2026-11-01","2026-11-25"))]
    candidates=[]
    for name,model in models().items():
        model.fit(train[FEATURES],(train.net_return*10000).clip(-400,400))
        calibration=model.predict(cal[FEATURES])
        scores=[model.predict(b[FEATURES]) for b in blocks]
        for q in QUANTILES:
            threshold=float(np.quantile(calibration,q))
            for direction in ("both","LONG","SHORT"):
                stats=[portfolio(replay(b,s,threshold,direction))[0] for b,s in zip(blocks,scores)]
                candidates.append(dict(model=name,steps=steps,stop=stop,quantile=q,direction=direction,
                    threshold=threshold,calibration_sha256=hashlib.sha256(calibration.round(8).tobytes()).hexdigest(),
                    periods=stats))
        print(name,steps,stop,flush=True)
    write_json(output,dict(candidates=candidates,source_sha256=source_hash(),
                           data_sha256=hashes(folder),extra_sha256=hashes(extra)))


def rank(candidate):
    stats=candidate["periods"]
    # Aim for at least two executed trades per calendar day on each validation month.
    enough=all(s["trades"]>=days*2 for s,days in zip(stats,(30,31,24)))
    returns=[s["net_equity_return_percent"] for s in stats]
    return (enough,min(returns),sum(returns))


def freeze(paths,output):
    runs=[json.loads(p.read_text()) for p in paths]
    if any(r["source_sha256"]!=source_hash() for r in runs):
        raise ValueError("Search source changed")
    candidates=[c for r in runs for c in r["candidates"]]
    write_json(output,dict(selected=max(candidates,key=rank),candidate_count=len(candidates),
         candidates=candidates,source_sha256=source_hash(),data_sha256=runs[0]["data_sha256"],
         extra_sha256=runs[0]["extra_sha256"],
         selection="Prefer >=2 executed trades/day each validation month, maximize worst month then total.",
         test_period="2026-03-01 / 2026-03-31, independent retrospective check. One chosen candidate."))


def test(folder,extra,independent,selection):
    lock=json.loads(selection.read_text())
    if source_hash()!=lock["source_sha256"] or hashes(folder)!=lock["data_sha256"] or hashes(extra)!=lock["extra_sha256"]:
        raise ValueError("Frozen code or development data differs")
    cfg=lock["selected"]
    data=dataset(prepare(folder,extra),cfg["steps"],cfg["stop"])
    model=models()[cfg["model"]]
    train=partition(data,"2026-05-01","2026-08-01")
    model.fit(train[FEATURES],(train.net_return*10000).clip(-400,400))
    calibration=model.predict(partition(data,"2026-08-01","2026-09-01")[FEATURES])
    if hashlib.sha256(calibration.round(8).tobytes()).hexdigest()!=cfg["calibration_sha256"]:
        raise ValueError("Frozen calibration differs")
    test_data=partition(dataset(prepare(independent),cfg["steps"],cfg["stop"]),"2026-03-01","2026-04-01")
    records=replay(test_data,model.predict(test_data[FEATURES]),cfg["threshold"],cfg["direction"])
    stats,fills=portfolio(records)
    stress=portfolio(records,2)[0]
    result=dict(selected=cfg,stats=stats,double_costs=stress,
          selection_sha256=hashlib.sha256(selection.read_bytes()).hexdigest(),
          test_sha256=hashes(independent),goal_reached=bool(stats["final_equity"]>=10100),
          active_days=len({r["entry_at_ms"]//86_400_000 for r in fills}),
          trade_count_per_day=len(fills)/31,period="March 2026, independent retrospective check",
          notes=["5x isolated hypothetical leverage, 0.25% equity risk, max 4 concurrent positions.",
                 "Stops first on ambiguous OHLC, next-open entry, actual funding, 5bps fee per side.",
                 "Closed-equity drawdown excludes floating losses and liquidation.",
                 "March predates model training and does not constitute a deployable chronological forward test."])
    write_json(selection.with_suffix(".test.json"),result)
    pd.DataFrame(fills).to_csv(selection.with_suffix(".fills.csv"),index=False)
    print(json.dumps(result),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action",choices=("head","freeze","test"))
    p.add_argument("--data",type=Path)
    p.add_argument("--extra",type=Path)
    p.add_argument("--independent",type=Path)
    p.add_argument("--report",type=Path,required=True)
    p.add_argument("--steps",type=int,choices=HORIZONS)
    p.add_argument("--stop",type=float,choices=STOPS)
    p.add_argument("--inputs",type=Path,nargs="+")
    a=p.parse_args()
    with threadpool_limits(limits=2):
        if a.action=="head":
            head(a.data,a.extra,a.steps,a.stop,a.report)
        elif a.action=="freeze":
            freeze(a.inputs,a.report)
        else:
            test(a.data,a.extra,a.independent,a.report)


if __name__=="__main__":
    main()
