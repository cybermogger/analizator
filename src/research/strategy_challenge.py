"""Chronological multi-market strategy comparison. Run select before holdout.

python -m src.research.strategy_challenge select --data ../public_research_2026 --report reports/challenge.json
python -m src.research.strategy_challenge holdout --data ../public_research_2026 --november ../public_research_november --report reports/challenge.json
"""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from .signal_audit import SYMBOLS, portfolio, write_json
from .signal_policy import ProfitPolicy, causal_features
from .pullback_orders import limit_outcomes, order_book

POLICY = ProfitPolicy(minimum_net_reward_risk=1.25)
BAR = 300_000
STEPS = 24
WARMUP = 288
PERCENTILES = (.975, .995)
RULES = ("sweep", "relative_reversal", "breakout", "pullback")
LEARNERS = ("ridge", "boost")
FLOW_METHODS = ("flow_boost", "absorption", "flow_trend", "crowded_reversal")
SIGNED = ["body", "wick_balance", "location", "r3", "r12", "r48", "r144",
          "btc12", "excess12", "excess48", "channel", "last_funding_bps"]
UNSIGNED = ["atr", "expansion", "volume_ratio", "dispersion", "hour_sin", "hour_cos", "weekend"]
FEATURES = SIGNED + UNSIGNED + ["side"]
FLOW = ["flow1","flow3","flow12","flow_change","price_response"]


def ms(day):
    return pd.Timestamp(day, tz="UTC").value // 1_000_000


def load_market(folder, symbol):
    with np.load(folder / (symbol + ".npz"), allow_pickle=False) as archive:
        return {key: archive[key] for key in archive.files}


def load_flow(folder, symbol, expected_times):
    manifest = json.loads((folder/(symbol+"-sources.json")).read_text(encoding="utf-8"))
    entries = manifest.get("sources",manifest.get("klines",[]))
    frames = []
    for item in sorted(entries,key=lambda e:e["url"]):
        if "/klines/" not in item["url"]:
            continue
        path = folder/"public_archives"/item["url"].rsplit("/",1)[1]
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item["sha256"]:
            raise ValueError(f"Public taker volume checksum mismatch for {path.name}")
        with zipfile.ZipFile(path) as archive:
            with archive.open(archive.namelist()[0]) as stream:
                frame = pd.read_csv(stream,usecols=["open_time","volume","taker_buy_volume"])
        frames.append(frame)
    data = pd.concat(frames,ignore_index=True)
    if not np.array_equal(data.open_time,expected_times):
        raise ValueError("Taker volume timestamp mismatch")
    return data.taker_buy_volume.to_numpy()


