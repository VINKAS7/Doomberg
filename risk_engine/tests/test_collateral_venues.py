"""Offline tests for the PT's collateral venues: lending markets whose liquidations are sold into its AMM."""
import time

import networkx as nx
import numpy as np
import pytest

from risk_engine import discovery
from risk_engine.discovery import Discovery, PositionRequest
from risk_engine.failures import applicable_modes
from risk_engine.graph import Node
from risk_engine.propagation import Simulator
from risk_engine.signals import data_issues, detect
from test_engine import add, build, build_market


def with_venue(g, collateral=50e6, borrow=40e6, tracks=False, expiry=None, venue="venue"):
    """The PT is also posted as collateral in ``venue``, priced by its own oracle."""
    if expiry:
        g.nodes["amm"].attrs["expiry"] = expiry
    return add(g, [
        Node(venue, "market", "PT-USDX/USDT", attrs={"role": "collateral_venue", "lltv": 0.915,
                                                     "collateral_usd": collateral, "borrow_usd": borrow}),
        Node(f"{venue}-oracle", "oracle", "venue oracle", risk_factors={"tracks_market": tracks}),
    ], [("pt", venue, "liquidated_in"), (venue, f"{venue}-oracle", "priced_by"), (venue, "proto", "runs_on")])


def held_loss(g, shock, mode, n=4000, **kw):
    return Simulator(g, seed=3).run(shock, mode, n=n, **kw).loss["position"]


def test_underpricing_venue_oracle_fire_sells_a_held_pt():
    loss = held_loss(with_venue(build()), "venue-oracle", "oracle_wrong_price")
    # Only the under-pricing draws (~35%) liquidate; the PT dips in those.
    assert 0.02 < loss.mean() and 0.2 < (loss > 1e-3).mean() < 0.5
    # Over-pricing (the replay direction) liquidates nobody.
    assert held_loss(with_venue(build()), "venue-oracle", "oracle_wrong_price", fixed_severity=0.3).max() == 0


def test_fire_sale_scales_with_collateral_and_is_capped_by_maturity_arbitrage():
    expiry = time.strftime("%Y-%m-%d", time.gmtime(time.time() + 61 * 86400))
    small = held_loss(with_venue(build(), collateral=2e6, borrow=1.5e6, expiry=expiry), "venue-oracle", "oracle_wrong_price")
    big = held_loss(with_venue(build(), collateral=200e6, borrow=170e6, expiry=expiry), "venue-oracle", "oracle_wrong_price")
    assert small.mean() < big.mean()
    tau = 61 / 365
    assert big.max() <= 1 - 3.0 ** (-tau) + 1e-9  # never below the price yielding 200% APY to maturity
    expired = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 86400))
    assert held_loss(with_venue(build(), expiry=expired), "venue-oracle", "oracle_wrong_price").max() == 0


def test_stale_or_frozen_venue_cannot_liquidate():
    g = with_venue(build(), tracks=True)
    sim = Simulator(g, seed=3)
    drop, hours = np.full(1000, 0.1), np.full(1000, 5.0)
    assert sim._fire_sale(sim.outs["pt"], drop, {}, {}, {}, 1000).mean() > 0.01
    assert sim._fire_sale(sim.outs["pt"], drop, {}, {"venue-oracle": hours}, {}, 1000).max() == 0
    assert sim._fire_sale(sim.outs["pt"], drop, {}, {}, {"venue": hours}, 1000).max() == 0
    # End to end: a tracking venue sees the real drop and amplifies it.
    assert held_loss(g, "usdx", "depeg", fixed_severity=0.1).mean() > 0.1 + 0.01


def test_blind_venue_does_not_amplify_an_underlying_depeg():
    loss = held_loss(with_venue(build(), tracks=False), "usdx", "depeg", fixed_severity=0.1)
    assert loss.mean() == pytest.approx(0.1, abs=0.01)


