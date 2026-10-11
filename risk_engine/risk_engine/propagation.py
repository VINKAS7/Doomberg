"""Stage 2 - Shock: Monte Carlo propagation of one failure through a PT position's dependency graph.

Each node carries, per simulation draw:
  loss    fraction of value lost (token price drop, AMM exit discount, lender loss in a market)
  oerr    oracle price error, + means the oracle over-prices the collateral
  stale   hours an oracle is frozen
  frozen  hours a chain / protocol cannot process transactions

Nodes are evaluated dependencies-first; each edge type has a transmission rule.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from . import leverage
from .exit_cost import amm_impact
from .failures import asset_vol, severity_prior
from .graph import DependencyGraph

TRANSMISSION = {
    "backed_by": "the token is only worth what backs it",
    "issued_by": "the issuer must honour redemptions",
    "mintable_by": "unbacked minting dilutes every holder",
    "bridged_via": "a bridged copy is only as good as the bridge's locked collateral",
    "traded_on": "the PT is priced and exited through this AMM",
    "liquidated_in": "liquidations of this collateral are sold into the PT's AMM, pushing its price down",
    "collateral_in": "PT value drop -> under-water loans -> bad debt for lenders",
    "priced_by": "wrong or frozen oracle -> over-borrowing or failed liquidations",
    "reads_feed": "the oracle passes on whatever its feed reports",
    "runs_on": "a contract bug hits everything deployed on it",
    "denominated_in": "the claim is paid in this token; if it depegs, the claim is worth less",
    "deposits_in": "the position is a supply share of this market",
    "holds": "the position is this token",
    "borrows_from": "the position owes this market: a collateral drop its oracle sees triggers liquidation with a penalty",
    "deploys_into": "the borrowed money sits in this token; if it loses value, the debt still has to be repaid",
    "settles_on": "if the chain stops, nobody can liquidate, withdraw or sell",
    "lends": "loan asset of the market",
}


def _combine(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return 1.0 - (1.0 - a) * (1.0 - b)


def _years_to(expiry: str | None) -> float:
    """Years until a ``YYYY-MM-DD`` expiry; one year when unknown, so the arbitrage bound stays finite."""
    if not expiry:
        return 1.0
    try:
        t = datetime.strptime(expiry[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 1.0
    return max(t - time.time(), 0.0) / (365 * 86400)


@dataclass
class SimResult:
    loss: dict[str, np.ndarray]
    oerr: dict[str, np.ndarray]
    stale: dict[str, np.ndarray]
    frozen: dict[str, np.ndarray]
    severity: np.ndarray


class Simulator:
    def __init__(self, graph: DependencyGraph, seed: int = 7):
        self.g = graph
        self.order = graph.ancestors_order()
        self.rng = np.random.default_rng(seed)
        self.outs = {nid: graph.out_edges(nid) for nid in graph.nodes}

    def sample_severity(self, node_id: str, mode: str, n: int, fixed: float | None = None) -> np.ndarray:
        node = self.g.nodes[node_id]
        if fixed is not None:
            if mode in ("chain_halt", "oracle_stale"):
                return np.clip(self.rng.normal(fixed, 0.15 * fixed + 0.1, n), 0, None)
            return np.clip(self.rng.normal(fixed, 0.08 * fixed + 0.01, n), 0, 1)
        mean, kappa = severity_prior(mode, node.type, {**node.attrs, **node.risk_factors})
        if kappa == 0:
            return self.rng.exponential(mean, n)
        return self.rng.beta(mean * kappa, (1 - mean) * kappa, n)

    def run(self, shock: str, mode: str, n: int = 4000, fixed_severity: float | None = None) -> SimResult:
        rng, g = self.rng, self.g
        z = np.zeros(n)
        L, OE, ST, FZ = {}, {}, {}, {}
        sev = self.sample_severity(shock, mode, n, fixed_severity)
        # Unknown direction: over-pricing (the harmful case for lenders) 65% of the time. Replays use the observed direction.
        over = (np.ones(n, bool) if fixed_severity is not None else rng.random(n) < 0.65) if mode == "oracle_wrong_price" else None

        for nid in self.order:
            node = g.nodes[nid]
            t = node.type
            own = nid == shock
            loss, oerr, stale, frozen = z, z, z, z
            outs = self.outs[nid]

            def get(d, target):
                return d.get(target, z)

            for e in outs:
                if e.type in ("settles_on", "runs_on", "traded_on"):
                    frozen = np.maximum(frozen, get(FZ, e.target))

            if t == "chain":
                if own and mode == "chain_halt":
                    frozen = np.maximum(frozen, sev)

            elif t in ("protocol", "issuer", "bridge", "mint_authority"):
                if own:
                    loss = sev

            elif t in ("feed", "oracle"):
                if own and mode == "oracle_wrong_price":
                    oerr = np.where(over, sev, -sev)
                if own and mode == "oracle_stale":
                    stale = sev
                price_factor = 1.0 + oerr
                for e in outs:
                    if e.type == "reads_feed":
                        # Compose price ratios exactly: base feeds multiply,
                        # quote feeds divide. Keep zero-price failures finite.
                        factor = np.maximum(1.0 + get(OE, e.target), 1e-9)
                        if str(e.attrs.get("role", "")).startswith("quote"):
                            price_factor = price_factor / factor
                        else:
                            price_factor = price_factor * factor
                        stale = np.maximum(stale, get(ST, e.target))
                oerr = price_factor - 1.0

            elif t == "amm":
                if own and mode == "liquidity_drain":
                    loss = sev
                elif own and mode == "exploit":
                    loss = sev * 0.3
                for e in outs:
                    if e.type == "runs_on":
                        loss = _combine(loss, get(L, e.target) * 0.3)

            elif t == "asset":
                if own and mode in ("depeg", "unbacked_mint"):
                    loss = sev
                for e in outs:
                    if e.type in ("backed_by", "issued_by", "mintable_by", "bridged_via", "traded_on"):
                        loss = _combine(loss, get(L, e.target))
                if any(e.type == "liquidated_in" for e in outs):
                    loss = _combine(loss, self._fire_sale(outs, loss, OE, ST, FZ, n))

            elif t == "market":
                loss = self._market(node, outs, L, OE, ST, frozen, mode, n)
                if own and mode == "liquidity_drain":
                    # A run strands the lent-out share of supply; a lender who needs out waits or sells the claim at a discount.
                    util = min(max(node.attrs.get("utilization") or 0.0, 0.0), 1.0)
                    loss = _combine(loss, sev * util)

            elif t == "position" and node.attrs.get("kind") == "borrow":
                loss = self._borrow(node, L, OE, ST, FZ, n)

            elif t == "position":
                for e in outs:
                    if e.type in ("deposits_in", "holds", "denominated_in"):
                        loss = _combine(loss, get(L, e.target))
                    if e.type == "holds" and get(FZ, e.target).any():
                        # A held PT cannot be sold while its venue is frozen; its price keeps moving meanwhile.
                        kind = g.nodes[e.target].attrs.get("kind", "unknown")
                        move = np.abs(rng.normal(0, 1, n)) * asset_vol(kind) * np.sqrt(get(FZ, e.target) / 8760.0)
                        loss = _combine(loss, np.clip(move, 0, 0.99))

            L[nid], OE[nid], ST[nid], FZ[nid] = loss, oerr, stale, frozen
        return SimResult(L, OE, ST, FZ, sev)

    def _fire_sale(self, outs, drop, OE, ST, FZ, n) -> np.ndarray:
        """PT price dip from liquidations in the lending markets that hold it, sold into its Pendle AMM.

        A venue liquidates when its oracle shows a lower price: it under-prices the PT, or it tracks the
        market and sees ``drop`` (the PT's loss from everything else). Arbitrage to maturity bounds the dip:
        nobody sells a sound PT below the price that yields ``apy_stress`` until it redeems.
        """
        rng, g = self.rng, self.g
        z = np.zeros(n)
        amm = next((g.nodes[e.target] for e in outs if e.type == "traded_on"), None)
        depth = (amm.attrs.get("liquidity_usd") if amm else None) or 0.0
        tau = _years_to(amm.attrs.get("expiry") if amm else None)
        if depth <= 0 or tau <= 0:
            return z  # no pool to sell into is an exit problem (exit_cost); an expired PT redeems 1:1
        sold = z
        for e in outs:
            if e.type != "liquidated_in":
                continue
            v = g.nodes[e.target]
            orc = next((x.target for x in self.outs[v.id] if x.type == "priced_by"), None)
            tracks = bool(g.nodes[orc].risk_factors.get("tracks_market")) if orc else False
            seen = (1.0 - (drop if tracks else 0.0)) * (1.0 + OE.get(orc, z))
            s = np.clip(1.0 - seen, 0.0, 1.0)
            # Borrowers sit at LTV/LLTV ~ U(0.70, 0.97), as in _market: those above 1 - s are liquidated.
            share = np.clip((0.97 - (1.0 - s)) / 0.27, 0.0, 1.0)
            share = np.where((ST.get(orc, z) > 0) | (FZ.get(v.id, z) > 0), 0.0, share)  # nobody can liquidate
            lltv = v.attrs.get("lltv") or 0.86
            backing = min(v.attrs.get("collateral_usd") or 0.0, (v.attrs.get("borrow_usd") or 0.0) / (lltv * 0.835))
            sold = sold + backing * share * rng.uniform(0.5, 1.0, n)  # some liquidators keep the PT to maturity
        cap = 1.0 - (1.0 + rng.uniform(0.5, 2.0, n)) ** (-tau)
        return np.minimum(amm_impact(sold, depth), cap)

    def _borrow(self, node, L, OE, ST, FZ, n) -> np.ndarray:
        """Borrower's equity loss: leveraged collateral and deployed-PT losses, plus liquidation penalties."""
        z = np.zeros(n)
        p = leverage.parts(self.g)
        get = lambda d, k: d.get(k, z) if k else z  # noqa: E731
        orc = p["oracle"]
        tracks, vol, _ = leverage.seen_price(self.g, p["collateral"], orc)
        blind = np.maximum(get(ST, orc), get(FZ, p["market"]))
        a = node.attrs
        return leverage.borrower_loss(a["structure"], a["ltv"], a["lltv"], self.rng, n,
                                      dc=get(L, p["collateral"]), dv=get(L, p["deployed"]), dl=get(L, p["loan"]),
                                      dp=get(L, p["protocol"]), oerr=get(OE, orc), tracks=tracks, blind_h=blind, vol=vol)

    def _market(self, node, outs, L, OE, ST, frozen, mode, n) -> np.ndarray:
        """Lender-side loss of a Morpho market as a fraction of supplied assets."""
        rng = self.rng
        z = np.zeros(n)
        lltv = node.attrs.get("lltv") or 0.86
        util = min(max(node.attrs.get("utilization") or 0.0, 0.0), 1.0)
        coll = next((e.target for e in outs if e.type == "collateral_in"), None)
        orc = next((e.target for e in outs if e.type == "priced_by"), None)
        proto = next((e.target for e in outs if e.type == "runs_on"), None)
        d = np.clip(L.get(coll, z), 0, 0.999)
        oerr = OE.get(orc, z)
        stale = ST.get(orc, z)
        tracks = bool(self.g.nodes[orc].risk_factors.get("tracks_market")) if orc else False
        vol = asset_vol(self.g.nodes[coll].attrs.get("kind", "unknown")) if coll else 0.3

        # Average borrower LTV sits a little below the liquidation threshold.
        ell = lltv * rng.uniform(0.70, 0.97, n)
        # Share of an insolvency liquidators can prevent: only when the oracle sees the move in time.
        sudden = mode in ("unbacked_mint", "bridge_hack", "exploit")
        eff = rng.uniform(0.0, 0.3, n) if sudden else rng.uniform(0.3, 0.8, n)
        if not tracks:
            eff = z
        eff = np.where((stale > 0) | (frozen > 0), 0.0, eff)
        insolvent = np.maximum(0.0, 1.0 - (1.0 - d) / ell)
        bad_debt = insolvent * (1.0 - eff)

        # Price moves while nobody can liquidate (stale oracle or halted chain).
        blind_h = np.maximum(stale, frozen)
        if blind_h.any():
            move = np.abs(rng.normal(0, 1, n)) * vol * np.sqrt(blind_h / 8760.0)
            bad_debt = _combine(bad_debt, np.maximum(0.0, 1.0 - (1.0 - np.clip(move, 0, 0.99)) / ell))

        # Over-borrowing: the oracle over-prices collateral (manipulated, or blind to a real drop).
        unseen = np.where(tracks & (stale == 0), 0.0, d / (1.0 - d))
        e = np.maximum(oerr, 0.0) + unseen
        drain = (1.0 - util) * np.maximum(0.0, 1.0 - 1.0 / (lltv * (1.0 + e))) * rng.uniform(0.6, 1.0, n)

        loss = np.clip(util * bad_debt + drain, 0, 1)
        if proto:
            loss = _combine(loss, L.get(proto, z))
        return loss


def summarize(arr: np.ndarray) -> dict:
    return {
        "mean": float(arr.mean()),
        "p05": float(np.quantile(arr, 0.05)),
        "p50": float(np.quantile(arr, 0.50)),
        "p95": float(np.quantile(arr, 0.95)),
        "p99": float(np.quantile(arr, 0.99)),
        "prob_any_loss": float((arr > 1e-4).mean()),
    }


def histogram(arr: np.ndarray, bins: int = 40) -> list[dict]:
    hi = max(float(np.quantile(arr, 0.995)), 1e-3)
    counts, edges = np.histogram(np.clip(arr, 0, hi), bins=bins, range=(0, hi))
    total = counts.sum() or 1
    return [{"x0": float(edges[i]), "x1": float(edges[i + 1]), "share": float(c / total)} for i, c in enumerate(counts)]


def explain_paths(g: DependencyGraph, sim: SimResult, shock: str, max_paths: int = 4) -> list[dict]:
    means = {k: float(v.mean()) for k, v in sim.loss.items()}
    out = []
    for path in g.paths_to(shock, max_paths=12):
        hops = []
        for i, nid in enumerate(path):
            n = g.nodes[nid]
            edge = g.edge_between(path[i - 1], nid) if i else None
            hops.append({
                "node_id": nid, "label": n.label, "type": n.type,
                "mean_loss": means.get(nid, 0.0),
                "oracle_error": float(np.abs(sim.oerr[nid]).mean()) if nid in sim.oerr and n.type in ("oracle", "feed") else None,
                "edge_type": edge.type if edge else None,
                "edge_weight": edge.weight if edge else None,
                "why": TRANSMISSION.get(edge.type) if edge else None,
            })
        strength = min((h["mean_loss"] for h in hops[:-1]), default=0.0)
        out.append({"hops": hops, "strength": strength})
    out.sort(key=lambda p: -p["strength"])
    return out[:max_paths]
