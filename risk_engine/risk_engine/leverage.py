"""Borrower positions: collateral posted on Morpho, a loan taken against it, and the loan deployed into a PT.

Two structures share one model, everything as a fraction of the equity E (the client's capital):
  - PT loop: the PT is the collateral and the borrowed loan asset was used to buy more of it.
    Collateral C = E / (1 - ltv), debt D = E * ltv / (1 - ltv), nothing deployed elsewhere.
  - Deploy: other collateral (e.g. cbBTC) backs a loan that is put to work in a PT.
    Collateral C = E, debt D = E * ltv, deployed V = D.

Equity after a shock is C(1 - dc)(1 - dp) + V(1 - dv) - D(1 - dl) - liquidation penalty. The borrower can
lose at most the equity; anything beyond is the lenders' bad debt (``Simulator._market``).
"""

from __future__ import annotations

import math

import numpy as np

from .failures import asset_vol
from .graph import DependencyGraph

# Morpho Blue: LIF = min(M, 1 / (beta * LLTV + (1 - beta))), M = 1.15, beta = 0.3.
LIF_MAX, LIF_BETA = 1.15, 0.3
# Share of the debt a liquidator repays once a position is liquidatable (Morpho Blue has no close factor).
REPAID = (0.5, 1.0)


def liquidation_incentive(lltv: float) -> float:
    return min(LIF_MAX, 1.0 / (LIF_BETA * lltv + (1.0 - LIF_BETA)))


def balance_sheet(structure: str, ltv: float) -> tuple[float, float, float]:
    """(collateral, debt, deployed) per unit of equity."""
    if structure == "loop":
        return 1.0 / (1.0 - ltv), ltv / (1.0 - ltv), 0.0
    return 1.0, ltv, ltv


def borrower_loss(structure: str, ltv: float, lltv: float, rng: np.random.Generator, n: int, *,
                  dc, dv, dl, dp, oerr, tracks: bool, blind_h, vol: float) -> np.ndarray:
    """Loss of the borrower's equity per draw.

    dc collateral loss, dv deployed PT loss, dl loan-asset loss (the debt shrinks), dp Morpho Blue loss
    (collateral stolen), oerr oracle error (+ over-prices), blind_h hours nobody can act (stale oracle or
    halted chain). The oracle sees the collateral at ``s = (1 - dc * tracks) * (1 + oerr)`` of its old price.
    """
    z = np.zeros(n)
    C, D, V = balance_sheet(structure, ltv)
    dc = np.clip(dc + z, 0.0, 0.999)
    # While nobody can add collateral or repay, the price drifts; it counts toward liquidation only:
    # a market move is the exposure the client chose, not a dependency loss.
    blind_h = blind_h + z
    drift = np.where(blind_h > 0, np.clip(np.abs(rng.normal(0, 1, n)) * vol * np.sqrt(blind_h / 8760.0), 0, 0.99), 0.0)
    trig = 1.0 - (1.0 - dc) * (1.0 - drift)
    s = np.maximum((1.0 - (trig if tracks else drift)) * (1.0 + oerr + z), 1e-9)
    liquidated = D / (C * s) > lltv
    q = np.where(liquidated, rng.uniform(*REPAID, n), 0.0)
    repaid = q * D
    lif = liquidation_incentive(lltv)
    # The liquidator takes collateral worth LIF x the repaid debt at the oracle price; at the true price it can be worth more.
    seized = np.minimum(repaid * lif * (1.0 - dc) / s, C * (1.0 - dc))
    penalty = np.maximum(seized - repaid, 0.0)
    equity = C * (1.0 - dc) * (1.0 - (dp + z)) + V * (1.0 - (dv + z)) - D * (1.0 - (dl + z)) - penalty
    return np.clip(1.0 - equity, 0.0, 1.0)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def hit_probability(dist: float, vol: float, horizon_days: float) -> float:
    """P(a driftless lognormal price falls by ``dist`` at some point within the horizon), reflection principle."""
    if dist <= 0:
        return 1.0
    if vol <= 0 or horizon_days <= 0 or dist >= 1:
        return 0.0
    return min(1.0, 2.0 * _norm_cdf(math.log(1.0 - dist) / (vol * math.sqrt(horizon_days / 365.0))))


def parts(g: DependencyGraph) -> dict:
    """The borrow market and the nodes the borrower depends on through it."""
    pos = g.nodes[g.root]
    outs = g.out_edges(g.root)
    market = next((e.target for e in outs if e.type == "borrows_from"), None)
    mouts = g.out_edges(market) if market else []
    pick = lambda es, t: next((e.target for e in es if e.type == t), None)  # noqa: E731
    return {"position": pos, "market": market, "collateral": pick(mouts, "collateral_in"), "oracle": pick(mouts, "priced_by"),
            "loan": pick(mouts, "lends"), "protocol": pick(mouts, "runs_on"), "deployed": pick(outs, "deploys_into")}


