"""Shared causal signal and cost definitions for live plans and historical audit."""
from dataclasses import dataclass, asdict
import math
import numpy as np
import pandas as pd

FEATURES=['return_1','dist_ema_21','rsi_14','atr_pct','relative_volume']

@dataclass(frozen=True)
class ProfitPolicy:
    leverage: float = 5.0
    minimum_margin_profit: float = .03
    minimum_net_reward_risk: float = 2.0
    maximum_margin_loss: float = .05
    entry_fee: float = .0005
    exit_fee: float = .0005
    spread: float = .0002
    slippage: float = .0004
    funding_reserve: float = .0002

    def __post_init__(self):
        if not all(math.isfinite(value) for value in self.__dict__.values()):raise ValueError('Nonfinite policy')
        if not 1<=self.leverage<=10:raise ValueError('Research leverage must be between 1 and 10')
        if self.minimum_margin_profit<=0 or self.minimum_net_reward_risk<1 or not 0<self.maximum_margin_loss<=.1:raise ValueError('Invalid profit/risk policy')
        if min(self.entry_fee,self.exit_fee,self.spread,self.slippage,self.funding_reserve)<0:raise ValueError('Negative execution cost')

    @property
    def cost(self):
        return self.entry_fee+self.exit_fee+self.spread+self.slippage+self.funding_reserve

    def distances(self,atr_fraction,direction):
        sign=1 if direction=='LONG' else -1
        stop=max(.002,1.25*float(atr_fraction))
        net_loss=stop*(1-sign*self.exit_fee)+self.cost
        wanted=max(self.minimum_margin_profit/self.leverage,self.minimum_net_reward_risk*net_loss)
        target=(wanted+self.cost)/(1-sign*self.exit_fee)
        return stop,target,net_loss,wanted

    def plan(self,entry,atr_fraction,direction):
        stop,target,loss,profit=self.distances(atr_fraction,direction)
        sign=1 if direction=='LONG' else -1
        return {'direction':direction,'entry':float(entry),'stop_loss':entry*(1-sign*stop),
            'take_profit':entry*(1+sign*target),'stop_distance_percent':stop*100,
            'target_distance_percent':target*100,'reward_risk':target/stop,
            'reward_risk_after_costs':profit/loss,'estimated_roundtrip_cost_percent':self.cost*100,
            'leverage':self.leverage,'target_net_margin_percent':profit*self.leverage*100,
            'stop_net_margin_percent':loss*self.leverage*100,
            'risk_allowed':loss*self.leverage<=self.maximum_margin_loss,
            'profit_basis':'net return on isolated margin; not on account equity',
            'basis':'min 3% net margin ROI and 2:1 net reward/risk; 1.25 ATR stop'}


def causal_features(frame):
    """Only the live model's five features; no future or centered windows."""
    close=frame.close
    delta=close.diff()
    gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False,min_periods=14).mean()
    rsi=(100-100/(1+gain/loss.replace(0,np.nan))).where(loss>0,100).where(gain>0,0).where(gain+loss>0,50)
    tr=pd.concat([frame.high-frame.low,(frame.high-close.shift()).abs(),(frame.low-close.shift()).abs()],axis=1).max(axis=1)
    result=pd.DataFrame({'return_1':close.pct_change(),
        'dist_ema_21':close/close.ewm(span=21,adjust=False,min_periods=21).mean()-1,
        'rsi_14':rsi,'atr_pct':tr.ewm(alpha=1/14,adjust=False,min_periods=14).mean()/close,
        'relative_volume':frame.volume/frame.volume.rolling(20).mean()})
    return result


def first_exit(frame,entry_index,sign,stop_fraction,target_fraction,steps):
    """Next-bar open entry. Gap stops fill worse; ambiguous candles stop first."""
    entry=float(frame['open'][entry_index]);stop=entry*(1-sign*stop_fraction);target=entry*(1+sign*target_fraction)
    final=min(len(frame['open'])-1,entry_index+steps-1)
    for j in range(entry_index,final+1):
        op,hi,lo=frame['open'][j],frame['high'][j],frame['low'][j]
        if (sign==1 and op<=stop) or (sign==-1 and op>=stop):return j,float(op),'SL_GAP'
        hit_stop=lo<=stop if sign==1 else hi>=stop
        hit_target=hi>=target if sign==1 else lo<=target
        if hit_stop:return j,float(stop),'SL_AMBIGUOUS' if hit_target else 'SL'
        if hit_target:return j,float(target),'TP'
    return final,float(frame['close'][final]),'TIME'


def policy_labels(frame,features,steps,policy):
    """LONG/SHORT mean TP before own SL, WAIT includes stops/timeouts.

    Opposing TP-before-SL events are mutually exclusive with these barriers.
    Ambiguous within-bar ordering is conservatively treated as no TP.
    """
    n=len(frame);labels=np.full(n,np.nan);expectations=np.full((n,2),np.nan)
    arrays={k:frame[k].to_numpy() for k in ['open','high','low','close']}
    atr=features.atr_pct.to_numpy()
    for i in range(30,n-steps):
        outcomes=[]
        for side,sign in [('LONG',1),('SHORT',-1)]:
            stop,target,_,_=policy.distances(atr[i],side)
            j,exit_price,reason=first_exit(arrays,i+1,sign,stop,target,steps)
            ratio=exit_price/arrays['open'][i+1]
            outcomes.append(reason=='TP')
            expectations[i,0 if sign==1 else 1]=sign*(ratio-1)-policy.entry_fee-policy.exit_fee*ratio-policy.spread-policy.slippage-policy.funding_reserve
        labels[i]=0 if outcomes[0] and not outcomes[1] else 1 if outcomes[1] and not outcomes[0] else 2
    return labels,expectations


def posterior(features,labels,index,steps,window=300,warmup=30):
    begin=max(warmup,index-window+1+warmup)
    end=index-2*steps
    if end-begin<60:return None
    history=features[begin:end]
    valid=np.isfinite(history).all(axis=1)&np.isfinite(labels[begin:end])
    history=history[valid]
    if len(history)<60 or not np.isfinite(features[index]).all():return None
    scale=np.std(history,axis=0);scale[scale==0]=1
    distances=np.mean(((history-features[index])/scale)**2,axis=1)
    nearest=np.argpartition(distances,59)[:60]
    ids=np.flatnonzero(valid)[nearest]+begin
    counts=np.bincount(labels[ids].astype(int),minlength=3)
    return (counts+np.array([3.,3.,6.]))/72,ids
