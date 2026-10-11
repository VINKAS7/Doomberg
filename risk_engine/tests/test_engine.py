"""Offline tests on a synthetic PT graph: no network needed."""

import numpy as np

from risk_engine.decision import decide
from risk_engine.failures import applicable_modes
from risk_engine.graph import DependencyGraph, Edge, Node
from risk_engine.probability import failure_probability
from risk_engine.propagation import Simulator


def add(g: DependencyGraph, nodes: list[Node], edges: list[tuple[str, str, str]]) -> DependencyGraph:
    for n in nodes:
        g.add_node(n)
    for u, v, t in edges:
        g.add_edge(Edge(u, v, t))
    return g


def pt_subgraph() -> DependencyGraph:
    g = DependencyGraph()
    g.root = "position"
    return add(g, [
        Node("position", "position", "pos"),
        Node("pt", "asset", "PT-USDX", attrs={"kind": "pt"}),
        Node("usdx", "asset", "USDX", attrs={"kind": "synthetic_stable"}),
        Node("issuer", "issuer", "USDX Labs", risk_factors={"issuer_tier": "unknown"}),
        Node("amm", "amm", "Pendle market PT-USDX", attrs={"liquidity_usd": 5e6}),
        Node("proto", "protocol", "Pendle V2", risk_factors={"audited_major": True}),
        Node("chain", "chain", "Ethereum", attrs={"chain_class": "ethereum"}, risk_factors={"chain_class": "ethereum"}),
    ], [
        ("pt", "usdx", "backed_by"), ("usdx", "issuer", "issued_by"),
        ("pt", "amm", "traded_on"), ("amm", "proto", "runs_on"), ("proto", "chain", "settles_on"),
    ])


def build() -> DependencyGraph:
    """Hold the PT."""
    return add(pt_subgraph(), [], [("position", "pt", "holds")])


def build_market(tracks: bool = True, util: float = 0.9, lltv: float = 0.86) -> DependencyGraph:
    """Supply USDC to a Morpho market that takes the PT as collateral."""
    return add(pt_subgraph(), [
        Node("market", "market", "PT-USDX/USDC", attrs={"lltv": lltv, "utilization": util, "liquidity_usd": 2e6}),
        Node("usdc", "asset", "USDC", attrs={"kind": "fiat_stable"}),
        Node("oracle", "oracle", "oracle", risk_factors={"tracks_market": tracks}),
        Node("morpho", "protocol", "Morpho Blue", risk_factors={"immutable_core": True}),
    ], [
        ("position", "market", "deposits_in"), ("position", "usdc", "denominated_in"),
        ("market", "pt", "collateral_in"), ("market", "oracle", "priced_by"), ("market", "usdc", "lends"),
        ("market", "morpho", "runs_on"), ("morpho", "chain", "settles_on"),
    ])


def test_brief_examples():
    """README section 9: 30% x 50% = $150k > $110k -> EXIT; 10% x 40% = $40k < $110k -> STAY."""
    size = 1_000_000
    d1 = decide(size, 10 * size, np.full(1000, 0.30), np.full(1000, 0.50), 0.11, None, 1.0)
    assert d1["verdict"] == "EXIT" and round(d1["expected_loss_usd"]) == 150_000
    d2 = decide(size, 10 * size, np.full(1000, 0.10), np.full(1000, 0.40), 0.11, None, 1.0)
    assert d2["verdict"] == "STAY" and round(d2["expected_loss_usd"]) == 40_000
    assert abs(d1["break_even_probability"] - 0.22) < 1e-9


def test_break_even_table_matches_readme():
    d = decide(1e6, 1e7, np.full(10, 0.1), np.full(10, 0.5), 0.11, None, 1.0)
    table = {row["loss_if_fails"]: row["exit_above"] for row in d["break_even_table"]}
    assert abs(table[1.0] - 0.11) < 1e-9 and abs(table[0.5] - 0.22) < 1e-9 and abs(table[0.25] - 0.44) < 1e-9
    assert table[0.10] is None


def test_underlying_depeg_passes_straight_through_to_pt():
    sim = Simulator(build(), seed=4).run("usdx", "depeg", n=3000, fixed_severity=0.12)
    assert abs(sim.loss["position"].mean() - 0.12) < 0.02


def test_issuer_default_reaches_position_through_underlying():
    sim = Simulator(build(), seed=5).run("issuer", "issuer_default", n=3000, fixed_severity=0.4)
    assert abs(sim.loss["position"].mean() - 0.4) < 0.03


def test_amm_liquidity_drain_hits_pt():
    sim = Simulator(build(), seed=6).run("amm", "liquidity_drain", n=3000, fixed_severity=0.2)
    assert abs(sim.loss["pt"].mean() - 0.2) < 0.02 and sim.loss["usdx"].mean() == 0