def seen_price(g: DependencyGraph, collateral: str | None, oracle: str | None) -> tuple[bool, float, str]:
    """What the oracle shows the borrower: (sees a drop of the collateral token itself, volatility of the price it reports, why).

    An oracle the Morpho API cannot describe (type Unknown, no feeds; e.g. Pendle's own PT oracles) is assumed
    to follow the market. That is the conservative side for a borrower, the opposite of the lender model.
    """
    if not collateral:
        return False, 0.0, "no collateral"
    c = g.nodes[collateral]
    kind = c.attrs.get("kind", "unknown")
    o = g.nodes[oracle] if oracle else None
    if o is None:
        return False, 0.0, "no oracle data: market moves are not modelled as liquidation triggers"
    feeds = bool(o.attrs.get("feed_descriptions")) or any(e.type == "reads_feed" for e in g.out_edges(o.id))
    if o.risk_factors.get("tracks_market"):
        return True, asset_vol(kind), f"the oracle follows the {c.label} price ({kind.replace('_', ' ')} volatility)"
    if not feeds and o.attrs.get("oracle_type") in (None, "Unknown"):
        return True, asset_vol(kind), (f"oracle type not resolved: assumed to follow the {c.label} market price "
                                       f"(conservative for a borrower)")
    if not feeds:
        return False, 0.0, "the oracle prices the collateral at a fixed value: market moves cannot liquidate"
    if kind != "pt":
        return False, asset_vol(kind), (f"the oracle follows the reference price of {c.label} ({kind.replace('_', ' ')} volatility); "
                                        f"a depeg of the token itself is not seen")
    under = next((g.nodes[e.target] for e in g.out_edges(collateral) if e.type == "backed_by"), None)
    k = under.attrs.get("kind", "unknown") if under else "unknown"
    return False, asset_vol(k), f"the oracle prices {c.label} through {under.label if under else 'its underlying'}, not its own market"


def health(g: DependencyGraph, horizon_days: float) -> dict | None:
    """Live health of a borrow position: distance to liquidation, its chance within the horizon, and carry."""
    p = parts(g)
    a = p["position"].attrs
    if a.get("kind") != "borrow" or not p["market"]:
        return None
    m = g.nodes[p["market"]].attrs
    ltv, lltv, structure = a["ltv"], a["lltv"], a["structure"]
    E = a["equity_usd"]
    C, D, V = balance_sheet(structure, ltv)
    lif = liquidation_incentive(lltv)
    dist = 1.0 - ltv / lltv
    sees_drop, vol, why = seen_price(g, p["collateral"], p["oracle"])
    p_liq = hit_probability(dist, vol, horizon_days)
    penalty = sum(REPAID) / 2 * D * (lif - 1.0)
    pt_node = g.nodes[p["deployed"] or p["collateral"]]
    amm = next((g.nodes[e.target] for e in g.out_edges(pt_node.id) if e.type == "traded_on"), None)
    pt_apy = (amm.attrs.get("implied_apy") if amm else None) or 0.0
    borrow_apy = m.get("borrow_apy") or 0.0
    exposure = C if structure == "loop" else V
    carry = pt_apy * exposure - borrow_apy * D
    c_px = g.nodes[p["collateral"]].attrs.get("price_usd") if p["collateral"] else None
    return {
        "structure": structure, "equity_usd": E, "collateral_usd": C * E, "debt_usd": D * E, "deployed_usd": V * E,
        "collateral_label": g.nodes[p["collateral"]].label if p["collateral"] else None,
        "loan_label": g.nodes[p["loan"]].label if p["loan"] else None,
        "deployed_label": g.nodes[p["deployed"]].label if p["deployed"] else None,
        "leverage": C, "ltv": ltv, "lltv": lltv, "health_factor": lltv / ltv, "drop_to_liquidation": dist,
        "liquidation_price_usd": c_px * (1.0 - dist) if c_px else None,
        "liquidation_incentive": lif, "seen_vol": vol, "vol_reason": why, "oracle_sees_depeg": sees_drop, "p_liquidation": p_liq,
        "expected_penalty_pct": p_liq * penalty, "penalty_if_liquidated_pct": penalty,
        "pt_apy": pt_apy, "borrow_apy": borrow_apy, "net_apy": carry,
        "live": {k: a.get(k) for k in ("account", "health_factor_live")} if a.get("account") else None,
    }