def feature_frame(data, btc):
    frame = pd.DataFrame({name: data[name] for name in ("open", "high", "low", "close", "volume")})
    close, high, low = frame.close, frame.high, frame.low
    atr = causal_features(frame).atr_pct.clip(lower=.0001)
    now = data["open_time"].astype(np.int64)
    if not np.array_equal(now, btc["open_time"]) or np.any(np.diff(now) != BAR):
        raise ValueError("Expected synchronized contiguous USD-M candles")
    prior_rate = np.searchsorted(data["funding_time"], now, side="right")-1
    funding = np.where(prior_rate >= 0, data["funding_rate"][prior_rate], np.nan)
    btc_close = pd.Series(btc["close"])
    width = (high-low).replace(0, np.nan)
    out = pd.DataFrame({
        "atr": atr, "body": (close-frame.open)/(close*atr),
        "wick_balance": (np.minimum(close,frame.open)-low-high+np.maximum(close,frame.open))/(close*atr),
        "location": ((close-low)/width-.5)*2,
        "expansion": (high-low)/(close*atr),
        "volume_ratio": frame.volume/frame.volume.rolling(96).median().replace(0,np.nan),
        "dispersion": atr/atr.rolling(144).median(),
        "channel": (close-low.rolling(48).min())/(high.rolling(48).max()-low.rolling(48).min()).replace(0,np.nan)*2-1,
        "last_funding_bps": funding*10000,
    })
    for k in (3,12,48,144):
        out[f"r{k}"] = close.pct_change(k)/(atr*np.sqrt(k))
    out["btc12"] = btc_close.pct_change(12)/(atr*np.sqrt(12))
    for k in (12,48):
        out[f"excess{k}"] = (close.pct_change(k)-btc_close.pct_change(k))/(atr*np.sqrt(k))
    hour = ((now+BAR)//BAR % 288)/288*2*np.pi
    out["hour_sin"], out["hour_cos"] = np.sin(hour), np.cos(hour)
    out["weekend"] = (pd.to_datetime(now, unit="ms", utc=True).dayofweek>=5).astype(float)
    return out


def simulate(data, anchors, sign, atr):
    """Vectorized next-open replay. Stop gaps and ambiguous bars are charged first."""
    policy = POLICY
    entry_idx = anchors+1
    entry = data["open"][entry_idx]
    stop = np.maximum(.0045, 1.6*atr)
    loss = stop*(1-sign*policy.exit_fee)+policy.cost
    net_goal = np.maximum(policy.minimum_margin_profit/policy.leverage, policy.minimum_net_reward_risk*loss)
    target = (net_goal+policy.cost)/(1-sign*policy.exit_fee)
    sl, tp = entry*(1-sign*stop), entry*(1+sign*target)
    end = entry_idx+STEPS-1
    exit_price = data["close"][end].copy()
    reason = np.full(len(entry), "TIME", dtype="<U12")
    live = np.ones(len(entry), dtype=bool)
    for offset in range(STEPS):
        j = entry_idx+offset
        opening, hi, lo = (data[k][j] for k in ("open","high","low"))
        gap = live & (sign*(opening-sl)<=0)
        hit_sl = live & ((lo<=sl) if sign==1 else (hi>=sl))
        hit_tp = live & ((hi>=tp) if sign==1 else (lo<=tp))
        done = gap | hit_sl | hit_tp
        end[done] = j[done]
        exit_price[done] = np.where(gap, opening, np.where(hit_sl, sl, tp))[done]
        reason[hit_tp] = "TP"
        reason[hit_sl] = "SL"
        reason[hit_sl & hit_tp] = "SL_AMBIGUOUS"
        reason[gap] = "SL_GAP"
        live &= ~done

    start_ms = data["open_time"][entry_idx].astype(np.int64)
    end_ms = data["open_time"][end].astype(np.int64)
    cumulative = np.r_[0., np.cumsum(data["funding_rate"])]
    ftime = data["funding_time"]
    funding = sign*(cumulative[np.searchsorted(ftime,end_ms,side="right")]
                    - cumulative[np.searchsorted(ftime,start_ms,side="left")])
    ratio = exit_price/entry
    gross = sign*(ratio-1)
    fees = policy.entry_fee+policy.exit_fee*ratio
    net = gross-fees-policy.spread-policy.slippage-funding
    return pd.DataFrame({
        "entry_at_ms": start_ms, "exit_at_ms": data["close_time"][end].astype(np.int64),
        "label_deadline": data["close_time"][entry_idx+STEPS-1].astype(np.int64),
        "entry":entry, "exit":exit_price,"stop_loss":sl,"take_profit":tp,"exit_reason":reason,
        "gross_return":gross,"net_return":net,"fees":fees,"spread":policy.spread,
        "slippage":policy.slippage,"funding":funding,"leverage":policy.leverage,
        "planned_margin_risk":loss*policy.leverage,"direction":"LONG" if sign==1 else "SHORT",
        "liquidity_notional_cap":data["volume"][anchors]*data["close"][anchors]*.01,
    })


def dataset(data_dir, extension=None, depth=0., with_flow=False):
    def history(symbol):
        base = load_market(data_dir, symbol)
        if extension is None:
            return base
        extra = load_market(extension, symbol)
        # Keep identical causal warmup before November; discard development decisions below.
        return {k: np.concatenate([base[k],extra[k]]) for k in base}
    btc = history("BTCUSDT")
    result = []
    for symbol in SYMBOLS:
        data = btc if symbol=="BTCUSDT" else history(symbol)
        features = feature_frame(data, btc)
        if with_flow:
            sources = [data_dir] if extension is None else [data_dir,extension]
            buy = pd.Series(np.concatenate([load_flow(f,symbol,load_market(f,symbol)["open_time"]) for f in sources]))
            volume = pd.Series(data["volume"])
            if (buy<0).any() or (buy>volume+1e-7).any():
                raise ValueError("Invalid official taker volume")
            for k in (1,3,12):
                features[f"flow{k}"] = 2*buy.rolling(k).sum()/volume.rolling(k).sum().replace(0,np.nan)-1
            features["flow_change"] = features.flow3-features.flow12
            features["price_response"] = features.body-2*features.flow1
        anchors = np.arange(WARMUP,len(features)-STEPS,3)
        if extension is not None:
            anchors = anchors[data["open_time"][anchors]+BAR >= ms("2026-11-01")]
        elif depth:
            anchors = anchors[data["open_time"][anchors]+BAR >= ms("2026-08-01")]
        cand = features.iloc[anchors].reset_index(drop=True)
        for sign in (1,-1):
            oriented = cand.copy()
            oriented[SIGNED] *= sign
            if with_flow:
                oriented[FLOW] *= sign
            oriented["side"] = sign
            trades = (limit_outcomes(data,anchors,sign,cand.atr.to_numpy(),depth,POLICY,STEPS)
                      if depth else simulate(data, anchors, sign, cand.atr.to_numpy()))
            rows = pd.concat([oriented,trades],axis=1)
            rows["symbol"] = symbol
            keep = ((rows.planned_margin_risk <= POLICY.maximum_margin_loss)
                    & (rows.liquidity_notional_cap>0) & np.isfinite(rows[FEATURES+(FLOW if with_flow else [])]).all(axis=1))
            result.append(rows[keep])
    return pd.concat(result, ignore_index=True)


def partition(rows, start, end):
    clock = rows["submit_at_ms"] if "submit_at_ms" in rows else rows.entry_at_ms
    return rows[(clock >= ms(start)) & (rows.label_deadline < ms(end))]


def fit_models(train):
    models = {
        "ridge": make_pipeline(StandardScaler(), Ridge(alpha=1000)),
        "boost": HistGradientBoostingRegressor(max_iter=100, learning_rate=.06,
                    max_leaf_nodes=15, min_samples_leaf=250, l2_regularization=10,
                    early_stopping=False, random_state=27),
    }
    if "flow1" in train:
        from sklearn.base import clone
        models = {"flow_boost":clone(models["boost"])}
    for name,model in models.items():
        model.fit(train[FEATURES+(FLOW if name=="flow_boost" else [])], (train.net_return*10000).clip(-400,400))
    return models


def score(rows, name, models):
    if name in models:
        return models[name].predict(rows[FEATURES+(FLOW if name=="flow_boost" else [])])
    # Higher means more favorable for the oriented side.
    volume = np.log1p(rows.volume_ratio.clip(0,20))
    if name == "absorption":
        return (-rows.flow3).clip(lower=0)*rows.price_response.clip(lower=0)*volume
    if name == "flow_trend":
        return rows.flow_change.clip(lower=0)*rows.r12.clip(lower=0)*volume
    if name == "crowded_reversal":
        return (-rows.last_funding_bps).clip(lower=0)*(-rows.excess12).clip(lower=0)*volume
    if name == "sweep":
        return rows.wick_balance.clip(lower=0)*rows.location.clip(lower=0)*volume
    if name == "relative_reversal":
        return (-rows.excess12).clip(lower=0)*volume
    if name == "breakout":
        return rows.channel.clip(lower=0)*rows.r12.clip(lower=0)*volume
    if name == "pullback":
        return rows.r144.clip(lower=0)*(-rows.r3).clip(lower=0)*volume
    raise ValueError(name)


def replay(rows, scores, cutoff, regime):
    selected = rows.assign(score=scores)
    selected = selected[selected.score > cutoff]
    if regime == "active":
        selected = selected[(selected.dispersion >= 1) & (selected.volume_ratio >= 1.25)]
    # Choose side before inspecting its outcome. Tied sides supply no directional evidence.
    clock = "submit_at_ms" if "submit_at_ms" in rows else "entry_at_ms"
    keys = [clock,"symbol"]
    selected = selected.sort_values(keys+["score"], ascending=[True,True,False])
    selected = selected[~selected.duplicated(keys+["score"],keep=False)].drop_duplicates(keys)
    if clock == "submit_at_ms":
        return selected.drop(columns=FEATURES).to_dict("records")
    available, records = {}, []
    for trade in selected.drop(columns=FEATURES).to_dict("records"):
        symbol = trade["symbol"]
        if trade["entry_at_ms"] > available.get(symbol,0):
            records.append(trade)
            available[symbol] = trade["exit_at_ms"]
    return records


def signature():
    sha = hashlib.sha256()
    for filename in ("strategy_challenge.py","pullback_orders.py","signal_policy.py","signal_audit.py"):
        sha.update(Path(__file__).with_name(filename).read_text(encoding="utf-8").encode())
    return sha.hexdigest()


def dev_inputs(folder):
    return {symbol: hashlib.sha256((folder/(symbol+".npz")).read_bytes()).hexdigest() for symbol in SYMBOLS}


def ranking(profile):
    stats = list(profile["validation"].values())
    returns = [s["net_equity_return_percent"] for s in stats]
    return (min(s["trades"] for s in stats)>=20, min(returns), sum(returns))


def prediction_hash(scores):
    return hashlib.sha256(np.asarray(scores,dtype="<f8").round(8).tobytes()).hexdigest()


def select(folder, report):
    data = dataset(folder)
    train = partition(data,"2026-05-01","2026-08-01")
    cal = partition(data,"2026-08-01","2026-09-01")
    months = {"September":partition(data,"2026-09-01","2026-10-01"),
              "October":partition(data,"2026-10-01","2026-11-01")}
    models = fit_models(train)
    candidates = []
    for name in (*RULES,*LEARNERS):
        calibration = score(cal,name,models)
        scores = {month:score(rows,name,models) for month,rows in months.items()}
        for q in PERCENTILES:
            cutoff = float(np.quantile(calibration,q))
            for regime in ("all","active"):
                stats = {month:portfolio(replay(rows,scores[month],cutoff,regime))[0]
                         for month,rows in months.items()}
                candidates.append(dict(name=name,quantile=q,cutoff=cutoff,regime=regime,
                                       calibration_sha256=prediction_hash(calibration),validation=stats))
    del data,train,cal,months
    flow_data = dataset(folder,with_flow=True)
    flow_model = fit_models(partition(flow_data,"2026-05-01","2026-08-01"))
    calibration_rows = partition(flow_data,"2026-08-01","2026-09-01")
    months = {"September":partition(flow_data,"2026-09-01","2026-10-01"),
              "October":partition(flow_data,"2026-10-01","2026-11-01")}
    for name in FLOW_METHODS:
        calibration = score(calibration_rows,name,flow_model)
        scores = {month:score(rows,name,flow_model) for month,rows in months.items()}
        for q in (.995,.999):
            cutoff = float(np.quantile(calibration,q))
            for regime in ("all","active"):
                stats = {month:portfolio(replay(rows,scores[month],cutoff,regime))[0]
                         for month,rows in months.items()}
                candidates.append(dict(name=name,quantile=q,cutoff=cutoff,regime=regime,
                                       calibration_sha256=prediction_hash(calibration),validation=stats))
    del flow_data,months,calibration_rows
    for depth in (.006,.012,.020):
        limits = dataset(folder,depth=depth)
        cal = partition(limits,"2026-08-01","2026-09-01")
        months = {"September":partition(limits,"2026-09-01","2026-10-01"),
                  "October":partition(limits,"2026-10-01","2026-11-01")}
        for name in RULES:
            calibration = score(cal,name,{})
            scores = {month:score(rows,name,{}) for month,rows in months.items()}
            for q in (.75,.95):
                cutoff = float(np.quantile(calibration,q))
                stats = {month:order_book(replay(rows,scores[month],cutoff,"all"))[0]
                         for month,rows in months.items()}
                candidates.append(dict(name=name,quantile=q,cutoff=cutoff,regime="all",
                                       depth=depth,calibration_sha256=prediction_hash(calibration),validation=stats))
    best = max(candidates,key=ranking)
    write_json(report,dict(source_sha256=signature(),development_sha256=dev_inputs(folder),
                           selection="Prefer >=20 fills in each month, maximize worst monthly portfolio return, then total.",
                           selected=best,candidates=candidates))
    print(json.dumps(best),flush=True)


def holdout(folder, november, report):
    lock = json.loads(report.read_text(encoding="utf-8"))
    if lock["source_sha256"] != signature() or lock["development_sha256"]!=dev_inputs(folder):
        raise ValueError("Frozen selection differs from source/data")
    best = lock["selected"]
    models = {}
    with_flow = best["name"] in FLOW_METHODS
    depth = best.get("depth",0.)
    dev = dataset(folder,depth=depth,with_flow=with_flow)
    if best["name"] in (*LEARNERS,"flow_boost"):
        models = fit_models(partition(dev,"2026-05-01","2026-08-01"))
    calibration = score(partition(dev,"2026-08-01","2026-09-01"),best["name"],models)
    if prediction_hash(calibration)!=best["calibration_sha256"]:
        raise ValueError("Reproduced calibration scores differ from frozen model")
    del dev
    book = order_book if depth else portfolio
    test = partition(dataset(folder,november,depth,with_flow),"2026-11-01","2026-11-25")
    records = replay(test,score(test,best["name"],models),best["cutoff"],best["regime"])
    stats, fills = book(records)
    stress = book(records,2)[0]
    blocks = {start:book([r for r in records if ms(start)<=r.get("submit_at_ms",r["entry_at_ms"]) and r["label_deadline"]<ms(end)])[0]
              for start,end in [("2026-11-01","2026-11-09"),("2026-11-09","2026-11-17"),("2026-11-17","2026-11-25")]}
    passed = (ranking(best)[0] and ranking(best)[1]>0 and stats["trades"]>=30
              and (stats["profit_factor"] or 0)>1.1 and stress["net_equity_return_percent"]>0
              and all(b["trades"]>=5 and b["net_equity_return_percent"]>0 for b in blocks.values()))
    result = dict(selection_sha256=hashlib.sha256(report.read_bytes()).hexdigest(),
                  november_sha256=dev_inputs(november),selected=best,period="2026-11-01 / 2026-11-24 UTC",
                  candidates_tested_on_holdout=1,stats=stats,double_costs=stress,blocks=blocks,
                  active_days=len({r["entry_at_ms"]//86_400_000 for r in fills}),
                  symbols=sorted({r["symbol"] for r in fills}),passed=bool(passed))
    write_json(report.with_suffix(".test.json"),result)
    pd.DataFrame(fills).to_csv(report.with_suffix(".fills.csv"), index=False)
    print(json.dumps(result),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action",choices=("select","holdout"))
    for arg in ("data","november","report"):
        parser.add_argument("--"+arg,type=Path,required=arg!="november")
    args=parser.parse_args()
    with threadpool_limits(limits=2):
        if args.action=="select":
            select(args.data,args.report)
        else:
            if not args.november:
                parser.error("holdout requires --november")
            holdout(args.data,args.november,args.report)


if __name__=="__main__":
    main()
