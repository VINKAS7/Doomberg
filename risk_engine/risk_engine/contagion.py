"""Time-varying failure rates: a Hawkes-style self-exciting term on top of the static base rates.

Failures cluster: after one depeg or hack, related failures are more likely for a while. Each recent
event adds to the hazard of a component and decays exponentially:

    lambda(t) = lambda_0 x (1 + sum_i alpha_i x exp(-(t - t_i) / tau))

Events are failures recorded as outcomes, and episodes of alert-level signals seen by any monitored
position (from the run history), plus the alert-level signals of this run on other components.
alpha depends on how close the event is to the component: the same component, the same class
(e.g. another synthetic stablecoin depegging) or anything else. A component's own live signals are
not counted here, since they already move its probability through the alert likelihood ratio.
alpha, tau and the cap are judgement calls and are surfaced in each multiplier's reasons.
"""

from __future__ import annotations

import math
from typing import Protocol

from .graph import DependencyGraph
from .signals import LEVEL_RANK, Signal, alert_levels, node_class

DAY = 86400.0
TAU_DAYS = 30.0          # decay time: an event's effect halves in ~21 days
LOOKBACK_DAYS = 120.0    # ~4 tau: older events contribute < 2%
CAP = 5.0
ALPHA = {
    ("outcome", "same"): 3.0,    # this component failed recently
    ("outcome", "class"): 1.0,   # a component of the same class failed the same way
    ("outcome", "other"): 0.2,   # market-wide stress
    ("alert", "class"): 0.3,
    ("alert", "other"): 0.05,
}
RELATION_LABEL = {"same": "this component", "class": "same class", "other": "elsewhere"}


class EventHistory(Protocol):
    def alert_episodes(self, since: float, until: float) -> list[dict]: ...
    def outcomes(self, since: float | None = None) -> list[dict]: ...


def gather_events(g: DependencyGraph, signals: list[Signal], now: float, history: EventHistory | None) -> list[dict]:
    since = now - LOOKBACK_DAYS * DAY
    events: list[dict] = []
    if history is not None:
        events += [{**o, "kind": "outcome", "label": o.get("note") or o["node_id"]} for o in history.outcomes(since) if o["ts"] <= now]
        events += history.alert_episodes(since, now)
    # This run's alerts start an episode now, unless the history already has it running.
    running = {(e["node_id"], e["mode"]) for e in events if e["kind"] == "alert" and now - e.get("last", e["ts"]) <= 36 * 3600}
    for (_, mode), level in alert_levels(signals).items():
        if LEVEL_RANK[level["level"]] < LEVEL_RANK["alert"]:
            continue
        for s in level["signals"]:
            origin = s["origin_node_id"]
            if (origin, mode) in running:
                continue
            running.add((origin, mode))
            events.append({"ts": now, "node_id": origin, "node_class": node_class(g.nodes[origin]),
                           "label": g.nodes[origin].label, "mode": mode, "kind": "alert"})
    return events


def multipliers(g: DependencyGraph, keys: list[tuple[str, str]], events: list[dict], now: float) -> dict[tuple[str, str], dict]:
    """Hazard multiplier and its reasons for each (component, mode)."""
    out = {}
    for nid, mode in keys:
        cls = node_class(g.nodes[nid])
        parts = []
        for e in events:
            # A feed warning displayed on its oracle is the same failure, not contagion.
            if e["kind"] == "alert" and e["mode"] == mode and mode == "oracle_stale" and any(
                edge.type == "reads_feed" and edge.target == e["node_id"] for edge in g.out_edges(nid)
            ):
                continue
            if e["node_id"] == nid:
                rel = "same"
            elif e.get("node_class") == cls and e["mode"] == mode:
                rel = "class"
            else:
                rel = "other"
            alpha = ALPHA.get((e["kind"], rel))
            if not alpha:
                continue
            w = alpha * math.exp(-max(now - e["ts"], 0) / (TAU_DAYS * DAY))
            parts.append((w, e, rel))
        total = sum(w for w, _, _ in parts)
        mult = min(CAP, 1 + total)
        parts.sort(key=lambda x: -x[0])
        reasons = []
        if mult > 1.005:
            top = ", ".join(f"{e['label']} {e['mode'].replace('_', ' ')} "
                            f"{'failure' if e['kind'] == 'outcome' else 'alert'} {(now - e['ts']) / DAY:.0f}d ago "
                            f"({RELATION_LABEL[rel]}, +{w:.2f})" for w, e, rel in parts[:3])
            more = f" and {len(parts) - 3} more" if len(parts) > 3 else ""
            reasons.append(f"x{mult:.2f} contagion: {top}{more}" + (" (capped)" if mult >= CAP else ""))
        out[(nid, mode)] = {"multiplier": mult, "reasons": reasons, "events": len(parts)}
    return out


def regime(mults: dict[tuple[str, str], dict]) -> str:
    worst = max((m["multiplier"] for m in mults.values()), default=1.0)
    return "calm" if worst < 1.2 else "elevated" if worst < 2.0 else "stressed"