def test_chain_halt_causes_small_price_drift_only():
    sim = Simulator(build(), seed=7).run("chain", "chain_halt", n=4000, fixed_severity=24)
    assert 0 < sim.loss["position"].mean() < 0.01


def test_pt_cannot_be_unbacked_minted():
    modes = {m.id for m in applicable_modes("asset", {"kind": "pt"})}
    assert "depeg" in modes and "unbacked_mint" not in modes


def test_small_pt_depeg_inside_lltv_buffer_causes_no_lender_loss():
    sim = Simulator(build_market(tracks=True), seed=1).run("usdx", "depeg", n=2000, fixed_severity=0.03)
    assert sim.loss["market"].mean() < 0.005


def test_blind_oracle_turns_pt_depeg_into_bad_debt():
    seen = Simulator(build_market(tracks=True), seed=1).run("usdx", "depeg", n=4000, fixed_severity=0.4).loss["position"].mean()
    blind = Simulator(build_market(tracks=False), seed=1).run("usdx", "depeg", n=4000, fixed_severity=0.4).loss["position"].mean()
    assert blind > seen > 0


def test_oracle_overpricing_drains_idle_liquidity_only():
    # Over-borrowing pays only once LLTV x (1 + error) > 1, i.e. error > 16% at LLTV 86%.
    small = Simulator(build_market(util=0.5), seed=2).run("oracle", "oracle_wrong_price", n=4000, fixed_severity=0.10).loss["market"]
    big = Simulator(build_market(util=0.5), seed=2).run("oracle", "oracle_wrong_price", n=4000, fixed_severity=1.0).loss["market"]
    assert small.mean() < 0.01
    assert 0.1 < big.mean() and big.max() <= 0.5 + 1e-9  # never more than the idle 50% of supply


def test_loan_asset_depeg_passes_straight_through_to_lender():
    sim = Simulator(build_market(), seed=4).run("usdc", "depeg", n=3000, fixed_severity=0.12)
    assert abs(sim.loss["position"].mean() - 0.12) < 0.02


def test_alert_raises_probability():
    node = Node("coll", "asset", "COLL", attrs={"kind": "synthetic_stable"})
    rng = np.random.default_rng(0)
    base = failure_probability(node, "depeg", 30, rng, alert="none")["p"]
    alert = failure_probability(node, "depeg", 30, rng, alert="alert")["p"]
    confirmed = failure_probability(node, "depeg", 30, rng, alert="confirmed")["p"]
    assert base < alert < confirmed


def test_market_liquidity_drain_hits_lenders_in_proportion_to_utilization():
    busy = Simulator(build_market(util=0.9), seed=3).run("market", "liquidity_drain", n=3000, fixed_severity=0.2)
    quiet = Simulator(build_market(util=0.3), seed=3).run("market", "liquidity_drain", n=3000, fixed_severity=0.2)
    assert abs(busy.loss["position"].mean() - 0.18) < 0.02
    assert 0 < quiet.loss["position"].mean() < busy.loss["position"].mean()


def test_quote_feed_overreport_underprices_collateral():
    g = build_market(util=0.5)
    add(g, [Node("qfeed", "feed", "USDC / USD")], [])
    g.add_edge(Edge("oracle", "qfeed", "reads_feed", attrs={"role": "quoteFeedOne"}))
    sim = Simulator(g, seed=2).run("qfeed", "oracle_wrong_price", n=2000, fixed_severity=1.0)
    assert (sim.oerr["oracle"] < 0).all() and sim.loss["market"].mean() < 1e-6


def test_exit_benefit_counts_only_withdrawable_share():
    # 30% x 50% = 15% expected loss vs 11% exit cost: EXIT if fully liquid, STAY if only half can get out.
    full = decide(1e6, 1e7, np.full(100, 0.30), np.full(100, 0.50), 0.11, None, 1.0)
    half = decide(1e6, 1e7, np.full(100, 0.30), np.full(100, 0.50), 0.11, None, 0.5)
    assert full["verdict"] == "EXIT" and half["verdict"] == "STAY"
    assert abs(half["break_even_probability"] - 0.44) < 1e-9 and round(half["avoidable_loss_usd"]) == 75_000


def test_zero_age_counts_as_young_but_unknown_age_does_not():
    from risk_engine.failures import hazard_prior
    _, rate_new, _ = hazard_prior("exploit", "protocol", {}, {"age_days": 0})
    _, rate_unknown, _ = hazard_prior("exploit", "protocol", {}, {"age_days": None})
    assert rate_new < rate_unknown  # lower Gamma rate parameter = higher hazard
