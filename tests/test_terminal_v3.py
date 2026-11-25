import asyncio,json
from pathlib import Path
import pytest
from src.dashboard.ranking import best_signal
from src.data.storage import SQLiteStorage
from src.events.translation import NewsTranslator
from setup_x_rules import build_rules

def row(identity,p,h='15m',received=100000):
    return dict(instrument_id=identity,horizon=h,status='ESTIMATE',quote_received_at_ms=received,
        probabilities={'LONG':p,'SHORT':100-p-10,'WAIT':10},eligible=False)

def test_best_selects_max_long_or_short_across_horizons_and_changes():
    a=row('A',60);b=row('B',15,'5m')
    assert best_signal([a,b],100001)['leader']['instrument_id']=='B'
    c=row('C',85,'1h')
    winner=best_signal([a,b,c],100001)['leader']
    assert winner['instrument_id']=='C' and winner['leading_side']=='LONG'
    assert winner['eligible'] is False
    assert best_signal([a,b,c],140001)['leader'] is None

def test_best_rejects_bad_percentages_and_future_quotes():
    bad=row('A',80);bad['probabilities']['WAIT']=200
    assert best_signal([bad,row('future',70,received=200000)],100000)['leader'] is None

def test_local_translation_cache_preserves_original_and_never_sends_network():
    storage=SQLiteStorage(':memory:');translator=NewsTranslator(storage)
    translator.model_path=Path(__file__).parent
    translator.view=lambda text: {'summary_ru':None,'translation_status':'PENDING'}
    translator._translate=lambda text:'Решение о ставке'
    asyncio.run(translator.translate('Interest rate decision'))
    saved=storage.query('SELECT * FROM news_translation_ru_v1')[0]
    assert saved['original']=='Interest rate decision' and saved['translated']=='Решение о ставке'
    storage.close()

def test_missing_model_is_explicit_and_russian_is_not_retranslated(tmp_path):
    storage=SQLiteStorage(':memory:');translator=NewsTranslator(storage);translator.model_path=tmp_path
    assert translator.view('Official announcement')['translation_status']=='MODEL_MISSING'
    assert translator.view('Изменение процентной ставки')['summary_ru']=='Изменение процентной ставки'
    storage.close()

def test_watchlist_and_rules_are_exactly_one_hundred_candidates():
    source=json.loads((Path(__file__).parents[1]/'config/x_watchlist.json').read_text(encoding='utf-8'))
    accounts=source['accounts'];assert len(accounts)==100
    assert len({a['handle'].lower() for a in accounts})==100
    assert all(a['identity_status']=='PENDING_ID_VERIFICATION' for a in accounts)
    assert all(len(rule['value'])<512 for rule in build_rules(accounts))


def test_periodic_database_health_does_not_scan_pages(monkeypatch):
    storage = SQLiteStorage(':memory:')
    query = storage.query
    statements = []
    def record(sql, params=()):
        statements.append(sql)
        return query(sql, params)
    monkeypatch.setattr(storage, 'query', record)
    assert storage.health(deep=False)['integrity'] == 'not_checked'
    assert not any('PRAGMA' in sql for sql in statements)
    assert storage.health()['integrity'] == 'ok'
    storage.close()
