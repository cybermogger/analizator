"""Static trend experts with larger targets, active-day selection and reserved February."""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from .model_search import prepare, FEATURES, ORIENTED, replay, partition, hashes
from .signal_audit import portfolio, write_json
from .strategy_challenge import ms

RULES = ("trend","breakout","pullback","flow","fade","btc_trend")


def exits(data,idx,side,stop,target,steps):
    entry=data["open"][idx+1]
    sl=entry*(1-side*stop)
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
        done=gap|hit_sl|hit_tp
        end[done]=j[done]
        price[done]=np.where(gap,op,np.where(hit_sl,sl,tp))[done]
        reason[hit_tp]="TP"
        reason[hit_sl]="SL"
        reason[hit_sl&hit_tp]="SL_AMBIGUOUS"
        reason[gap]="SL_GAP"
        active &= ~done
    start=data["open_time"][idx+1].astype(np.int64)
    times=data["funding_time"]
    rates=np.r_[0.,np.cumsum(data["funding_rate"])]
    funding=side*(rates[np.searchsorted(times,data["open_time"][end],"right")]
                  -rates[np.searchsorted(times,start,"left")])
    ratio=price/entry
    fees=.0005*(1+ratio)
    return pd.DataFrame(dict(entry_at_ms=start,exit_at_ms=data["close_time"][end].astype(np.int64),
        deadline=data["close_time"][idx+steps].astype(np.int64),entry=entry,exit=price,
        stop_loss=sl,take_profit=tp,exit_reason=reason,gross_return=side*(ratio-1),fees=fees,
        spread=.0002,slippage=.0004,funding=funding,net_return=side*(ratio-1)-fees-.0006-funding,
        leverage=5,planned_margin_risk=(stop*(1-side*.0005)+.0018)*5,
        liquidity_notional_cap=data["volume"][idx]*data["close"][idx]*.01,
        direction="LONG" if side==1 else "SHORT"))


def dataset(prepared,stop,target):
    parts=[]
    for symbol,asset,data,f,idx in prepared:
        for side in (1,-1):
            features=f.iloc[idx].reset_index(drop=True).copy()
            features[ORIENTED]*=side
            features["side"],features["asset"]=side,asset
            rows=pd.concat([features,exits(data,idx,side,stop,target,48)],axis=1)
            rows["symbol"]=symbol
            parts.append(rows)
    return pd.concat(parts,ignore_index=True)


def score(rows,rule):
    volume=np.log1p(rows.volume_ratio.clip(0,20))
    if rule=="trend":
        return rows.r144.clip(lower=0)*rows.r48.clip(lower=0)*volume
    if rule=="breakout":
        return rows.channel.clip(lower=0)*rows.r144.clip(lower=0)*volume
    if rule=="pullback":
        return rows.r144.clip(lower=0)*(-rows.r3).clip(lower=0)*volume
    if rule=="flow":
        return rows.r144.clip(lower=0)*rows.flow12.clip(lower=0)*volume
    if rule=="btc_trend":
        return rows.btc12.clip(lower=0)*rows.r144.clip(lower=0)*volume
    if rule=="fade":
        return (-rows.r144).clip(lower=0)*rows.body.clip(lower=0)*volume
    raise ValueError(rule)


def stats_for(rows,scores,threshold):
    records=replay(rows,scores,threshold)
    stats,fills=portfolio(records)
    stats["active_days"]=len({r["entry_at_ms"]//86_400_000 for r in fills})
    return stats,records


def source_hash():
    digest=hashlib.sha256()
    for name in ("trend_search.py","model_search.py","signal_audit.py","strategy_challenge.py"):
        digest.update(Path(__file__).with_name(name).read_text(encoding="utf-8").encode())
    return digest.hexdigest()


def head(folder,extra,stop,target,output):
    data=dataset(prepare(folder,extra),stop,target)
    cal=partition(data,"2026-08-01","2026-09-01")
    blocks=[partition(data,a,b) for a,b in (("2026-09-01","2026-10-01"),
            ("2026-10-01","2026-11-01"),("2026-11-01","2026-11-25"))]
    results=[]
    for rule in RULES:
        calibration=score(cal,rule)
        scores=[score(b,rule) for b in blocks]
        for q in (.8,.9,.95,.975):
            cutoff=float(np.quantile(calibration,q))
            periods=[stats_for(b,s,cutoff)[0] for b,s in zip(blocks,scores)]
            results.append(dict(rule=rule,quantile=q,threshold=cutoff,stop=stop,target=target,periods=periods))
    write_json(output,dict(source_sha256=source_hash(),data_sha256=hashes(folder),
                           extra_sha256=hashes(extra),candidates=results))


def rank(candidate):
    stats=candidate["periods"]
    enough=all(s["trades"]>=d*2 and s["active_days"]>=d*.5 for s,d in zip(stats,(30,31,24)))
    return (enough,min(s["net_equity_return_percent"] for s in stats),
            sum(s["net_equity_return_percent"] for s in stats))


def freeze(paths,output):
    runs=[json.loads(p.read_text()) for p in paths]
    if any(r["source_sha256"]!=source_hash() for r in runs):
        raise ValueError("Frozen search differs")
    candidates=[c for r in runs for c in r["candidates"]]
    write_json(output,dict(selected=max(candidates,key=rank),candidate_count=len(candidates),
        candidates=candidates,source_sha256=source_hash(),data_sha256=runs[0]["data_sha256"],
        extra_sha256=runs[0]["extra_sha256"],
        selection="Prefer 2 trades/day and >=50% active days in every month; maximize worst month then total.",
        test_period="February 2026; independent retrospective check; one frozen candidate."))


def test(folder,selection):
    lock=json.loads(selection.read_text())
    if lock["source_sha256"]!=source_hash():
        raise ValueError("Code differs from frozen selection")
    cfg=lock["selected"]
    data=partition(dataset(prepare(folder),cfg["stop"],cfg["target"]),"2026-02-01","2026-03-01")
    stats,records=stats_for(data,score(data,cfg["rule"]),cfg["threshold"])
    stress=portfolio(records,2)[0]
    _,fills=portfolio(records)
    result=dict(selected=cfg,stats=stats,double_costs=stress,goal_reached=bool(stats["final_equity"]>=10100),
                selection_sha256=hashlib.sha256(selection.read_bytes()).hexdigest(),test_sha256=hashes(folder),
                period="February 2026, retrospective independent test; predates development")
    write_json(selection.with_suffix(".test.json"),result)
    pd.DataFrame(fills).to_csv(selection.with_suffix(".fills.csv"),index=False)
    print(json.dumps(result),flush=True)


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("action",choices=("head","freeze","test"))
    p.add_argument("--data",type=Path)
    p.add_argument("--extra",type=Path)
    p.add_argument("--report",type=Path,required=True)
    p.add_argument("--stop",type=float,choices=(.004,.008))
    p.add_argument("--target",type=float,choices=(.008,.012,.02))
    p.add_argument("--inputs",type=Path,nargs="+")
    args=p.parse_args()
    if args.action=="head":
        head(args.data,args.extra,args.stop,args.target,args.report)
    elif args.action=="freeze":
        freeze(args.inputs,args.report)
    else:
        test(args.data,args.report)
