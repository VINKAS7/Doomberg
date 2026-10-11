"""Monitoring signals: turn what Discovery reads right now into an alert level per (component, failure mode).

The alert level feeds the likelihood-ratio update in ``probability.py``, so the probability reacts to
news without anyone typing it in. Two kinds of rule:

- **Level rules** on live data: peg deviation, oracle vs market price, stale feeds, utilization,
  bad debt, the collateral's 24h price move.
- **Change rules** that need the run history (``history.py``): supply or AMM liquidity withdrawn in
  24h, a token's 24h price drop, a jump in the PT's implied APY.

Rules in the same family read the same thing (a PT's price drop and its implied-APY jump are one
move seen twice), so only distinct families count as independent. Per (component, mode): a watch
-> watch; an alert, or watches from two families -> alert; two families with at least one alert
-> corroborated. "Confirmed" is never set
automatically: a person confirms an incident. Thresholds are judgement calls and are listed in
``RULES`` so each signal shows the threshold it crossed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Protocol

from .failures import asset_vol
from .graph import DependencyGraph, Node

LEVEL_RANK = {"none": 0, "watch": 1, "alert": 2, "corroborated": 3, "confirmed": 4}
DAY = 86400.0

RULES: dict[str, dict] = {
    "peg_deviation":      {"family": "price", "watch": 0.005, "alert": 0.02, "label": "Stablecoin off its peg"},
    "price_drop":         {"family": "price", "watch": 0.02, "alert": 0.05, "label": "Token price fell over 24h"},
    "collateral_drop":    {"family": "price", "watch": 0.02, "alert": 0.05, "label": "Collateral price fell over 24h"},
    "implied_apy_jump":   {"family": "price", "watch": 0.03, "alert": 0.08, "label": "PT implied APY jumped over 24h"},
    "oracle_dex_gap":     {"family": "oracle", "watch": 0.02, "alert": 0.05, "label": "Oracle prices the collateral above the market"},
    "feed_stale":         {"family": "feed", "alert": 1.0, "label": "Price feed not updated within its heartbeat"},
    "utilization_pinned": {"family": "liquidity", "watch": 0.97, "alert": 0.995, "label": "Market utilization pinned near 100%"},
    "supply_outflow":     {"family": "liquidity", "watch": 0.10, "alert": 0.25, "label": "Market supply withdrawn over 24h"},
    "amm_outflow":        {"family": "liquidity", "watch": 0.20, "alert": 0.40, "label": "AMM liquidity withdrawn over 24h"},
    "bad_debt":           {"family": "solvency", "watch": 0.001, "alert": 0.01, "label": "Unrealized bad debt (share of supply)"},
}

# A 24h price drop is news for a stable or yield token, but ordinary volatility for ETH or BTC.
MAX_VOL_FOR_PRICE_RULE = 0.25


class MetricHistory(Protocol):
    def window(self, node_id: str, metric: str, start: float, end: float) -> list[float]: ...
    def signal_first_seen(self, node_id: str, mode: str, rule: str, now: float) -> float | None: ...


@dataclass
class Signal:
    node_id: str
    label: str
    mode: str
    rule: str
    level: str          # watch | alert
    value: float
    threshold: float
    detail: str
    first_seen: float | None = None
    origin_node_id: str | None = None

    def to_dict(self) -> dict:
        return {**asdict(self), "origin_node_id": self.origin_node_id or self.node_id,
                "rule_label": RULES[self.rule]["label"]}


def data_issues(g: DependencyGraph) -> list[dict]:
    """Missing readings are unknown, not evidence that a monitored check passed."""
    def valid(v, positive=False):
        return isinstance(v, (int, float)) and math.isfinite(v) and (v > 0 if positive else v >= 0)
    issues = []
    for node in g.nodes.values():
        a = node.attrs
        checks = {}
        if node.type == "feed":
            checks = {"answer": valid(a.get("answer"), True), "age_s": valid(a.get("age_s"))}
        elif node.type == "asset":
            checks = {"price_usd": valid(a.get("price_usd"), True)}
        elif node.type == "market" and a.get("role") == "collateral_venue":
            # The inputs that size the liquidations sold into the PT's AMM.
            checks = {k: valid(a.get(k)) for k in ("collateral_usd", "borrow_usd")}
        elif node.type == "market" and a.get("role") == "borrow_market":
            # The inputs that set the borrower's liquidation distance and carry.
            checks = {"lltv": valid(a.get("lltv"), True), "borrow_apy": valid(a.get("borrow_apy")),
                      "oracle_price": valid(a.get("oracle_price"), True)}
        elif node.type == "market":
            checks = {k: valid(a.get(k)) for k in ("utilization", "supply_usd", "liquidity_usd")}
            checks["utilization"] = checks["utilization"] and a["utilization"] <= 1
        elif node.type == "amm":
            checks = {"liquidity_usd": valid(a.get("liquidity_usd"))}
        missing = [k for k, ok in checks.items() if not ok]
        if missing:
            issues.append({"node_id": node.id, "label": node.label, "fields": missing,
                           "detail": f"{node.label}: missing or invalid {', '.join(missing)}"})
    return issues


def _grade(rule: str, value: float | None) -> tuple[str, float] | None:
    """Level reached by ``value`` on ``rule``, with the threshold it crossed."""
    if value is None or not math.isfinite(value):
        return None
    r = RULES[rule]
    if "alert" in r and value >= r["alert"]:
        return "alert", r["alert"]
    if "watch" in r and value >= r["watch"]:
        return "watch", r["watch"]
    return None


def node_class(node: Node) -> str:
    """Components that fail together: assets by kind, everything else by type."""
    return f"asset:{node.attrs.get('kind', 'unknown')}" if node.type == "asset" else node.type


def observe(g: DependencyGraph) -> list[tuple[str, str, float]]:
    """Metrics worth storing each run, so the next run can compute 24h changes."""
    out = []
    keep = {"market": ("supply_usd", "liquidity_usd", "utilization", "oracle_dex_gap"),
            "amm": ("liquidity_usd", "implied_apy"), "asset": ("price_usd",)}
    for n in g.nodes.values():
        for k in keep.get(n.type, ()):
            v = n.attrs.get(k)
            if isinstance(v, (int, float)) and math.isfinite(v):
                out.append((n.id, k, float(v)))
        if n.type == "asset" and n.risk_factors.get("price_deviation") is not None:
            out.append((n.id, "peg_deviation", float(n.risk_factors["price_deviation"])))
    return out


def detect(g: DependencyGraph, now: float, history: MetricHistory | None = None) -> list[Signal]:
    sig: list[Signal] = []

    def fire(node: Node, mode: str, rule: str, value: float | None, detail: str, origin: str | None = None):
        hit = _grade(rule, value)
        if hit:
            sig.append(Signal(node.id, node.label, mode, rule, hit[0], float(value), hit[1], detail,
                              origin_node_id=origin or node.id))

    def change(node_id: str, metric: str, now_value, worst) -> float | None:
        """Worst value of ``metric`` over the previous 24h, or None without history."""
        if history is None or not isinstance(now_value, (int, float)):
            return None
        past = history.window(node_id, metric, now - DAY, now - 60)
        return worst(past) if past else None

    for n in g.nodes.values():
        a, rf = n.attrs, n.risk_factors
        if n.type == "asset":
            dev = rf.get("price_deviation")
            fire(n, "depeg", "peg_deviation", dev, f"{n.label} trades {(dev or 0):.2%} away from its peg")
            price = a.get("price_usd")
            if asset_vol(a.get("kind", "unknown")) <= MAX_VOL_FOR_PRICE_RULE:
                peak = change(n.id, "price_usd", price, max)
                if peak:
                    drop = 1 - price / peak
                    fire(n, "depeg", "price_drop", drop, f"{n.label} at ${price:,.4f}, down {drop:.2%} from its 24h high ${peak:,.4f}")
        elif n.type == "feed" and a.get("stale"):
            hours = (a.get("age_s") or 0) / 3600
            detail = f"Feed {n.label} last updated {hours:,.0f}h ago"
            fire(n, "oracle_stale", "feed_stale", 1.0, detail)
            for e in g.in_edges(n.id):
                if e.type == "reads_feed":
                    fire(g.nodes[e.source], "oracle_stale", "feed_stale", 1.0, detail, origin=n.id)
        elif n.type == "oracle":
            gap = a.get("oracle_dex_gap")
            if gap is not None:
                fire(n, "oracle_wrong_price", "oracle_dex_gap", gap,
                     f"{n.label} reads {a.get('oracle_price'):,.4f} vs market {a.get('market_price'):,.4f} ({gap:+.2%})")
        elif n.type == "market":
            dpv = a.get("daily_price_variation")
            coll = next((g.nodes[e.target] for e in g.out_edges(n.id) if e.type == "collateral_in"), None)
            if coll is None and a.get("role") == "collateral_venue":
                coll = next((g.nodes[e.source] for e in g.in_edges(n.id) if e.type == "liquidated_in"), None)
            if coll is not None and dpv is not None:
                fire(coll, "depeg", "collateral_drop", -dpv, f"{coll.label} oracle price moved {dpv:+.2%} over 24h ({n.label})")
            if a.get("role") == "collateral_venue":
                continue  # its lenders' liquidity is not a failure mode of the PT holder
            util = a.get("utilization")
            fire(n, "liquidity_drain", "utilization_pinned", util, f"{n.label} utilization {(util or 0):.1%}")
            supply = a.get("supply_usd")
            bad = a.get("bad_debt_usd")
            if bad and supply:
                fire(n, "liquidity_drain", "bad_debt", bad / supply, f"{n.label} carries ${bad:,.0f} unrealized bad debt")
            peak = change(n.id, "supply_usd", supply, max)
            if peak:
                out = 1 - supply / peak
                fire(n, "liquidity_drain", "supply_outflow", out,
                     f"{n.label} supply ${supply / 1e6:,.1f}M, {out:.0%} below its 24h high ${peak / 1e6:,.1f}M")
        elif n.type == "amm":
            liq = a.get("liquidity_usd")
            peak = change(n.id, "liquidity_usd", liq, max)
            if peak:
                out = 1 - liq / peak
                fire(n, "liquidity_drain", "amm_outflow", out,
                     f"{n.label} liquidity ${liq / 1e6:,.1f}M, {out:.0%} below its 24h high ${peak / 1e6:,.1f}M")
            apy = a.get("implied_apy")
            low = change(n.id, "implied_apy", apy, min)
            if low is not None:
                for e in g.in_edges(n.id):
                    if e.type == "traded_on":
                        pt = g.nodes[e.source]
                        fire(pt, "depeg", "implied_apy_jump", apy - low,
                             f"{pt.label} implied APY {apy:.2%}, up {apy - low:.2%} from its 24h low: the market is selling the PT")

    if history is not None:
        for s in sig:
            s.first_seen = history.signal_first_seen(s.node_id, s.mode, s.rule, now)
    return sig


def alert_levels(signals: list[Signal]) -> dict[tuple[str, str], dict]:
    """Combine the signals on each (component, mode) into one alert level."""
    grouped: dict[tuple[str, str], dict[str, Signal]] = {}
    for s in signals:
        rules = grouped.setdefault((s.node_id, s.mode), {})
        rule_key = f"{s.rule}|{s.origin_node_id or s.node_id}"
        if rule_key not in rules or LEVEL_RANK[s.level] > LEVEL_RANK[rules[rule_key].level]:
            rules[rule_key] = s
    out = {}
    for key, rules in grouped.items():
        families: dict[str, str] = {}
        for s in rules.values():
            f = RULES[s.rule]["family"]
            if LEVEL_RANK[s.level] > LEVEL_RANK[families.get(f, "none")]:
                families[f] = s.level
        n_alert = sum(lv == "alert" for lv in families.values())
        if len(families) >= 2 and n_alert:
            level = "corroborated"
        elif n_alert or len(families) >= 2:
            level = "alert"
        else:
            level = "watch"
        out[key] = {"level": level, "signals": [s.to_dict() for s in rules.values()]}
    return out


def max_level(levels: dict[tuple[str, str], dict]) -> str:
    return max((v["level"] for v in levels.values()), key=LEVEL_RANK.__getitem__, default="none")
