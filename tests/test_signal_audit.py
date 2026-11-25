import copy
import numpy as np
import pytest
from src.research.signal_policy import ProfitPolicy,first_exit,causal_features,policy_labels,posterior,FEATURES
from src.research.signal_audit import portfolio,trade_record
from src.service.market_forecast import MarketForecastService
from tests.test_market_forecast import setup_engine


@pytest.mark.parametrize('direction',['LONG','SHORT'])
def test_profit_target_covers_costs_and_net_two_to_one_risk(direction):
    policy=ProfitPolicy()
    plan=policy.plan(100,.002,direction)
    sign=1 if direction=='LONG' else -1
    ratio=plan['take_profit']/plan['entry']
    net=sign*(ratio-1)-policy.entry_fee-policy.exit_fee*ratio-policy.spread-policy.slippage-policy.funding_reserve
    assert net*policy.leverage>=.03-1e-12
    assert plan['reward_risk_after_costs']>=2
    assert plan['leverage']==5
    assert plan['stop_loss']<100<plan['take_profit'] if direction=='LONG' else plan['take_profit']<100<plan['stop_loss']


def test_stops_win_ambiguous_candles_and_gaps_fill_worse():
    data={'open':np.array([100.,90.]),'high':np.array([104.,92.]),'low':np.array([97.,89.]),'close':np.array([100.,91.])}
    assert first_exit(data,0,1,.02,.03,2)==(0,98.,'SL_AMBIGUOUS')
    data['high'][0]=101;data['low'][0]=99
    assert first_exit(data,0,1,.02,.03,2)==(1,90.,'SL_GAP')


def test_unseen_future_cannot_change_historical_posterior(candles):
    policy=ProfitPolicy();features=causal_features(candles)
    labels,_=policy_labels(candles,features,24,policy)
    old=posterior(features[FEATURES].to_numpy(),labels,400,24)
    changed=candles.copy();changed.loc[401:,['open','high','low','close']]*=10
    new_features=causal_features(changed);new_labels,_=policy_labels(changed,new_features,24,policy)
    new=posterior(new_features[FEATURES].to_numpy(),new_labels,400,24)
    assert np.array_equal(old[0],new[0]) and np.array_equal(old[1],new[1])
    assert max(old[1])+24<400


def test_realized_fees_funding_and_short_signs_reconcile():
    arrays={'open':np.array([100.,100.,100.]),'high':np.array([101.,101.,101.]),'low':np.array([99.,99.,99.]),
        'close':np.array([100.,100.,100.]),'open_time':np.array([0,300000,600000]),'close_time':np.array([299999,599999,899999]),
        'funding_time':np.array([600000]),'funding_rate':np.array([.001])}
    policy=ProfitPolicy()
    long=trade_record('TEST','test',arrays,0,1,.05,.10,2,policy)
    short=trade_record('TEST','test',arrays,0,-1,.05,.10,2,policy)
    assert long['funding']==.001 and short['funding']==-.001
    assert long['net_return']==pytest.approx(-.0026)
    assert short['net_return']==pytest.approx(-.0006)


def test_account_return_is_not_sum_of_leveraged_trade_percentages():
    rows=[{'symbol':str(i),'entry_at_ms':100,'exit_at_ms':200,'net_return':.01,'planned_margin_risk':.02,
        'exit_reason':'TP','fees':.001,'spread':.0002,'slippage':.0004} for i in range(5)]
    result,filled=portfolio(rows)
    assert len(filled)==4 and result['skipped_capacity']==1
    assert result['final_equity']==pytest.approx(10200)
    assert result['net_equity_return_percent']==pytest.approx(2)


def test_live_policy_never_promotes_an_unvalidated_three_percent_plan(candles):
    old,card,now=setup_engine(candles)
    engine=MarketForecastService(old.analysis,old.storage,{'profit_policy_enabled':True,'require_historical_audit':True})
    engine.audit={}
    result=engine.forecast(card,'2h',now)
    assert result['probabilities'] is not None
    assert result['trade_plan']['target_net_margin_percent']>=3
    assert result['trade_plan']['reward_risk_after_costs']>=2
    assert not result['eligible'] and result['trade_plan']['status']=='CONDITIONAL'
    assert any('проверка' in reason for reason in result['reasons'])
    assert 'Плечо 5' in engine.format(result)
    old.storage.close()


def test_invalid_leverage_and_costs_are_rejected():
    for kwargs in ({'leverage':100},{'entry_fee':-.1},{'leverage':float('nan')}):
        with pytest.raises(ValueError):ProfitPolicy(**kwargs)