def test_sibling_venue_on_a_market_position_is_acyclic_and_has_no_modes():
    g = with_venue(build_market(tracks=False))
    assert nx.is_directed_acyclic_graph(nx.DiGraph(g.g))
    assert applicable_modes("market", g.nodes["venue"].attrs) == []
    assert applicable_modes("market", g.nodes["market"].attrs)  # the position's own market keeps its modes
    # Sibling liquidations depress the PT, which the lender's own blind-oracle market cannot see.
    lender = held_loss(g, "venue-oracle", "oracle_wrong_price")
    assert lender.mean() > 0


def test_venue_signals_and_data_checks():
    g = with_venue(build())
    g.nodes["venue"].attrs.update(utilization=1.0, daily_price_variation=-0.06)
    sig = detect(g, time.time())
    assert {(s.node_id, s.rule) for s in sig} == {("pt", "collateral_drop")}  # no liquidity alert on the venue
    g.nodes["venue"].attrs["borrow_usd"] = None
    assert [i["node_id"] for i in data_issues(g) if i["node_id"] == "venue"] == ["venue"]


class FakeRpc:
    def block_number(self):
        return 1

    def read_feed(self, addr):
        return {"description": "PT-USDX / USD exchange rate", "answer": 0.98, "age_s": 60}


def market(mid, collateral, borrow=None, oracle="0xoracle"):
    return {"marketId": mid, "lltv": str(int(0.915e18)), "creationTimestamp": int(time.time() - 400 * 86400),
            "collateralAsset": {"address": "0xpt", "symbol": "PT-USDX-1JAN2027", "decimals": 18},
            "loanAsset": {"address": "0xusdc", "symbol": "USDC", "decimals": 6},
            "oracle": {"address": oracle, "type": "ChainlinkOracleV2", "data": {"baseFeedOne": {"address": "0x" + "1" * 40}}},
            "state": {"collateralAssetsUsd": collateral, "borrowAssetsUsd": borrow if borrow is not None else collateral * 0.8,
                      "supplyAssetsUsd": collateral, "liquidityAssetsUsd": collateral * 0.1, "utilization": 0.9}}


def test_discovery_adds_venues_but_not_the_own_market_or_dust(monkeypatch):
    found = [market("0xown", 90e6), market("0xdust", 1e5)] + [market(f"0xv{i}", (i + 1) * 1e6) for i in range(7)]
    monkeypatch.setattr(discovery.morpho, "markets_by_collateral", lambda *a: found)
    monkeypatch.setattr(discovery, "protocol_tvl", lambda *a: None)
    d = Discovery(PositionRequest("market", "0xOWN", 1))
    monkeypatch.setattr(d, "rpc", lambda chain_id: FakeRpc())
    d.g.add_node(Node("pt", "asset", "PT-USDX-1JAN2027", 1, "0xpt", attrs={"kind": "pt"}))
    d.g.add_node(Node("amm", "amm", "Pendle market USDX", 1, "0xamm", attrs={"liquidity_usd": 4e6}))
    # The own market's oracle was classified first: a venue sharing it must not overwrite its risk factors.
    d.g.add_node(Node("oracle:1:0xoracle", "oracle", "Oracle", 1, "0xoracle",
                      risk_factors={"collateral_tvl_usd": 90e6, "age_days": 30}))
    d.add_collateral_venues("pt", "0xpt", 1, "amm")
    venues = [e.target for e in d.g.out_edges("pt") if e.type == "liquidated_in"]
    assert venues == [f"market:1:0xv{i}" for i in (6, 5, 4, 3, 2)]  # largest 5, own and dust skipped
    assert not any(e.type == "collateral_in" for e in d.g.edges.values())
    assert d.g.nodes["oracle:1:0xoracle"].risk_factors == {"collateral_tvl_usd": 90e6, "age_days": 30}
    assert d.g.nodes["pt"].attrs["collateral_elsewhere_usd"] == pytest.approx(28e6)
    assert d.g.nodes["pt"].attrs["collateral_to_amm"] == pytest.approx(7.0)
    assert any("not modelled" in w for w in d.warnings)
