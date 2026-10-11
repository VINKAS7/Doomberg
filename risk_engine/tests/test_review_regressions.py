"""Offline regressions for exit proceeds and oracle-ratio review findings."""
import numpy as np
import pytest

from risk_engine.graph import Edge, Node
from risk_engine.propagation import Simulator
from risk_engine.sources import pricing, pendle
from risk_engine.exit_cost import exit_cost, quote_asset_sale, GAS_USD
from risk_engine.decision import decide
from risk_engine.explain import template
from test_engine import build, build_market


@pytest.mark.parametrize('output_price, expected', [(1.0, 1000), (.8, 800), (1.1, 1100)])
def test_swap_values_raw_proceeds_at_independent_output_price(monkeypatch, output_price, expected):
    monkeypatch.setattr(pricing, 'get_json', lambda *a, **k: {'data': {'routeSummary': {
        'amountInUsd': '1000', 'amountOutUsd': '9999', 'amountOut': '1000000000', 'gasUsd': '10'}}})
    monkeypatch.setattr(pricing, 'price_usd', lambda *a: {'price': output_price, 'decimals': 6})
    q = pricing.swap_quote('0xinput', 18, 1000, 1, 1)
    assert q['amount_out_usd'] == pytest.approx(expected)
    assert q['price_impact'] == pytest.approx(max(0, 1 - (expected - 10) / 1000))


def test_unpriced_proceeds_are_not_assumed_to_have_a_dollar_peg(monkeypatch):
    monkeypatch.setattr(pricing, 'get_json', lambda *a, **k: {'data': {'routeSummary': {'amountOut': '1000000000'}}})
    monkeypatch.setattr(pricing, 'price_usd', lambda *a: None)
    assert pricing.swap_quote('0xinput', 18, 1000, 1, 1) is None


def test_output_valuation_respects_decimals(monkeypatch):
    monkeypatch.setattr(pricing, 'price_usd', lambda *a: {'price': 2, 'decimals': 18})
    assert pricing.output_value_usd('0xoutput', 3 * 10**18, 1) == 6


def pt_with_route():
    g = build()
    g.nodes['pt'].address = '0xpt'
    g.nodes['pt'].attrs.update(price_usd=1, decimals=18)
    g.nodes['amm'].address = '0xmarket'
    g.nodes['amm'].attrs['underlying'] = '0xstill-exposed'
    return g


def test_pendle_exit_quotes_final_cash_proceeds_not_the_underlying(monkeypatch):
    g = pt_with_route()
    monkeypatch.setattr('risk_engine.exit_cost.swap_quote', lambda *a, **kw: None)
    calls = []
    def quote(market, pt, token_out, amount_raw, chain):
        calls.append(token_out)
        return {'amount_out': 900 * 10**6, 'price_impact': .001, 'fee_usd': 1}
    monkeypatch.setattr(pendle, 'swap_quote', quote)
    monkeypatch.setattr(pricing, 'price_usd', lambda *a: {'price': .8, 'decimals': 6})
    q = quote_asset_sale(g, 'pt', 1000)
    assert calls == [pricing.USDC[1]]
    # $720 actually received, plus disclosed gas. Fee is already in amountOut.
    assert q['impact'] == pytest.approx(1 - (720 - GAS_USD) / 1000)


def test_held_pt_exit_takes_the_cheapest_executable_route(monkeypatch):
    g = pt_with_route()
    # The aggregator routes through thin pools; the PT's own Pendle market is deep.
    monkeypatch.setattr('risk_engine.exit_cost.swap_quote', lambda *a, **kw: {
        'price_impact': .91, 'amount_out_usd': 90, 'gas_usd': 0})
    monkeypatch.setattr(pendle, 'swap_quote', lambda *a: {'amount_out': 999 * 10**6})
    monkeypatch.setattr(pricing, 'price_usd', lambda *a: {'price': 1, 'decimals': 6})
    q = quote_asset_sale(g, 'pt', 1000)
    assert q['source'] == 'pendle-api' and q['impact'] == pytest.approx(1 - (999 - GAS_USD) / 1000)
    assert 'kyberswap 91.00%' in q['detail']


def test_wrapper_depeg_does_not_recount_its_underlying_class():
    from risk_engine.failures import hazard_prior, severity_prior
    plain = {'kind': 'yield_stable'}
    wrapper = {'kind': 'yield_stable', 'wraps': 'USDe'}
    k0, T0, _ = hazard_prior('depeg', 'asset', plain, {})
    k1, T1, why = hazard_prior('depeg', 'asset', wrapper, {})
    assert k1 / T1 < k0 / T0 and 'USDe depeg is modelled on its own node' in why[0]
    assert severity_prior('depeg', 'asset', wrapper)[0] < severity_prior('depeg', 'asset', plain)[0]
    assert hazard_prior('depeg', 'asset', {'kind': 'pt', 'wraps': 'USDe'}, {})[:2] == hazard_prior('depeg', 'asset', {'kind': 'pt'}, {})[:2]


