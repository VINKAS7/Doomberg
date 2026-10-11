"""Regression checks for the six daily-monitoring review findings; no live services."""
import numpy as np
import pytest

from risk_engine import calibration, contagion
from risk_engine.discovery import PositionRequest
from risk_engine.engine import RiskEngine
from risk_engine.graph import Edge, Node
from risk_engine.history import History
from risk_engine.signals import alert_levels, detect
from test_engine import add, build_market

NOW = 1_800_000_000.0
DAY = 86400
REQUEST = PositionRequest('market', '0xmarket', 1, 1e6)


def complete_graph():
    g = build_market()
    for node in g.nodes.values():
        if node.type == 'asset':
            node.attrs['price_usd'] = 1.0
    g.nodes['market'].attrs['supply_usd'] = 10e6
    return g


def add_feed(g, **attrs):
    add(g, [Node('feed', 'feed', 'PT USD', attrs={'answer': 1.0, 'age_s': 108000,
                                                 'stale': True, **attrs})], [])
    g.add_edge(Edge('oracle', 'feed', 'reads_feed', attrs={'role': 'baseFeedOne'}))


@pytest.fixture(autouse=True)
def no_routes_or_notifications(monkeypatch):
    monkeypatch.setattr('risk_engine.engine.exit_cost', lambda *a, **k: {'cost_pct': .1, 'instant_fraction': 1.0})
    monkeypatch.setattr('risk_engine.notify.deliver', lambda *a, **k: 0)


def test_baseline_risk_still_justifies_exit():
    row = dict(node_id='market', label='Market', mode='liquidity_drain', mode_label='Run',
               p=.4, base_p=.4, loss_given_failure=.5, expected_loss_pct=.2, alert='none', signals=[])
    m = RiskEngine().monitor(complete_graph(), REQUEST, 30,
                             {'rows': [row], 'total_expected_loss_pct': .2, 'p_any_loss_event': .4},
                             {'cost_pct': .01, 'instant_fraction': 1}, [], {}, {}, False, NOW)
    assert m['verdict'] == 'EXIT'
    assert m['avoidable_loss_pct'] == .2 and m['excess_loss_pct'] == 0
    assert row['break_even_p'] == pytest.approx(.02)


def test_yield_given_up_offsets_background_risk():
    row = dict(node_id='market', label='Market', mode='liquidity_drain', mode_label='Run',
               p=.01, base_p=.01, loss_given_failure=.5, expected_loss_pct=.005, alert='none', signals=[])
    screening = {'rows': [row], 'total_expected_loss_pct': .005, 'p_any_loss_event': .01}
    paid = RiskEngine().monitor(complete_graph(), REQUEST, 37, screening,
                                {'cost_pct': .001, 'instant_fraction': 1, 'apy': .0365, 'redeploy_days': 7}, [], {}, {}, False, NOW)
    # 3.65% APY over the 30 days not already charged in the exit cost = 0.3% credit.
    assert paid['yield_pct'] == pytest.approx(.003)
    assert paid['net_loss_pct'] == pytest.approx(.002) and paid['verdict'] == 'EXIT'
    unpaid = RiskEngine().monitor(complete_graph(), REQUEST, 37, screening,
                                  {'cost_pct': .0025, 'instant_fraction': 1, 'apy': .0365, 'redeploy_days': 7}, [], {}, {}, False, NOW)
    assert unpaid['verdict'] == 'STAY'  # 0.2% net < 0.25% exit, though 0.5% gross > 0.25%


def test_propagated_feed_warning_has_one_forecast_and_episode():
    g = complete_graph()
    add_feed(g)
    sig = detect(g, NOW)
    levels = alert_levels(sig)
    assert levels[('oracle', 'oracle_stale')]['level'] == 'alert'
    events = contagion.gather_events(g, sig, NOW, None)
    assert [(e['node_id'], e['mode']) for e in events] == [('feed', 'oracle_stale')]
    keys = [('feed', 'oracle_stale'), ('oracle', 'oracle_stale')]
    mults = contagion.multipliers(g, keys, events, NOW)
    assert all(m['multiplier'] == 1 for m in mults.values())
    rows = RiskEngine().screen(g, 30, np.random.default_rng(11), levels, mults)['rows']
    assert len([r for r in rows if r['mode'] == 'oracle_stale']) == 1
    h = History(':memory:')
    RiskEngine(history=h).evaluate(g, REQUEST, record=True, now=NOW)
    episodes = h.alert_episodes(NOW - 1, NOW + 1)
    assert len(episodes) == 1 and episodes[0]['node_id'] == 'feed'


