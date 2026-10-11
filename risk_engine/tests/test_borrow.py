"""Borrow positions on synthetic graphs: a PT loop and cbBTC collateral deployed into a PT. No network needed."""

import numpy as np
import pytest

from risk_engine import exit_cost as exit_mod
from risk_engine.discovery import PositionRequest
from risk_engine.engine import position_key
from risk_engine.failures import applicable_modes
from risk_engine.graph import Node
from risk_engine.leverage import health, hit_probability, liquidation_incentive
from risk_engine.propagation import Simulator

from test_engine import add, pt_subgraph


def borrow_attrs(structure: str, ltv: float, lltv: float, equity: float = 1e6) -> dict:
    return {"kind": "borrow", "structure": structure, "ltv": ltv, "lltv": lltv, "equity_usd": equity, "size_usd": equity,
            "lif": liquidation_incentive(lltv)}


def build_loop(ltv: float = 0.8, lltv: float = 0.915, tracks: bool = False):
    """Post the PT on Morpho, borrow USDC against it (the USDC bought more PT)."""
    g = pt_subgraph()
    g.nodes["position"].attrs.update(borrow_attrs("loop", ltv, lltv))
    return add(g, [
        Node("market", "market", "PT-USDX/USDC", attrs={"lltv": lltv, "utilization": 0.9, "borrow_apy": 0.06, "role": "borrow_market"}),
        Node("usdc", "asset", "USDC", attrs={"kind": "fiat_stable"}),
        Node("oracle", "oracle", "oracle", attrs={"feed_descriptions": ["USDX / USD"]}, risk_factors={"tracks_market": tracks}),
        Node("morpho", "protocol", "Morpho Blue", risk_factors={"immutable_core": True}),
    ], [
        ("position", "market", "borrows_from"),
        ("market", "pt", "collateral_in"), ("market", "oracle", "priced_by"), ("market", "usdc", "lends"),
        ("market", "morpho", "runs_on"), ("morpho", "chain", "settles_on"),
    ])


def build_deploy(ltv: float = 0.4, lltv: float = 0.86):
    """Post cbBTC, borrow USDC, put the USDC in the PT."""
    g = pt_subgraph()
    g.nodes["position"].attrs.update(borrow_attrs("deploy", ltv, lltv))
    return add(g, [
        Node("market", "market", "cbBTC/USDC", attrs={"lltv": lltv, "utilization": 0.9, "borrow_apy": 0.05, "role": "borrow_market"}),
        Node("cbbtc", "asset", "cbBTC", attrs={"kind": "wrapped_btc", "price_usd": 100_000}),
        Node("usdc", "asset", "USDC", attrs={"kind": "fiat_stable"}),
        Node("oracle", "oracle", "oracle", attrs={"feed_descriptions": ["BTC / USD"]}, risk_factors={"tracks_market": False}),
        Node("morpho", "protocol", "Morpho Blue", risk_factors={"immutable_core": True}),
    ], [
        ("position", "market", "borrows_from"), ("position", "pt", "deploys_into"),
        ("market", "cbbtc", "collateral_in"), ("market", "oracle", "priced_by"), ("market", "usdc", "lends"),
        ("market", "morpho", "runs_on"), ("morpho", "chain", "settles_on"),
    ])


def loss(g, node, mode, sev, n=3000, seed=3):
    return Simulator(g, seed=seed).run(node, mode, n=n, fixed_severity=sev).loss["position"]


def test_loop_levers_a_depeg_the_oracle_does_not_see():
    # 5x leverage: a 5% PT drop costs ~25% of the equity, and a blind oracle never liquidates.
    lo = loss(build_loop(tracks=False), "usdx", "depeg", 0.05)
    assert abs(lo.mean() - 0.25) < 0.04


def test_loop_liquidation_adds_a_penalty_when_the_oracle_sees_the_drop():
    # Distance to liquidation at 80% / 91.5% is 12.6%: a 15% drop is seen and liquidated.
    blind = loss(build_loop(tracks=False), "usdx", "depeg", 0.15)
    seen = loss(build_loop(tracks=True), "usdx", "depeg", 0.15)
    assert seen.mean() > blind.mean() + 0.01


def test_under_pricing_oracle_wrongfully_liquidates_a_sound_borrower():
    g = build_loop(tracks=True)
    # Replays use the observed direction (over-pricing); flip it to under-pricing via the sign of the severity path.
    sim = Simulator(g, seed=5)
    res = sim.run("oracle", "oracle_wrong_price", n=4000)
    under = res.oerr["oracle"] < -0.2
    over = res.oerr["oracle"] > 0
    assert res.loss["position"][under].mean() > 0.01
    assert res.loss["position"][over].mean() < 1e-6