def test_unconfirmed_cash_route_does_not_credit_full_loss_avoidance(monkeypatch):
    g = pt_with_route()
    monkeypatch.setattr('risk_engine.exit_cost.swap_quote', lambda *a, **kw: None)
    monkeypatch.setattr(pendle, 'swap_quote', lambda *a: None)
    ex = exit_cost(g, 1000000, np.random.default_rng(1), stressed=False, n=50)
    assert ex['instant_fraction'] == ex['instant_fraction_calm'] == 0
    d = decide(1000000, 10000000, np.ones(50), np.ones(50), ex['cost_pct'], ex['samples'], ex['instant_fraction'])
    assert d['verdict'] == 'STAY' and d['avoidable_loss_usd'] == 0
    assert 'No immediate complete exit' in d['reasons'][0]


def test_pendle_sdk_enables_cash_conversion_and_preserves_output_amount(monkeypatch):
    requests = []
    def response(*a, **kw):
        requests.append(kw['params'])
        return {'data': {'amountOut': '950000000', 'priceImpact': -.01, 'fee': {'usd': 1}}}
    monkeypatch.setattr(pendle, 'get_json', response)
    q = pendle.swap_quote('0xmarket', '0xpt', pricing.USDC[1], 10**18, 1)
    assert requests[0]['enableAggregator'] == 'true'
    assert requests[0]['tokenOut'] == pricing.USDC[1]
    assert q['amount_out'] == 950000000


def test_quote_underreport_crosses_overborrowing_threshold(monkeypatch):
    g = build_market(util=.5)
    g.add_node(Node('quote', 'feed', 'USDC/USD'))
    g.add_edge(Edge('oracle', 'quote', 'reads_feed', attrs={'role': 'quoteFeedOne'}))
    s = Simulator(g, seed=2)
    monkeypatch.setattr(s, 'sample_severity', lambda *a, **kw: np.full(2000, .15))
    result = s.run('quote', 'oracle_wrong_price', n=2000)
    under = result.oerr['quote'] < 0
    assert under.any()
    np.testing.assert_allclose(result.oerr['oracle'][under], 1 / .85 - 1)
    assert result.loss['market'][under].mean() > 0
    np.testing.assert_allclose(result.oerr['oracle'][~under], 1 / 1.15 - 1)


def test_zero_quote_feed_price_produces_finite_bounded_losses(monkeypatch):
    g = build_market(util=.5)
    g.add_node(Node('quote', 'feed', 'USDC/USD'))
    g.add_edge(Edge('oracle', 'quote', 'reads_feed', attrs={'role': 'quoteFeedOne'}))
    s = Simulator(g, seed=2)
    monkeypatch.setattr(s, 'sample_severity', lambda *a, **kw: np.ones(100))
    result = s.run('quote', 'oracle_wrong_price', n=100)
    assert np.isfinite(result.oerr['oracle']).all()
    assert np.isfinite(result.loss['position']).all()
    assert (result.loss['position'] >= 0).all() and (result.loss['position'] <= 1).all()


def test_partial_exit_narrative_compares_avoidable_loss():
    d = decide(1e6, 1e7, np.full(100, .3), np.full(100, .5), .11, None, .5)
    text = template({'verdict': d['verdict'], 'probability': d['probability'], 'loss_if_fails': d['loss_given_failure'],
                     'main_paths': [], 'shock': {'node_label': 'Oracle'}, 'expected_loss_usd': d['expected_loss_usd'],
                     'avoidable_loss_usd': d['avoidable_loss_usd'], 'instant_exit_fraction': .5,
                     'exit_cost_usd': d['exit_cost_usd'], 'exit_cost_pct': .11,
                     'probability_range_90': [.3, .3], 'probability_reasons': [], 'exit_steps': [],
                     'break_even_probability': d['break_even_probability']})
    assert 'STAY: exiting can avoid $75,000 of the $150,000 expected loss' in text
    assert 'versus $110,000' in text


def test_cash_destination_excludes_transitive_backing(monkeypatch):
    g = pt_with_route()
    g.nodes['usdx'].address = pricing.USDC[1]
    monkeypatch.setattr('risk_engine.exit_cost.swap_quote', lambda *a, **kw: None)
    destinations = []
    def quote(*args):
        destinations.append(args[2])
        return {'amount_out': 1000 * 10**6}
    monkeypatch.setattr(pendle, 'swap_quote', quote)
    monkeypatch.setattr(pricing, 'price_usd', lambda *a: {'price': 1, 'decimals': 6})
    quote_asset_sale(g, 'pt', 1000)
    assert destinations == [pricing.USDT[1]]
