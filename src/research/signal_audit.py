"""Reproducible public-data replay. Never reads live databases or credentials."""
import argparse,asyncio,hashlib,io,json,zipfile,subprocess,re
from pathlib import Path
import numpy as np
import pandas as pd
import aiohttp
from .signal_policy import ProfitPolicy,causal_features,posterior,policy_labels,first_exit,FEATURES

MONTHS=[f'2026-{month:02d}' for month in range(5,11)]
SYMBOLS=['BTCUSDT','ETHUSDT','BNBUSDT','SOLUSDT','XRPUSDT','DOGEUSDT','ADAUSDT','LINKUSDT','AVAXUSDT','DOTUSDT','SUIUSDT','LTCUSDT']
PROFILES={'legacy_5m':('legacy',1),'legacy_15m':('legacy',3),'legacy_1h':('legacy',12),
    'profit_1h':('profit',12),'profit_2h':('profit',24),
    'trend_1h':('trend',12),'trend_2h':('trend',24),
    'breakout_1h':('breakout',12),'breakout_2h':('breakout',24)}

def write_json(path,value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')

async def fetch_symbol(symbol,destination):
    destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
    frames=[];funding=[];sources=[]
    archive_folder=destination/'public_archives';archive_folder.mkdir(exist_ok=True)
    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(force_close=True,limit=4),timeout=aiohttp.ClientTimeout(total=6)) as session:
        async def curl_bytes(url,byte_range=None,with_headers=False):
            args=['curl.exe','--noproxy','*','--fail','--silent','--show-error','--max-time','6']
            if byte_range:args+=['--range',byte_range]
            if with_headers:args+=['--dump-header','-']
            args.append(url)
            for attempt in range(4):
                def request():
                    return subprocess.run(args,capture_output=True,timeout=8,
                        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                result=await asyncio.to_thread(request)
                if result.returncode==0:return result.stdout
                if attempt==3:raise OSError('Public archive curl transfer failed')
        async def archive_bytes(url):
            response=await curl_bytes(url,'0-16383',True)
            header,first=response.split(b'\r\n\r\n',1)
            match=re.search(rb'content-range:\s*bytes \d+-\d+/(\d+)',header,re.I)
            if not match:return first
            total=int(match.group(1));gate=asyncio.Semaphore(4)
            async def part(start):
                end=min(total-1,start+16383)
                async with gate:
                    body=await curl_bytes(url,f'{start}-{end}')
                    if len(body)!=end-start+1:raise ValueError('Truncated archive range')
                    return body
            return first+b''.join(await asyncio.gather(*(part(start) for start in range(len(first),total,16384))))
        for month in MONTHS:
            for kind in ('klines','fundingRate'):
                segment=f'klines/{symbol}/5m/{symbol}-5m-{month}' if kind=='klines' else f'fundingRate/{symbol}/{symbol}-fundingRate-{month}'
                url=f'https://data.binance.vision/data/futures/um/monthly/{segment}.zip'
                for attempt in range(3):
                    try:
                        checksum=(await curl_bytes(url+'.CHECKSUM')).decode().split()[0]
                        stored=archive_folder/(segment.rsplit('/',1)[-1]+'.zip')
                        body=stored.read_bytes() if stored.is_file() else await archive_bytes(url)
                        if hashlib.sha256(body).hexdigest()!=checksum:raise ValueError('Archive checksum mismatch')
                        if not stored.is_file():stored.write_bytes(body)
                        print(json.dumps({'verified':symbol,'month':month,'kind':kind,'bytes':len(body)}),flush=True)
                        break
                    except (aiohttp.ClientError,asyncio.TimeoutError,OSError):
                        if attempt==2:raise
                sources.append({'url':url,'sha256':checksum,'bytes':len(body)})
                with zipfile.ZipFile(io.BytesIO(body)) as archive:
                    names=[name for name in archive.namelist() if name.endswith('.csv')]
                    if len(names)!=1:raise ValueError('Expected one historical CSV')
                    content=archive.read(names[0])
                if kind=='klines':
                    frame=pd.read_csv(io.BytesIO(content),header=None,low_memory=False)
                    frame=frame[pd.to_numeric(frame[0],errors='coerce').notna()]
                    frame=frame.iloc[:,:7].astype(float)
                    frame.columns=['open_time','open','high','low','close','volume','close_time']
                    if frame.open_time.max()>1e14:frame[['open_time','close_time']]/=1000
                    frames.append(frame)
                else:
                    frame=pd.read_csv(io.BytesIO(content))
                    frame=frame.rename(columns={'calc_time':'timestamp','last_funding_rate':'rate'})
                    if not {'timestamp','rate'}<=set(frame.columns):raise ValueError(f'Unexpected funding columns: {list(frame.columns)}')
                    funding.append(frame[['timestamp','rate']].astype(float))
    bars=pd.concat(frames).sort_values('open_time').drop_duplicates('open_time').reset_index(drop=True)
    rates=pd.concat(funding).sort_values('timestamp').drop_duplicates('timestamp')
    if not np.isfinite(bars.to_numpy()).all() or not (bars[['open','high','low','close']]>0).all().all():raise ValueError('Invalid historical price')
    if not ((bars.low<=bars[['open','close']].min(axis=1))&(bars.high>=bars[['open','close']].max(axis=1))).all():raise ValueError('Invalid historical OHLC')
    np.savez_compressed(destination/(symbol+'.npz'),**{c:bars[c].to_numpy() for c in bars},
        funding_time=rates.timestamp.to_numpy(),funding_rate=rates.rate.to_numpy())
    write_json(destination/(symbol+'-sources.json'),{'symbol':symbol,'sources':sources,'rows':len(bars),
        'first_ms':int(bars.open_time.iloc[0]),'last_ms':int(bars.close_time.iloc[-1]),
        'gaps':int((bars.open_time.diff().dropna()!=300_000).sum())})
    print(json.dumps({'downloaded':symbol,'bars':len(bars),'funding_events':len(rates),'checksum_verified_files':len(sources)}),flush=True)


def legacy_labels(arrays,atr,steps):
    n=len(atr);result=np.full(n,np.nan)
    close=arrays['close'];high=arrays['high'];low=arrays['low']
    width=np.clip(1.25*np.maximum(atr,.001),.001,.20)
    upper=close*(1+width);lower=close*(1-width)
    pending=np.arange(n-steps)
    result[:n-steps]=2
    for offset in range(1,steps+1):
        up=high[pending+offset]>=upper[pending];down=low[pending+offset]<=lower[pending]
        result[pending[up&~down]]=0;result[pending[down&~up]]=1
        pending=pending[~(up|down)]
    return result


def trade_record(symbol,profile,arrays,i,sign,stop,target,steps,policy):
    entry_index=i+1
    j,exit_price,reason=first_exit(arrays,entry_index,sign,stop,target,steps)
    entry=float(arrays['open'][entry_index]);ratio=exit_price/entry
    entry_at=int(arrays['open_time'][entry_index]);exit_at=int(arrays['close_time'][j])
    mask=(arrays['funding_time']>=entry_at)&(arrays['funding_time']<=int(arrays['open_time'][j]))
    funding=float((arrays['funding_rate'][mask]*sign).sum())
    gross=sign*(ratio-1)
    fees=policy.entry_fee+policy.exit_fee*ratio
    costs=fees+policy.spread+policy.slippage+funding
    return {'symbol':symbol,'profile':profile,'direction':'LONG' if sign==1 else 'SHORT',
        'entry_at_ms':entry_at,'exit_at_ms':exit_at,'entry':entry,'exit':exit_price,'exit_reason':reason,
        'stop_fraction':stop,'target_fraction':target,'gross_return':gross,'fees':fees,
        'spread':policy.spread,'slippage':policy.slippage,'funding':funding,'net_return':gross-costs,
        'margin_return':policy.leverage*(gross-costs),'leverage':policy.leverage,
        'planned_margin_risk':policy.leverage*(stop+policy.cost),
        'liquidity_notional_cap':float(arrays.get('volume',np.full(len(arrays['open']),float('inf')))[i]*arrays['close'][i]*.01),
        'exit_index':j}


def replay_symbol(symbol,destination):
    folder=Path(destination)
    with np.load(folder/(symbol+'.npz')) as source:arrays={key:source[key] for key in source.files}
    frame=pd.DataFrame({c:arrays[c] for c in ['open_time','open','high','low','close','volume']})
    features=causal_features(frame);matrix=features[FEATURES].to_numpy();atr=features.atr_pct.to_numpy()
    policy=ProfitPolicy()
    labels={};expectations={}
    for steps in (1,3,12):labels['legacy',steps]=legacy_labels(arrays,atr,steps)
    for steps in (12,24):labels['profit',steps],expectations[steps]=policy_labels(frame,features,steps,policy)
    # Prespecified filters. Only validation decides which one, if any, survives.
    ema50=frame.close.ewm(span=50,adjust=False,min_periods=50).mean().to_numpy()
    high20=frame.high.rolling(20).max().shift(1).to_numpy();low20=frame.low.rolling(20).min().shift(1).to_numpy()
    gap=np.r_[False,np.diff(arrays['open_time'])!=300_000]
    gap_prefix=np.cumsum(gap)
    trade_lists={name:[] for name in PROFILES};next_allowed={name:300 for name in PROFILES}
    for i in range(300,len(frame)-25,3):
        if gap_prefix[i+24]-gap_prefix[max(0,i-299)]>0:continue
        post={}
        for key,label in labels.items():post[key]=posterior(matrix,label,i,key[1],warmup=20 if key[0]=='legacy' else 30)
        for name,(kind,steps) in PROFILES.items():
            if i<next_allowed[name]:continue
            legacy=kind=='legacy';result=post['legacy' if legacy else 'profit',steps]
            if result is None:continue
            p,nearest=result;side=int(np.argmax(p))
            if side==2 or p[side]<(0.60 if legacy else .45) or abs(p[0]-p[1])<.20:continue
            sign=1 if side==0 else -1;direction='LONG' if sign==1 else 'SHORT'
            if legacy:
                mean=float(np.mean(arrays['close'][nearest+steps]/arrays['close'][nearest]-1))*sign
                # Reproduce the old 4 bps/side signal gate, then charge the
                # verified standard 5 bps/side in realized PnL below.
                if mean-.0014<=0:continue
                stop=target=float(np.clip(max(.00125,1.25*atr[i]),.00125,.20))
            else:
                stop,target,loss,_=policy.distances(atr[i],direction)
                if loss*policy.leverage>policy.maximum_margin_loss:continue
                if float(expectations[steps][nearest,side].mean())<=0:continue
                if kind in ('trend','breakout'):
                    if sign*(arrays['close'][i]/ema50[i]-1)<=0:continue
                    if sign*(features.rsi_14.iloc[i]-50)<2:continue
                    if features.relative_volume.iloc[i]<1:continue
                if kind=='breakout' and not (arrays['close'][i]>high20[i] if sign==1 else arrays['close'][i]<low20[i]):continue
            trade=trade_record(symbol,name,arrays,i,sign,stop,target,steps,policy)
            trade['directional_estimate']=float(p[side]);trade_lists[name].append(trade)
            next_allowed[name]=max(i+3,trade['exit_index']+1)
    write_json(folder/(symbol+'-trades.json'),trade_lists)
    print(json.dumps({'replayed':symbol,'trades':{name:len(rows) for name,rows in trade_lists.items()}}),flush=True)


def summarize(rows):
    n=len(rows);returns=np.array([r['net_return'] for r in rows]);wins=returns>0
    gain=float(returns[wins].sum());loss=float(-returns[~wins].sum())
    return {'trades':n,'net_winrate_percent':float(wins.mean()*100) if n else None,
        'tp_hit_percent':sum(r['exit_reason']=='TP' for r in rows)/n*100 if n else None,
        'profit_factor':gain/loss if loss else None,
        'mean_net_price_percent':float(returns.mean()*100) if n else None,
        'mean_net_margin_percent':float(returns.mean()*500) if n else None,
        'sum_net_trade_percent':float(returns.sum()*100),
        'stop_exits':sum(r['exit_reason'].startswith('SL') for r in rows),
        'time_exits':sum(r['exit_reason']=='TIME' for r in rows)}


def portfolio(rows,stress=1.0):
    equity=10000.;peak=equity;drawdown=0.;active=[];filled=[];skipped=0
    def close_until(timestamp):
        nonlocal equity,peak,drawdown,active
        due=sorted([p for p in active if p['exit_at_ms']<=timestamp],key=lambda p:(p['exit_at_ms'],p['symbol']))
        active=[p for p in active if p['exit_at_ms']>timestamp]
        for p in due:
            equity+=p['pnl'];peak=max(peak,equity);drawdown=max(drawdown,1-equity/peak)
    for row in sorted(rows,key=lambda r:(r['entry_at_ms'],r['symbol'])):
        close_until(row['entry_at_ms'])
        if len(active)>=4 or equity<=0:skipped+=1;continue
        margin=min(equity*.10,equity*.0025/max(row['planned_margin_risk'],.001),row.get('liquidity_notional_cap',float('inf'))/5)
        if sum(p['margin'] for p in active)+margin>equity*.40+1e-8:skipped+=1;continue
        net=row['net_return']-(stress-1)*(row['fees']+row['spread']+row['slippage'])
        position={**row,'margin':margin,'notional':margin*5,'pnl':margin*5*net}
        active.append(position);filled.append(position)
    close_until(float('inf'))
    stats=summarize([{**p,'net_return':p['pnl']/p['notional']} for p in filled])
    gain=sum(p['pnl'] for p in filled if p['pnl']>0)
    loss=-sum(p['pnl'] for p in filled if p['pnl']<0)
    stats['profit_factor']=gain/loss if loss else None
    stats.update(initial_equity=10000,final_equity=equity,net_equity_return_percent=(equity/10000-1)*100,
        max_closed_equity_drawdown_percent=drawdown*100,skipped_capacity=skipped,
        maximum_concurrent_positions=4,margin_cap_percent=40,risk_per_trade_equity_percent=.25,
        drawdown_basis='closed equity only; no intrabar mark-to-market portfolio path',cost_multiplier=stress)
    return stats,filled


def aggregate(destination,report_path):
    folder=Path(destination);all_rows={name:[] for name in PROFILES};data=[]
    for symbol in SYMBOLS:
        content=json.loads((folder/(symbol+'-trades.json')).read_text(encoding='utf-8'))
        for name,rows in content.items():all_rows[name].extend(rows)
        data.append(json.loads((folder/(symbol+'-sources.json')).read_text(encoding='utf-8')))
    ms=lambda value:int(pd.Timestamp(value,tz='UTC').timestamp()*1000)
    periods={'train':(ms('2026-05-01'),ms('2026-09-01')),'validation':(ms('2026-09-01'),ms('2026-10-01')),'test':(ms('2026-10-01'),ms('2026-11-01'))}
    def subset(name,part):
        start,end=periods[part]
        return [r for r in all_rows[name] if r['entry_at_ms']>=start and r['exit_at_ms']<end-7_200_000]
    tuning={name:{part:portfolio(subset(name,part))[0] for part in ('train','validation')} for name in PROFILES}
    choices=[name for name in PROFILES if not name.startswith('legacy')]
    # This choice sees validation only. The test data is summarized afterwards.
    champion=max(choices,key=lambda name:(tuning[name]['validation']['net_equity_return_percent'] if tuning[name]['validation']['trades']>=30 else -1e9,name))
    evaluated=['legacy_5m','legacy_15m','legacy_1h',champion]
    test={name:portfolio(subset(name,'test'))[0] for name in evaluated}
    stress={name:portfolio(subset(name,'test'),2)[0] for name in evaluated}
    winner=test[champion]
    # No automatic model promotion from one favorable aggregate number.
    blocks=[]
    for start,end in [('2026-10-01','2026-10-11'),('2026-10-11','2026-10-21'),('2026-10-21','2026-11-01')]:
        blocks.append(portfolio([r for r in subset(champion,'test') if ms(start)<=r['entry_at_ms'] and r['exit_at_ms']<ms(end)])[0])
    validation=tuning[champion]['validation']
    passed=validation['trades']>=30 and validation['net_equity_return_percent']>0 and winner['trades']>=30 and winner['net_equity_return_percent']>0 and (winner['profit_factor'] or 0)>1.1 and all(b['net_equity_return_percent']>0 and b['trades']>=5 for b in blocks) and stress[champion]['net_equity_return_percent']>0
    report={'protocol_version':'signal-audit-v1','period':'2026-05-01 / 2026-10-31 UTC','symbols':SYMBOLS,
        'minimum_directional_percent':45,'decision_cadence_bars':3,
        'source_code_sha256':{name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ('signal_policy.py','signal_audit.py')},
        'data':data,'costs':ProfitPolicy().__dict__,'selection':'maximum September portfolio return among prespecified profiles with >=30 trades; October untouched for selection',
        'assumptions':['Decisions every 15m on closed 5m bars; entries next 5m open.',
            'Same rolling 300-bar neighbor model and causal embargo; no historical BBO/L2 available, so this is candle-logic replay, not exact alert-log replay.',
            'Stop-first if TP and SL touch in one bar. Stop gaps fill at worse open. TP fills at target, without favorable gap improvement.',
            'Actual funding timestamps/rates charged on entry notional; funding mark-price variation not reconstructed.',
            'One position per symbol/profile. Portfolio max 4, 0.25% equity risk, 10% equity margin/position, 5x isolated hypothetical leverage.',
            'Order notional capped at 1% of the previous closed 5m bar quote turnover; this is a proxy, not historical order-book depth.',
            'Legacy admission uses its former 14 bps cost estimate; realized results charge 5 bps taker fees on both sides plus 2 bps spread, 4 bps slippage and actual funding.',
            'Last two hours of partition exits purged. No overlapping trades within one instrument/profile.',
            'Fixed liquid-symbol sample has survivorship bias and does not validate every exchange or all market coins.',
            'Closed-equity drawdown understates possible floating drawdown; no order-book fills, liquidation or capacity guarantee.'],
        'tuning':tuning,'selected_profile':champion,'test':test,'test_double_costs':stress,'selected_test_blocks':blocks,
        'promotion_passed':passed,'promotion_status':'RESEARCH_PASS_REQUIRES_FORWARD_VALIDATION' if passed else 'BLOCKED_OUT_OF_SAMPLE'}
    report_path=Path(report_path);report_path.parent.mkdir(parents=True,exist_ok=True);write_json(report_path,report)
    for name in evaluated:
        _,rows=portfolio(subset(name,'test'))
        pd.DataFrame(rows).to_csv(report_path.with_name(name+'-test-trades.csv'),index=False)
    print(json.dumps({k:report[k] for k in ['selected_profile','test','test_double_costs','promotion_status']},ensure_ascii=False,indent=2),flush=True)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['fetch','replay','aggregate']);parser.add_argument('--symbol',choices=SYMBOLS);parser.add_argument('--data',required=True);parser.add_argument('--report')
    args=parser.parse_args()
    if args.action=='fetch':asyncio.run(fetch_symbol(args.symbol,args.data))
    elif args.action=='replay':replay_symbol(args.symbol,args.data)
    else:aggregate(args.data,args.report)
if __name__=='__main__':main()
