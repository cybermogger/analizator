"""Conservative resting-limit replay for the multi-market challenger."""
import numpy as np
import pandas as pd

from .signal_audit import summarize


def limit_outcomes(data, anchors, side, atr, depth, policy, steps):
    """Place at signal close. Three bars to fill, expiry two hours from submission."""
    submit = anchors+1
    distance = np.maximum(depth, 2.5*atr)
    price = data["close"][anchors]*(1-side*distance)
    stop = np.maximum(.0045, 1.6*atr)
    loss = stop*(1-side*policy.exit_fee)+policy.cost
    goal = np.maximum(policy.minimum_margin_profit/policy.leverage, policy.minimum_net_reward_risk*loss)
    target = (goal+policy.cost)/(1-side*policy.exit_fee)
    sl, tp = price*(1-side*stop), price*(1+side*target)
    n = len(anchors)
    entry = np.full(n,-1)
    end = submit+2
    reason = np.full(n, "EXPIRED", dtype="<U12")
    exit_price = price.copy()
    active = np.zeros(n,dtype=bool)
    for k in range(steps):
        j = submit+k
        opening, hi, lo, close = (data[key][j] for key in ("open","high","low","close"))
        new = np.zeros(n,dtype=bool)
        if k < 3:
            # Strict penetration of 5 bps. Charge the limit even on a favorable opening gap.
            new = (entry<0) & ((lo<=price*.9995) if side==1 else (hi>=price*1.0005))
            entry[new] = j[new]
            active |= new
        gap = active & (side*(opening-sl)<=0)
        stopped = active & ((lo<=sl) if side==1 else (hi>=sl))
        touched = active & ((hi>=tp) if side==1 else (lo<=tp))
        # On a fill bar an earlier wick cannot take profit on a later entry.
        target_hit = touched & (~new | (side*(close-tp)>=0))
        finished = stopped | target_hit | (active & (k==steps-1))
        end[finished] = j[finished]
        exit_price[finished] = np.where(gap, opening, np.where(stopped,sl,np.where(target_hit,tp,close)))[finished]
        reason[finished] = "TIME"
        reason[target_hit] = "TP"
        reason[stopped] = "SL"
        reason[stopped & touched] = "SL_AMBIGUOUS"
        reason[gap] = "SL_GAP"
        active &= ~finished
    filled = entry>=0
    ft = data["funding_time"]
    r = data["funding_rate"]*side
    csum = np.r_[0., np.cumsum(r)]
    charges = np.r_[0., np.cumsum(np.maximum(0,r))]
    entry_ms = data["open_time"][np.maximum(0,entry)].astype(np.int64)
    first = np.searchsorted(ft,entry_ms,"left")
    after = np.searchsorted(ft,entry_ms,"right")
    last = np.searchsorted(ft,data["open_time"][end],"right")
    funding = np.where(filled,csum[last]-csum[after]+charges[after]-charges[first],0.)
    ratio = exit_price/price
    gross = side*(ratio-1)
    fees = np.where(filled,policy.entry_fee+policy.exit_fee*ratio,0.)
    spread = np.where(filled,policy.spread,0.)
    slip = np.where(filled,policy.slippage,0.)
    return pd.DataFrame({
        "submit_at_ms":data["open_time"][submit].astype(np.int64),
        "entry_at_ms":np.where(filled,entry_ms,-1),
        "exit_at_ms":data["close_time"][end].astype(np.int64),
        "label_deadline":data["close_time"][submit+steps-1].astype(np.int64),
        "entry":price,"exit":exit_price,"stop_loss":sl,"take_profit":tp,"exit_reason":reason,
        "gross_return":gross,"net_return":gross-fees-spread-slip-funding,
        "fees":fees,"spread":spread,"slippage":slip,"funding":funding,
        "planned_margin_risk":loss*policy.leverage,"leverage":policy.leverage,
        "direction":"LONG" if side==1 else "SHORT",
        "liquidity_notional_cap":data["volume"][anchors]*data["close"][anchors]*.01,
    })


def order_book(rows, stress=1.):
    """Reserve slots and margin at submission, including orders that never fill."""
    equity = peak = 10000.
    drawdown = 0.
    pending, fills = {}, []
    submitted = rejected = 0
    def settle(now):
        nonlocal equity, peak, drawdown
        due = sorted((pos for pos in pending.values() if pos["exit_at_ms"]<=now),
                     key=lambda p:(p["exit_at_ms"],p["symbol"]))
        for pos in due:
            del pending[pos["symbol"]]
            equity += pos["pnl"]
            peak = max(peak,equity)
            drawdown = max(drawdown,1-equity/peak)

    for row in sorted(rows,key=lambda x:(x["submit_at_ms"],-x["score"],x["symbol"])):
        settle(row["submit_at_ms"])
        symbol = row["symbol"]
        if symbol in pending or len(pending)>=4 or equity<=0:
            rejected += 1
            continue
        margin = min(equity*.10,equity*.0025/row["planned_margin_risk"],row["liquidity_notional_cap"]/row["leverage"])
        if margin<=0 or margin+sum(p["margin"] for p in pending.values())>equity*.40+1e-8:
            rejected += 1
            continue
        net = row["net_return"]-(stress-1)*(row["fees"]+row["spread"]+row["slippage"])
        notional = margin*row["leverage"]
        pos = dict(row,margin=margin,notional=notional,pnl=notional*net,net_return=net)
        pending[symbol] = pos
        submitted += 1
        if row["exit_reason"]!="EXPIRED":
            fills.append(pos)
    settle(float("inf"))
    stats = summarize(fills)
    gains = sum(p["pnl"] for p in fills if p["pnl"]>0)
    losses = -sum(p["pnl"] for p in fills if p["pnl"]<0)
    stats.update(profit_factor=gains/losses if losses else None,orders=submitted,unfilled=submitted-len(fills),
                 skipped_capacity=rejected,net_equity_return_percent=(equity/10000-1)*100,
                 max_closed_equity_drawdown_percent=drawdown*100,initial_equity=10000,final_equity=equity)
    return stats,fills
