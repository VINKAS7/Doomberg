"""Orchestrates Discover -> Monitor -> Shock -> Price the exit -> Decide, and keeps analysed positions in memory.

Every screening reads live signals (``signals.py``) to set alert levels and a contagion multiplier
(``contagion.py``) to make failure rates time-varying. With a ``History`` attached, a screening can be
recorded: the run, its forecasts and signals are stored, compared with the previous run of the same
position, and changes raise notifications (``notify.py``).

The Monte Carlo seed is fixed on purpose: with common random numbers, a change between two runs
comes from the live inputs, not from sampling noise.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from . import contagion, notify
from .decision import decide
from .discovery import Discovery, PositionRequest
from .exit_cost import exit_cost
from .explain import explain
from .failures import MODES, applicable_modes, severity_prior
from .graph import DependencyGraph
from .history import History
from .leverage import health as leverage_health
from .probability import ALERT_LEVELS, failure_probability
from .propagation import Simulator, explain_paths, histogram, summarize
from .signals import LEVEL_RANK, alert_levels, data_issues, detect, max_level, node_class, observe

INCIDENTS = json.loads((Path(__file__).parent / "data" / "incidents.json").read_text(encoding="utf-8"))["incidents"]

PRESETS = [
    {"id": "pt-reusd-market", "label": "Brief example: PT-reUSD as collateral (USDC supply)", "kind": "market", "chain_id": 1,
     "address": "0x1e9d614631a7df0ec07fb05b2c8cb2491575fd1a63a33bf187a6afb295a4fc64", "size_usd": 1_000_000,
     "note": "The 5-layer Pendle PT example from the brief: chain, yield source, oracle, PT AMM, lending market"},
    {"id": "pt-usd3-market", "label": "PT-USD3 as collateral (USDC supply)", "kind": "market", "chain_id": 1,
     "address": "0xf8c5aa31ea6b2a068a9eddb46dd110cae57bf0f12be9583a3f9a818effecba89", "size_usd": 1_000_000,
     "note": "LLTV 86%, utilization above 90%: idle liquidity runs thin once lenders rush out"},
    {"id": "pt-susde-market", "label": "PT-sUSDe as collateral (USDC supply)", "kind": "market", "chain_id": 1,
     "address": "0x5613b170ded221c588b6e9637b910f49e5508680ceede207f44aa72e5c46cb05", "size_usd": 1_000_000,
     "note": "Ethena PT lent against at LLTV 91.5%"},
    {"id": "pt-reusd", "label": "PT-reUSD (held)", "kind": "token", "chain_id": 1,
     "address": "0xecfafdc7741323a945a163ed068b5a3c43483957", "size_usd": 1_000_000,
     "note": "The Pendle PT from the brief: Re Protocol reUSD yield source, Pendle AMM, settles on Ethereum"},
    {"id": "pt-susde", "label": "PT-sUSDe (held)", "kind": "token", "chain_id": 1,
     "address": "0xb195b618ea52b77cb2a58846f452f59f8dfa9390", "size_usd": 1_000_000,
     "note": "Ethena staked USDe: delta-neutral synthetic dollar with off-exchange custody"},
    {"id": "pt-strusd", "label": "PT-strUSD (held)", "kind": "token", "chain_id": 1,
     "address": "0x74299580811e1c3c1a2831db079ba0a8f513998f", "size_usd": 1_000_000,
     "note": "Collateral in RockawayX-curated Morpho vaults"},
    {"id": "pt-fxsave", "label": "PT-fxSAVE (held)", "kind": "token", "chain_id": 1,
     "address": "0x079c3469143804af5d0d645a878c371dbe8d038e", "size_usd": 1_000_000,
     "note": "f(x) Protocol savings token; thin Pendle liquidity"},
    {"id": "pt-wsteth", "label": "PT-wstETH (held)", "kind": "token", "chain_id": 1,
     "address": "0xb253eff1104802b97ac7e3ac9fdd73aece295a2c", "size_usd": 1_000_000,
     "note": "Lido wrapped staked ETH: blue-chip LST underlying"},
    {"id": "btc-borrow-pt-susde", "label": "cbBTC collateral -> borrow USDC -> PT-sUSDe", "kind": "borrow", "chain_id": 1,
     "address": "0x64d65c9a2d91c36d56fbc42d69e979335320169b3df63bf92789e2c8883fcc64", "size_usd": 1_000_000,
     "ltv": 0.40, "deploy_address": "0xb195b618ea52b77cb2a58846f452f59f8dfa9390",
     "note": "The Bitcoin-client strategy: keep the BTC as collateral, borrow USDC at 40% LTV, earn the PT's fixed yield"},
    {"id": "pt-susde-loop", "label": "PT-sUSDe loop (borrow USDC at 80% LTV)", "kind": "borrow", "chain_id": 1,
     "address": "0x5613b170ded221c588b6e9637b910f49e5508680ceede207f44aa72e5c46cb05", "size_usd": 1_000_000,
     "ltv": 0.80, "note": "The brief's layer 5 read literally: the PT itself posted as collateral, levered 5x"},
]


def _clean(obj):
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items() if k != "samples"}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, float) and (obj != obj or obj in (float("inf"), float("-inf"))):
        return None
    return obj


def position_key(req: PositionRequest, horizon_days: float) -> str:
    """Runs are comparable only at the same size and horizon (both move the exit cost and the probabilities),
    and for a borrow position at the same leverage and deploy target. A borrower read from chain is keyed by its
    account: its size and LTV move every day, and the key must be known before discovery reads them."""
    base = f"{req.kind}:{req.chain_id}:{req.address.strip().lower()}"
    if req.kind == "borrow":
        deploy = (req.deploy_address or "").strip().lower()
        if req.account and req.account.strip():
            return f"{base}:acct:{req.account.strip().lower()}:{horizon_days:g}d:{deploy}"
        return f"{base}:{req.size_usd:.0f}:{horizon_days:g}d:ltv{req.ltv or 0:.3f}:{deploy}"
    return f"{base}:{req.size_usd:.0f}:{horizon_days:g}d"


def request_from(it: dict) -> PositionRequest:
    """A ``PositionRequest`` from a preset or watchlist dict."""
    ltv = it.get("ltv")
    return PositionRequest(it.get("kind", "token"), it["address"], int(it.get("chain_id", 1)),
                           float(it.get("size_usd", 1_000_000)), float(it.get("book_usd") or 10_000_000), it.get("label"),
                           float(ltv) if ltv is not None else None, it.get("deploy_address") or None, it.get("account") or None)


class RiskEngine:
    def __init__(self, seed: int = 11, history: History | None = None):
        self.store: dict[str, dict] = {}
        self.seed = seed
        self.history = history

    # ------------------------------------------------------------------ analyse
    def analyze(self, req: PositionRequest, horizon_days: float = 30, record: bool = False, now: float | None = None) -> dict:
        return self.evaluate(Discovery(req).run(), req, horizon_days, record=record, now=now)

    def evaluate(self, g: DependencyGraph, req: PositionRequest, horizon_days: float = 30, record: bool = False,
                 now: float | None = None) -> dict:
        """Screen a discovered graph. ``record`` stores the run in the history and raises notifications."""
        now = now if now is not None else time.time()
        pid = hashlib.sha1(f"{req.kind}{req.address.lower()}{req.chain_id}{req.size_usd}{now}{time.time()}".encode()).hexdigest()[:12]
        rng = np.random.default_rng(self.seed)
        signals = detect(g, now, self.history)
        levels = alert_levels(signals)
        keys = [(nid, m.id) for nid, node in g.nodes.items() if node.type != "position" for m in applicable_modes(node.type, node.attrs)]
        mults = contagion.multipliers(g, keys, contagion.gather_events(g, signals, now, self.history), now)
        screening = self.screen(g, horizon_days, rng, levels, mults)
        calm_exit = exit_cost(g, req.size_usd, rng, stressed=False)
        lev = leverage_health(g, horizon_days)
        monitor = self.monitor(g, req, horizon_days, screening, calm_exit, signals, levels, mults, record, now, lev)
        self.store[pid] = {"req": req, "graph": g, "screening": screening, "horizon": horizon_days, "levels": levels, "mults": mults}
        graph = g.to_dict()
        for n in graph["nodes"]:
            r = screening["nodes"].get(n["id"])
            n["risk"] = r
            n["modes"] = [{"id": m.id, "label": m.label} for m in applicable_modes(n["type"], n["attrs"])]
        return _clean({
            "position_id": pid,
            "request": asdict(req),
            "graph": graph,
            "screening": {k: v for k, v in screening.items() if k not in ("nodes", "rows")},
            "monitor": monitor,
            "exit_now": calm_exit,
            "leverage": lev,
            "stats": {"nodes": len(g.nodes), "edges": len(g.edges),
                      "evidence": sum(len(n.evidence) for n in g.nodes.values()) + sum(len(e.evidence) for e in g.edges.values()),
                      "depth": max((n.layer for n in g.nodes.values()), default=0),
                      "discovery_seconds": g.nodes["position"].attrs.get("discovery_seconds"),
                      "warnings": g.nodes["position"].attrs.get("warnings")},
        })

    def screen(self, g: DependencyGraph, horizon_days: float, rng: np.random.Generator,
               levels: dict | None = None, mults: dict | None = None, n: int = 500) -> dict:
        """Risk of every (component, failure mode): P(fail in horizon) x E[position loss | fail].

        P starts from the base rate, is scaled by the contagion multiplier and updated by the alert
        level the live signals set for that component and mode.
        """
        levels, mults = levels or {}, mults or {}
        sim = Simulator(g, seed=self.seed)
        rows = []
        for nid, node in g.nodes.items():
            if node.type == "position":
                continue
            for m in applicable_modes(node.type, node.attrs):
                lv = levels.get((nid, m.id))
                if m.id == "oracle_stale" and lv and all(
                    s.get("origin_node_id", nid) != nid for s in lv["signals"]
                ):
                    # The parent displays its feed's warning, but the feed owns the forecast.
                    continue
                mu = mults.get((nid, m.id)) or {"multiplier": 1.0, "reasons": []}
                prob = failure_probability(node, m.id, horizon_days, rng, alert=lv["level"] if lv else "none", n=1000,
                                           rate_multiplier=mu["multiplier"], multiplier_reasons=mu["reasons"])
                res = sim.run(nid, m.id, n=n)
                lgf = float(res.loss[g.root].mean())
                rows.append({"node_id": nid, "label": node.label, "type": node.type, "node_class": node_class(node),
                             "mode": m.id, "mode_label": m.label, "p": prob["p"], "prior_p": prob["prior_p"], "base_p": prob["base_p"],
                             "annual_rate": prob["annual_rate"], "loss_given_failure": lgf,
                             "expected_loss_pct": prob["p"] * lgf, "reasons": prob["reasons"],
                             "alert": lv["level"] if lv else "none", "likelihood_ratio": prob["likelihood_ratio"],
                             "rate_multiplier": mu["multiplier"], "signals": [s["detail"] for s in lv["signals"]] if lv else []})
        total = sum(r["expected_loss_pct"] for r in rows) or 1e-12
        nodes: dict[str, dict] = {}
        for r in rows:
            r["share"] = r["expected_loss_pct"] / total
            d = nodes.setdefault(r["node_id"], {"expected_loss_pct": 0.0, "share": 0.0, "modes": []})
            d["expected_loss_pct"] += r["expected_loss_pct"]
            d["share"] += r["share"]
            d["modes"].append({k: r[k] for k in ("mode", "mode_label", "p", "loss_given_failure", "expected_loss_pct", "share", "alert")})
        for d in nodes.values():
            d["modes"].sort(key=lambda x: -x["expected_loss_pct"])
            d["top_mode"] = d["modes"][0]["mode"] if d["modes"] else None
        rows.sort(key=lambda r: -r["expected_loss_pct"])
        p_any = 1 - float(np.prod([1 - r["p"] for r in rows if r["loss_given_failure"] > 1e-4]))
        return {"horizon_days": horizon_days, "total_expected_loss_pct": total, "p_any_loss_event": p_any,
                "top": rows[:12], "nodes": nodes, "rows": rows}

    # ------------------------------------------------------------------ monitor
    def monitor(self, g: DependencyGraph, req: PositionRequest, horizon_days: float, screening: dict, calm_exit: dict,
                signals: list, levels: dict, mults: dict, record: bool, now: float, lev: dict | None = None) -> dict:
        """Exit if (expected loss - yield given up) x withdrawable share > exit cost, base-rate risk included.

        Staying earns the position's yield (PT implied APY, or the market's supply APY) over the horizon.
        The exit cost already charges the yield lost while redeploying, so only the rest of the horizon is
        credited, and the exited capital is assumed to earn nothing after that.
        Missing required readings suppress an actionable verdict and produce REVIEW.
        A borrow position also carries the expected penalty of being liquidated by an ordinary price move
        (``leverage.health``). It is a base rate, so it adds to the baseline as well.
        """
        c, f = calm_exit["cost_pct"], min(max(calm_exit["instant_fraction"], 0.0), 1.0)
        yld = (calm_exit.get("apy") or 0.0) * max(horizon_days - calm_exit.get("redeploy_days", 0.0), 0.0) / 365
        rows = screening["rows"]
        for r in rows:
            lf = r["loss_given_failure"] * f
            r["break_even_p"] = c / lf if lf > 1e-12 and c / lf < 1 else None
            r["above_break_even"] = r["break_even_p"] is not None and r["p"] > r["break_even_p"]
        liq = lev["expected_penalty_pct"] if lev else 0.0
        el = screening["total_expected_loss_pct"] + liq
        base_el = sum(r["base_p"] * r["loss_given_failure"] for r in rows) + liq
        excess = max(el - base_el, 0.0)
        net = (el - yld) * f
        model_verdict = "EXIT" if net > c else "STAY"
        issues = data_issues(g)
        verdict = "REVIEW" if issues else model_verdict
        key = position_key(req, horizon_days)
        sig = [{**s.to_dict(), "node_class": node_class(g.nodes[s.origin_node_id or s.node_id]),
                "effective_level": levels[(s.node_id, s.mode)]["level"]} for s in signals]
        run = {
            "ts": now, "position_key": key, "label": g.nodes[g.root].label, "kind": req.kind, "address": req.address,
            "chain_id": req.chain_id, "size_usd": req.size_usd, "horizon_days": horizon_days, "verdict": verdict,
            "expected_loss_pct": el, "baseline_loss_pct": base_el, "avoidable_loss_pct": el * f, "p_any": screening["p_any_loss_event"],
            "yield_pct": yld, "net_loss_pct": net, "liquidation_loss_pct": liq,
            "exit_cost_pct": c, "instant_fraction": f, "max_alert": max_level(levels),
            "contagion": max((m["multiplier"] for m in mults.values()), default=1.0),
            "summary": {"data_issues": issues, "model_verdict": model_verdict,
                        "decision_rule": "net_of_yield", "rows": {f"{r['node_id']}|{r['mode']}": {
                "node_id": r["node_id"], "mode": r["mode"], "label": r["label"], "mode_label": r["mode_label"], "p": r["p"],
                "break_even_p": r["break_even_p"], "above": r["above_break_even"], "alert": r["alert"],
                "el": r["expected_loss_pct"], "signals": r["signals"]} for r in rows}},
        }
        prev = self.history.last_run(key, before=now) if self.history else None
        events = notify.diff(prev, run, now)
        if record and self.history:
            # Unknown readings must not create apparently valid calibration forecasts.
            self.history.record_run(run, [] if issues else rows, sig, observe(g))
            self.history.add_events(events)
            notify.deliver(events)
        above = sorted((r for r in rows if r["above_break_even"]), key=lambda r: -r["p"] / r["break_even_p"])
        return {
            "position_key": key, "label": run["label"], "verdict": verdict, "expected_loss_pct": el, "baseline_loss_pct": base_el,
            "excess_loss_pct": excess, "avoidable_loss_pct": el * f, "yield_pct": yld, "net_loss_pct": net,
            "liquidation_loss_pct": liq,
            "model_verdict": model_verdict, "data_status": "incomplete" if issues else "complete", "data_issues": issues,
            "decision_rule": "net_of_yield", "execution_status": "blocked" if f == 0 else "available",
            "exit_cost_pct": c, "instant_fraction": f, "max_alert": run["max_alert"],
            "regime": contagion.regime(mults), "contagion_max": run["contagion"],
            "signals": sorted(sig, key=lambda s: -LEVEL_RANK[s["level"]]),
            "alerts": sorted(({"node_id": k[0], "label": g.nodes[k[0]].label, "mode": k[1], "mode_label": MODES[k[1]].label,
                               "level": v["level"], "signals": v["signals"]} for k, v in levels.items()),
                             key=lambda a: -LEVEL_RANK[a["level"]]),
            "above_break_even": [{k: r[k] for k in ("node_id", "label", "mode", "mode_label", "p", "break_even_p", "alert")} for r in above],
            "previous": {k: prev.get(k) for k in ("ts", "verdict", "expected_loss_pct", "baseline_loss_pct", "avoidable_loss_pct", "p_any",
                                                  "yield_pct", "net_loss_pct", "exit_cost_pct", "max_alert")}
                        if prev else None,
            "events": events,
            "recorded": bool(record and self.history),
        }

    # ------------------------------------------------------------------ shock
    def shock(self, position_id: str, node_id: str, mode: str, alert: str = "auto",
              probability_override: float | None = None, exit_cost_override: float | None = None,
              horizon_days: float | None = None, n: int = 5000, fixed_severity: float | None = None,
              use_llm: bool = True, incident: dict | None = None) -> dict:
        entry = self.store.get(position_id)
        if not entry:
            raise KeyError("Unknown position_id; analyse the position first")
        g: DependencyGraph = entry["graph"]
        req: PositionRequest = entry["req"]
        if node_id not in g.nodes:
            raise KeyError(f"Unknown node {node_id}")
        if mode not in MODES:
            raise KeyError(f"Unknown failure mode {mode}")
        node = g.nodes[node_id]
        if all(m.id != mode for m in applicable_modes(node.type, node.attrs)):
            raise ValueError(f"Failure mode '{mode}' does not apply to {node.label} ({node.type})")
        horizon = horizon_days or entry["horizon"]
        rng = np.random.default_rng(self.seed + 1)
        live = entry["levels"].get((node_id, mode))
        source = "signals" if alert == "auto" else "manual"
        if alert == "auto":
            alert = live["level"] if live else "none"
        mu = entry["mults"].get((node_id, mode)) or {"multiplier": 1.0, "reasons": []}

        sim = Simulator(g, seed=self.seed + 2).run(node_id, mode, n=n, fixed_severity=fixed_severity)
        pos_loss = sim.loss[g.root]
        prob = failure_probability(node, mode, horizon, rng, alert=alert, n=n,
                                   rate_multiplier=mu["multiplier"], multiplier_reasons=mu["reasons"])

        denom = next((e.target for e in g.out_edges(g.root) if e.type == "denominated_in"), None)
        sell = denom if denom and float(sim.loss[denom].mean()) > 1e-3 else None
        stressed = float(pos_loss.mean()) > 1e-4
        ex = exit_cost(g, req.size_usd, rng, stressed=stressed, sell_asset_id=sell, n=n)
        cost_pct = exit_cost_override if exit_cost_override is not None else ex["cost_pct"]
        if exit_cost_override is not None:
            ex["override"] = exit_cost_override

        dec = decide(req.size_usd, req.book_usd, prob["samples"], pos_loss, cost_pct,
                     None if exit_cost_override is not None else ex["samples"], ex["instant_fraction"], probability_override)

        paths = explain_paths(g, sim, node_id)
        sev_mean, sev_kappa = severity_prior(mode, node.type, {**node.attrs, **node.risk_factors})
        impacted = sorted(({"node_id": k, "label": g.nodes[k].label, "type": g.nodes[k].type, "mean_loss": float(v.mean())}
                           for k, v in sim.loss.items() if float(v.mean()) > 1e-4), key=lambda x: -x["mean_loss"])
        result = {
            "position": {"label": g.nodes[g.root].label, "size_usd": req.size_usd, "book_usd": req.book_usd, "kind": req.kind},
            "shock": {"node_id": node_id, "node_label": node.label, "node_type": node.type, "mode": mode,
                      "mode_label": MODES[mode].label, "description": MODES[mode].description,
                      "alert": alert, "alert_label": ALERT_LEVELS.get(alert, {}).get("label"), "alert_source": source,
                      "signals": live["signals"] if live else [],
                      "severity": summarize(sim.severity), "incident": incident},
            "probability": prob,
            "loss": {**summarize(pos_loss), "histogram": histogram(pos_loss)},
            "exit": ex,
            "decision": dec,
            "paths": paths,
            "impacted": impacted[:25],
            "node_losses": {k: float(v.mean()) for k, v in sim.loss.items()},
            "leverage": leverage_health(g, horizon),
            "findings": self.findings(g, paths),
            "assumptions": self.assumptions(mode, sev_mean, sev_kappa, horizon, n, ex, fixed_severity, req.kind == "borrow"),
        }
        result["explanation"] = explain(_clean(result), use_llm=use_llm)
        return _clean(result)

    def replay(self, position_id: str, incident_id: str, **kw) -> dict:
        entry = self.store.get(position_id)
        if not entry:
            raise KeyError("Unknown position_id")
        inc = next((i for i in INCIDENTS if i["id"] == incident_id), None)
        if not inc:
            raise KeyError("Unknown incident")
        g: DependencyGraph = entry["graph"]
        node_id = self.match_incident(g, inc, entry["screening"])
        if not node_id:
            raise ValueError(f"This position has no component comparable to '{inc['name']}' ({inc['kind']})")
        sev = inc["duration_h"] if inc["mode"] in ("chain_halt", "oracle_stale") else inc["severity"]
        return self.shock(position_id, node_id, inc["mode"], alert="confirmed", fixed_severity=sev, incident=inc, **kw)

    @staticmethod
    def match_incident(g: DependencyGraph, inc: dict, screening: dict) -> str | None:
        want = {"depeg": ("asset",), "unbacked_mint": ("mint_authority", "asset"), "oracle_wrong_price": ("oracle",),
                "oracle_stale": ("oracle",), "bridge_hack": ("bridge",), "exploit": ("protocol",),
                "chain_halt": ("chain",)}.get(inc["mode"]) or MODES[inc["mode"]].applies_to
        # Only components the mode can actually hit (e.g. a PT cannot be minted without backing).
        cands = [n for n in g.nodes.values() if n.type in want
                 and any(m.id == inc["mode"] for m in applicable_modes(n.type, n.attrs))]
        if inc["mode"] == "depeg":
            sym = [n for n in cands if inc.get("symbol") and n.label.lower() == inc["symbol"]]
            same_kind = [n for n in cands if n.attrs.get("kind") == inc["kind"]]
            near = {"fiat_stable": ("eur_stable",), "synthetic_stable": ("yield_stable", "rwa", "unknown"),
                    "yield_stable": ("synthetic_stable", "rwa", "unknown"), "lst": ("lrt",)}.get(inc["kind"], ())
            close = [n for n in cands if n.attrs.get("kind") in near]
            cands = sym or same_kind or close
        if inc["mode"] == "chain_halt":
            cands = [n for n in cands if n.chain_id == g.nodes[g.root].chain_id] or cands
        if not cands:
            return None
        score = lambda n: screening["nodes"].get(n.id, {}).get("expected_loss_pct", 0)
        return max(cands, key=score).id

    @staticmethod
    def findings(g: DependencyGraph, paths: list[dict]) -> list[str]:
        out, seen = [], set()
        for p in paths[:2]:
            for h in p["hops"]:
                n = g.nodes[h["node_id"]]
                if n.id in seen:
                    continue
                seen.add(n.id)
                a, rf = n.attrs, n.risk_factors
                if n.type == "oracle" and a.get("tracking_reason"):
                    out.append(f"{n.label}: {a['tracking_reason']} (feeds: {', '.join(a.get('feed_descriptions') or []) or 'none'})")
                if n.type == "feed" and a.get("stale"):
                    out.append(f"Feed {n.label} last updated {int((a.get('age_s') or 0) / 3600)}h ago")
                if n.type == "asset" and a.get("collateral_elsewhere_usd"):
                    venues = [g.nodes[e.target] for e in g.out_edges(n.id) if e.type == "liquidated_in"]
                    blind = sum(not g.nodes[o.target].risk_factors.get("tracks_market")
                                for v in venues for o in g.out_edges(v.id) if o.type == "priced_by")
                    ratio = a.get("collateral_to_amm")
                    other = " other" if g.nodes[g.root].attrs.get("kind") == "market" else ""
                    out.append(f"{n.label}: ${a['collateral_elsewhere_usd']:,.0f} posted as collateral in {len(venues)}{other} lending market(s)"
                               + (f", {ratio:.1f}x its AMM liquidity" if ratio else "")
                               + f"; {blind} of them priced by an oracle that does not follow the PT's market price")
                if n.type == "market" and a.get("role") == "borrow_market":
                    pa = g.nodes[g.root].attrs
                    out.append(f"Borrow market {n.label}: LTV {pa['ltv']:.1%} vs LLTV {pa['lltv']:.1%} (health factor "
                               f"{pa['lltv'] / pa['ltv']:.2f}); an oracle drop of {1 - pa['ltv'] / pa['lltv']:.1%} triggers liquidation "
                               f"with a {pa['lif'] - 1:.1%} penalty on the debt repaid; borrow APY {(a.get('borrow_apy') or 0):.2%}")
                elif n.type == "market" and a.get("role") == "collateral_venue":
                    out.append(f"Collateral venue {n.label}: ${(a.get('collateral_usd') or 0):,.0f} posted against "
                               f"${(a.get('borrow_usd') or 0):,.0f} borrowed at LLTV {(a.get('lltv') or 0):.1%}")
                elif n.type == "market":
                    out.append(f"Market {n.label}: utilization {(a.get('utilization') or 0):.0%}, LLTV {(a.get('lltv') or 0):.1%}, "
                               f"idle liquidity ${(a.get('liquidity_usd') or 0):,.0f}, age {a.get('age_days')} days")
                if n.type == "amm":
                    out.append(f"{n.label}: liquidity ${(a.get('liquidity_usd') or 0):,.0f}, expiry {a.get('expiry')}")
                if n.type == "mint_authority":
                    out.append(f"Mint / upgrade control of the token: {n.label} ({a.get('role') or 'role-based'})")
                if n.type == "asset" and rf.get("unverified"):
                    out.append(f"{n.label}: issuer and backing are not in the curated registry - classified as {a.get('kind')} by heuristic")
        return out[:8]

    @staticmethod
    def assumptions(mode, sev_mean, sev_kappa, horizon, n, ex, fixed, borrow: bool = False) -> list[str]:
        a = [
            f"Horizon {horizon:g} days (Belem clients can redeem monthly)",
            f"{n:,} Monte Carlo draws",
            (f"Replay: severity fixed at the observed {fixed:g} (+/- noise)" if fixed is not None else
             f"Severity at the failing component: exponential, mean {sev_mean:g}h" if sev_kappa == 0 else
             f"Severity at the failing component: Beta(mean {sev_mean:.0%}, concentration {sev_kappa:g})"),
            "The PT loses whatever its underlying, issuer, mint controls or AMM lose (combined as independent losses)",
            "While the chain is halted a held PT cannot be sold; its price drifts with the volatility of its asset class",
            "Borrowers sit at 70-97% of LLTV on average; liquidators prevent 30-80% of insolvency only when the oracle sees the move",
            "Over-borrowing drains up to 100% of idle market liquidity once LLTV x oracle over-pricing > 1",
            "Liquidations in other markets holding the PT: 50-100% of the seized PT is sold into its Pendle AMM (constant-product impact); "
            "the dip is capped where the PT would yield 50-200% APY to maturity, as arbitrageurs buy it back",
            "Probabilities: Gamma-Poisson base rates x risk-factor and contagion multipliers, then Bayesian update by the alert likelihood ratio",
        ]
        if borrow:
            a += [
                "Borrow position: losses are a share of the equity; collateral and deployed-PT losses are levered by collateral / equity",
                "Liquidation when debt / (collateral at the oracle price) > LLTV; a liquidator repays 50-100% of the debt and seizes "
                "LIF = min(1.15, 1/(0.3 LLTV + 0.7)) times that in collateral, valued at the oracle price",
                "A cheaper loan asset makes the debt smaller; a Morpho Blue loss takes the collateral",
                "Ordinary price moves matter only through liquidation: probability from a driftless lognormal barrier over the horizon",
            ]
        return a + ex.get("assumptions", [])

    def run_watchlist(self, items: list[dict], horizon_days: float = 30) -> list[dict]:
        """Screen and record every position of a watchlist (dicts shaped like ``PRESETS``). One failure does not stop the rest."""
        out = []
        for it in items:
            try:
                req = request_from(it)
                h = float(it.get("horizon_days", horizon_days))
                if req.kind not in ("market", "token", "borrow") or not isinstance(req.address, str) or not req.address.strip():
                    raise ValueError("A token, market or borrow position with an address is required")
                if req.chain_id <= 0 or any(not math.isfinite(v) or v <= 0 for v in (req.size_usd, req.book_usd, h)) or h > 365:
                    raise ValueError("Positive finite size, book and chain, and horizon in (0,365] are required")
                a = self.analyze(req, h, record=True)
                m = a["monitor"]
                self.store.pop(a["position_id"], None)
                out.append({"ok": True, **{k: m[k] for k in ("label", "position_key", "verdict", "expected_loss_pct", "yield_pct",
                                                             "net_loss_pct", "exit_cost_pct", "max_alert", "regime")},
                            "events": m["events"]})
            except Exception as e:  # noqa: BLE001 - keep monitoring the other positions
                label = (it.get("label") or it.get("address") or "Invalid position") if isinstance(it, dict) else "Invalid position"
                out.append({"label": label, "ok": False, "error": f"{type(e).__name__}: {e}"})
        return out

    # ------------------------------------------------------------------ misc
    def node_modes(self, position_id: str, node_id: str, horizon_days: float = 30) -> list[dict]:
        entry = self.store[position_id]
        g: DependencyGraph = entry["graph"]
        node = g.nodes[node_id]
        rng = np.random.default_rng(self.seed)
        out = []
        for m in applicable_modes(node.type, node.attrs):
            lv = entry["levels"].get((node_id, m.id))
            mu = entry["mults"].get((node_id, m.id)) or {"multiplier": 1.0, "reasons": []}
            pr = failure_probability(node, m.id, horizon_days, rng, alert=lv["level"] if lv else "none", n=1000,
                                     rate_multiplier=mu["multiplier"], multiplier_reasons=mu["reasons"])
            out.append({"id": m.id, "label": m.label, "description": m.description, "p": pr["p"],
                        "annual_rate": pr["annual_rate"], "reasons": pr["reasons"], "alert": pr["alert"],
                        "signals": lv["signals"] if lv else []})
        return out