def test_deploy_leg_loss_is_scaled_by_ltv():
    # 40% of the equity sits in the PT: a 20% PT loss is 8% of the equity.
    lo = loss(build_deploy(), "usdx", "depeg", 0.20)
    assert abs(lo.mean() - 0.08) < 0.02


def test_collateral_depeg_hits_the_equity_in_full():
    lo = loss(build_deploy(), "cbbtc", "depeg", 0.10)
    assert lo.mean() >= 0.095


def test_cheaper_loan_asset_shrinks_the_debt():
    # USDC depegs: the PT (backed by USDX, not USDC) is untouched, the debt is smaller -> no loss.
    lo = loss(build_deploy(), "usdc", "depeg", 0.10)
    assert lo.mean() < 1e-6


def test_liquidation_incentive_matches_morpho():
    assert abs(liquidation_incentive(0.86) - 1 / (0.3 * 0.86 + 0.7)) < 1e-12
    assert abs(liquidation_incentive(0.86) - 1.0438) < 1e-3
    assert abs(liquidation_incentive(0.915) - 1.026) < 1e-3
    assert liquidation_incentive(0.0) == 1.15


def test_liquidation_probability_rises_with_leverage_and_vanishes_without_a_price_feed():
    assert hit_probability(0.5, 0.55, 30) < hit_probability(0.2, 0.55, 30) < hit_probability(0.05, 0.55, 30)
    assert hit_probability(0.2, 0.0, 30) == 0.0
    lo, hi = health(build_deploy(ltv=0.3), 30), health(build_deploy(ltv=0.7), 30)
    assert lo["p_liquidation"] < hi["p_liquidation"]
    assert abs(lo["health_factor"] - 0.86 / 0.3) < 1e-9
    g = build_deploy()
    g.nodes["oracle"].attrs.update({"feed_descriptions": [], "oracle_type": "ChainlinkOracleV2"})
    assert health(g, 30)["p_liquidation"] == 0.0


def test_unresolved_oracle_is_assumed_to_follow_the_market_for_a_borrower():
    # The lender model assumes no liquidations here; for a borrower the conservative side is the opposite.
    g = build_loop(tracks=False)
    g.nodes["oracle"].attrs.update({"feed_descriptions": [], "oracle_type": "Unknown"})
    assert health(g, 30)["oracle_sees_depeg"] and health(g, 30)["seen_vol"] > 0
    blind = loss(build_loop(tracks=False), "usdx", "depeg", 0.15)
    assert loss(g, "usdx", "depeg", 0.15).mean() > blind.mean() + 0.01


def test_exit_cost_never_exceeds_the_equity(monkeypatch):
    monkeypatch.setattr(exit_mod, "quote_asset_sale", lambda g, a, s: {"impact": 0.9, "source": "test", "detail": "thin"})
    ex = exit_mod.exit_cost(build_loop(ltv=0.8), 1e6, np.random.default_rng(0), stressed=False)
    assert ex["cost_pct"] == 1.0 and any("Deleverage" in a for a in ex["assumptions"])


def test_health_carry_and_balance_sheet():
    h = health(build_loop(ltv=0.8), 30)
    assert abs(h["collateral_usd"] - 5e6) < 1 and abs(h["debt_usd"] - 4e6) < 1 and h["leverage"] == pytest.approx(5.0)
    d = health(build_deploy(ltv=0.4), 30)
    assert d["collateral_usd"] == 1e6 and d["deployed_usd"] == pytest.approx(4e5)


def test_loop_exit_sells_the_whole_levered_collateral(monkeypatch):
    sold = []

    def quote(g, asset_id, size):
        sold.append(size)
        return {"impact": 0.01, "source": "test", "detail": "test quote"}

    monkeypatch.setattr(exit_mod, "quote_asset_sale", quote)
    ex = exit_mod.exit_cost(build_loop(ltv=0.8), 1e6, np.random.default_rng(0), stressed=False)
    assert sold == [pytest.approx(5e6)]
    assert abs(ex["cost_pct"] - (0.05 + 80 / 1e6)) < 1e-6  # 1% impact on 5x the equity, plus gas
    assert ex["instant_fraction"] == 1.0


def test_borrow_market_has_no_modes_and_key_tracks_leverage():
    assert applicable_modes("market", {"role": "borrow_market"}) == []
    a = PositionRequest("borrow", "0xabc", 1, 1e6, 1e7, None, 0.4, "0xpt")
    b = PositionRequest("borrow", "0xabc", 1, 1e6, 1e7, None, 0.6, "0xpt")
    assert position_key(a, 30) != position_key(b, 30)
