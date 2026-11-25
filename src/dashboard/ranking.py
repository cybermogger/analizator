"""Presentation ranking, not a model promotion or cross-venue asset merge."""
import math

def best_signal(rows, now_ms):
    ranked=[]
    for row in rows:
        p=row.get('probabilities') or {}
        received=int(row.get('quote_received_at_ms') or 0)
        if row.get('status')!='ESTIMATE' or not 0<received<=now_ms or now_ms-received>30000:
            continue
        values=[p.get(k) for k in ('LONG','SHORT','WAIT')]
        if any(not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=100 for v in values): continue
        if not math.isclose(sum(values),100,abs_tol=.01): continue
        side='LONG' if p['LONG']>=p['SHORT'] else 'SHORT'
        ranked.append({**row,'leading_side':side,'leading_percent':p[side]})
    ranked.sort(key=lambda r:(-r['leading_percent'],r['instrument_id'],r['horizon']))
    runners=[]
    seen={ranked[0].get('base',ranked[0]['instrument_id'])} if ranked else set()
    for row in ranked[1:]:
        display=row.get('base',row['instrument_id'])
        if display not in seen:
            runners.append(row);seen.add(display)
        if len(runners)==4: break
    return {'leader':ranked[0] if ranked else None,'runners_up':runners,
        'selection_basis':'maximum_directional_estimate',
        'semantics':'Максимальная оценка LONG/SHORT среди свежих инструментов и горизонтов 5m/15m/1h. Это не доказанная вероятность прибыли; WAIT и фильтры входа сохраняются.'}