@pytest.mark.parametrize('answer,age', [(None, None), (0, 100), (-1, 100), (float('nan'), 100)])
def test_invalid_feed_readings_produce_review_and_no_calibration(answer, age):
    g = complete_graph()
    add_feed(g, answer=answer, age_s=age, stale=False)
    h = History(':memory:')
    a = RiskEngine(history=h).evaluate(g, REQUEST, record=True, now=NOW)
    assert a['monitor']['verdict'] == 'REVIEW'
    assert a['monitor']['data_status'] == 'incomplete'
    assert a['monitor']['model_verdict'] in ('STAY', 'EXIT')
    assert a['monitor']['data_issues'][0]['node_id'] == 'feed'
    assert any(e['kind'] == 'data_quality' for e in h.events())
    assert h.forecasts() == []
    assert h.positions()[0]['verdict'] == 'REVIEW'


def test_missing_asset_price_is_not_calm_stay():
    g = complete_graph()
    g.nodes['usdc'].attrs['price_usd'] = None
    m = RiskEngine().evaluate(g, REQUEST, now=NOW)['monitor']
    assert m['verdict'] == 'REVIEW' and m['data_issues'][0]['fields'] == ['price_usd']


def test_combined_watch_alert_is_persisted_and_affects_other_components():
    g = complete_graph()
    g.nodes['market'].attrs.update(utilization=.98, bad_debt_usd=50000)
    signals = detect(g, NOW)
    assert all(s.level == 'watch' for s in signals)
    assert alert_levels(signals)[('market', 'liquidity_drain')]['level'] == 'alert'
    events = contagion.gather_events(g, signals, NOW, None)
    assert len(events) == 1
    assert contagion.multipliers(g, [('amm', 'liquidity_drain')], events, NOW)[('amm', 'liquidity_drain')]['multiplier'] > 1
    h = History(':memory:')
    RiskEngine(history=h).evaluate(g, REQUEST, record=True, now=NOW)
    assert len(h.alert_episodes(NOW - 1, NOW + 1)) == 1
    assert contagion.gather_events(g, [], NOW + DAY, h)[0]['node_id'] == 'market'


def test_watchlist_validates_each_entry_without_stopping(monkeypatch):
    engine = RiskEngine()
    calls = []
    def fake_analyze(req, horizon, **kw):
        calls.append(req.address)
        return {'position_id': 'id', 'monitor': dict(label='Valid', position_key='key', verdict='STAY',
                expected_loss_pct=.01, yield_pct=0, net_loss_pct=.01, exit_cost_pct=.1, max_alert='none', regime='calm', events=[])}
    monkeypatch.setattr(engine, 'analyze', fake_analyze)
    result = engine.run_watchlist([None, {'label': 'Missing address'}, {'address': 'a', 'size_usd': 'bad'},
                                   {'address': 'a', 'size_usd': float('nan')}, {'address': 'a', 'horizon_days': -1},
                                   {'address': 'valid'}])
    assert [r['ok'] for r in result] == [False] * 5 + [True]
    assert calls == ['valid']


def test_one_incident_does_not_satisfy_ten_incident_calibration_gate():
    fc = [dict(ts=NOW + d * DAY, position_key=position, node_id='usdx', mode='depeg',
               horizon_days=30, p=.1, alert='none') for d in range(60) for position in ('a', 'b')]
    report = calibration.report(fc, [dict(node_id='usdx', mode='depeg', ts=NOW + 40 * DAY)], NOW + 90 * DAY)
    assert report['daily_forecasts'] == 120
    assert report['n'] == 2 and report['events'] == 1
    assert report['positive_forecasts'] == 1
    assert 'too few' in report['note']
    assert report['reliability'][0]['n'] == 2
