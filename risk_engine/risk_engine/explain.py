"""Narrative layer: the statistics produce the numbers, the LLM explains which path causes the risk.

The LLM never computes anything. It receives the engine's structured output and is told to use only
those numbers. Without an API key a deterministic writer produces the same structure.

Environment:
  OPENAI_API_KEY   (+ optional OPENAI_MODEL, OPENAI_BASE_URL)   or
  ANTHROPIC_API_KEY (+ optional ANTHROPIC_MODEL)
"""

from __future__ import annotations

import datetime as dt
import json
import os

import httpx

SYSTEM = (
    "You are the on-chain risk analyst of Belem Capital, a market-neutral DeFi asset manager. "
    "A portfolio manager must decide STAY or EXIT on a position after a dependency failure. "
    "Use ONLY the numbers and facts in the JSON you receive; never invent figures, protocols or events. "
    "Write at most 200 words in Markdown with exactly these sections:\n"
    "**Verdict** - one sentence with the verdict and the two numbers that drive it (avoidable expected loss vs exit cost).\n"
    "**What is causing the risk** - walk the main dependency path hop by hop (position -> ... -> failing component) "
    "and say what each hop does to the money. Name the weakest link and why (use the risk factors / evidence).\n"
    "**Why these numbers** - probability (and its range), loss if it fails (typical vs bad case), exit cost and what limits the exit.\n"
    "**What would change the call** - the break-even probability and the one or two facts to monitor."
)


def _pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def _usd(x):
    return "n/a" if x is None else f"${x:,.0f}"


def build_context(result: dict) -> dict:
    d = result["decision"]
    return {
        "position": result["position"],
        "shock": result["shock"],
        "verdict": d["verdict"],
        "probability": d["probability"],
        "probability_range_90": result["probability"]["p_ci"],
        "probability_reasons": result["probability"]["reasons"],
        "loss_if_fails": d["loss_given_failure"],
        "expected_loss_usd": d["expected_loss_usd"],
        "avoidable_loss_usd": d["avoidable_loss_usd"],
        "exit_cost_pct": d["exit_cost_pct"],
        "exit_cost_usd": d["exit_cost_usd"],
        "exit_steps": [s["detail"] for s in result["exit"]["steps"]],
        "instant_exit_fraction": result["exit"]["instant_fraction"],
        "break_even_probability": d["break_even_probability"],
        "decision_reasons": d["reasons"],
        "main_paths": [
            [{"component": h["label"], "type": h["type"], "via": h["edge_type"], "mean_loss": round(h["mean_loss"], 4),
              "transmission": h["why"]} for h in p["hops"]]
            for p in result["paths"][:2]
        ],
        "component_findings": result.get("findings", []),
        "leverage": {k: result["leverage"][k] for k in ("structure", "leverage", "ltv", "lltv", "health_factor", "drop_to_liquidation",
                                                         "p_liquidation", "liquidation_incentive", "net_apy")}
                    if result.get("leverage") else None,
    }


def _llm(system: str, ctx: dict, max_tokens: int = 700) -> tuple[str | None, str, str | None, str | None]:
    """(text, provider, model, error) from the configured LLM, or (None, "template", None, error) without one."""
    model = None
    try:
        if os.getenv("OPENAI_API_KEY"):
            model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            base = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
            r = httpx.post(f"{base}/chat/completions", timeout=40,
                           headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                           json={"model": model, "temperature": 0.2,
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": json.dumps(ctx, default=float)}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"], "openai", model, None
        if os.getenv("ANTHROPIC_API_KEY"):
            model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
            r = httpx.post("https://api.anthropic.com/v1/messages", timeout=40,
                           headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                           json={"model": model, "max_tokens": max_tokens, "system": system,
                                 "messages": [{"role": "user", "content": json.dumps(ctx, default=float)}]})
            r.raise_for_status()
            return r.json()["content"][0]["text"], "anthropic", model, None
    except Exception as e:  # network / quota / model errors fall back to the template
        return None, "template", model, str(e)[:200]
    return None, "template", None, None


def explain(result: dict, use_llm: bool = True) -> dict:
    """Narrative for a shock result. ``use_llm=False`` skips the network call and returns the template."""
    ctx = build_context(result)
    text, provider, model, error = _llm(SYSTEM, ctx) if use_llm else (None, "template", None, None)
    if not text:
        text, provider = template(ctx), "template"
    return {"text": text, "provider": provider, "model": model, "error": error}


def template(ctx: dict) -> str:
    v, p = ctx["verdict"], ctx["probability"]
    lf = ctx["loss_if_fails"]
    path = ctx["main_paths"][0] if ctx["main_paths"] else []
    hops = " -> ".join(h["component"] for h in path) or ctx["shock"]["node_label"]
    weakest = max(path[1:], key=lambda h: h["mean_loss"], default=None) if path else None
    lines = [
        "#### Verdict",
        f"{v}: exiting can avoid {_usd(ctx['avoidable_loss_usd'])} of the {_usd(ctx['expected_loss_usd'])} expected loss "
        f"({_pct(p)} chance x {_pct(lf['mean'])} loss x {_pct(ctx['instant_exit_fraction'])} available to exit), "
        f"versus {_usd(ctx['exit_cost_usd'])} ({_pct(ctx['exit_cost_pct'])}) exit cost.",
        "",
        "#### What is causing the risk",
        f"Path: {hops}.",
    ]
    for h in path[1:]:
        lines.append(f"- {h['component']} ({h['type']}, via {h['via']}): mean loss {_pct(h['mean_loss'])} - {h['transmission']}.")
    if weakest:
        lines.append(f"Weakest link: **{ctx['shock']['node_label']}** - {ctx['shock']['mode_label'].lower()}.")
    for f in ctx.get("component_findings", [])[:3]:
        lines.append(f"- {f}")
    lines += [
        "",
        "#### Why these numbers",
        f"Probability {_pct(p)} (90% range {_pct(ctx['probability_range_90'][0])}-{_pct(ctx['probability_range_90'][1])}): "
        + "; ".join(ctx["probability_reasons"][:4]) + ".",
        f"Loss if it fails: typical {_pct(lf['typical'])}, bad case {_pct(lf['bad_case'])}.",
        f"Exit: {' / '.join(ctx['exit_steps'][:2])}.",
    ]
    lev = ctx.get("leverage")
    if lev:
        shape = (f"PT loop at {lev['leverage']:.2f}x (collateral per unit of equity)" if lev["structure"] == "loop" else
                 f"{_pct(lev['ltv'])} of the collateral's value borrowed and deployed into the PT")
        lines.append(
            f"Leverage: {shape}, LTV {_pct(lev['ltv'])} vs LLTV {_pct(lev['lltv'])} "
            f"(health factor {lev['health_factor']:.2f}). A {_pct(lev['drop_to_liquidation'])} drop in the oracle price triggers "
            f"liquidation with a {_pct(lev['liquidation_incentive'] - 1)} penalty; chance from ordinary price moves over the horizon "
            f"{_pct(lev['p_liquidation'])}. Net carry on equity {_pct(lev['net_apy'])} a year.")
    lines += [
        "",
        "#### What would change the call",
    ]
    be = ctx["break_even_probability"]
    if ctx['instant_exit_fraction'] <= 0:
        lines.append("No immediate complete exit route is available; check executable routing and liquidity before attempting an exit.")
    elif be is None or be >= 1:
        lines.append("Even a certain failure costs less than exiting; only a larger loss estimate would flip this.")
    else:
        lines.append(f"Exit becomes right above a {_pct(be)} failure probability. "
                     f"Watch the component's price / feed and the instant exit liquidity ({_pct(ctx['instant_exit_fraction'])} available now).")
    return "\n".join(lines)


# ---------------------------------------------------------------------- daily monitoring
MONITOR_SYSTEM = (
    "You are the on-chain risk analyst of Belem Capital, a market-neutral DeFi asset manager. "
    "You write the daily monitoring note for one position that is screened on a schedule. "
    "Use ONLY the numbers and facts in the JSON you receive; never invent figures, protocols or events, "
    "and never change the verdict. Percentages in the JSON are fractions (0.012 = 1.2%). "
    "Write at most 220 words in Markdown with exactly these sections:\n"
    "**Today's call** - the verdict and the rule behind it: (expected loss - yield) x withdrawable share vs exit cost, with the numbers. "
    "If the verdict is REVIEW, say which readings are missing.\n"
    "**What changed** - compare with the previous runs in `trend` (verdict, net avoidable loss, exit cost, alerts) "
    "and mention the notifications. Say plainly if nothing material changed.\n"
    "**Main risk drivers** - the two or three largest drivers, with their probability and any live signal.\n"
    "**What to watch** - how far the net avoidable loss is from the exit cost (the margin before the call flips) "
    "and the one or two signals to monitor."
)


def _iso(ts) -> str | None:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if ts else None


def build_monitor_context(latest: dict, runs: list[dict], events: list[dict]) -> dict:
    """``latest`` is a recorded run with its summary; ``runs`` the recent runs, oldest first; ``events`` newest first."""
    s = latest.get("summary") or {}
    net = lambda r: r["net_loss_pct"] if r.get("net_loss_pct") is not None else r.get("avoidable_loss_pct")
    drivers = sorted(s.get("rows", {}).values(), key=lambda r: -(r.get("el") or 0))[:5]
    return {
        "position": {k: latest.get(k) for k in ("label", "kind", "chain_id", "size_usd", "horizon_days")},
        "screened_at": _iso(latest.get("ts")),
        "verdict": latest["verdict"],
        "model_verdict": s.get("model_verdict"),
        "expected_loss_pct": latest.get("expected_loss_pct"),
        "baseline_loss_pct": latest.get("baseline_loss_pct"),
        "yield_pct": latest.get("yield_pct"),
        "withdrawable_share": latest.get("instant_fraction"),
        "net_avoidable_loss_pct": net(latest),
        "exit_cost_pct": latest.get("exit_cost_pct"),
        "p_any_loss_event": latest.get("p_any"),
        "max_alert": latest.get("max_alert"),
        "contagion_multiplier": latest.get("contagion"),
        "data_issues": [d.get("detail") for d in s.get("data_issues") or []],
        "drivers": [{"component": d["label"], "failure_mode": d["mode_label"], "probability": d["p"],
                     "expected_loss_pct": d.get("el"), "break_even_probability": d.get("break_even_p"),
                     "above_break_even": d.get("above"), "alert": d.get("alert"), "signals": (d.get("signals") or [])[:2]}
                    for d in drivers],
        "trend": [{"at": _iso(r["ts"]), "verdict": r["verdict"], "net_avoidable_loss_pct": net(r),
                   "exit_cost_pct": r.get("exit_cost_pct"), "expected_loss_pct": r.get("expected_loss_pct"),
                   "max_alert": r.get("max_alert")} for r in runs[-10:]],
        "notifications": [{"at": _iso(e["ts"]), "severity": e["severity"], "message": e["message"]} for e in events[:8]],
    }


def explain_monitor(latest: dict, runs: list[dict], events: list[dict], use_llm: bool = True) -> dict:
    """Narrative for the latest scheduled run of a position. The verdict and numbers come from the run."""
    ctx = build_monitor_context(latest, runs, events)
    text, provider, model, error = _llm(MONITOR_SYSTEM, ctx) if use_llm else (None, "template", None, None)
    if not text:
        text, provider = monitor_template(ctx), "template"
    return {"text": text, "provider": provider, "model": model, "error": error}


def monitor_template(ctx: dict) -> str:
    net, c, f = ctx["net_avoidable_loss_pct"] or 0.0, ctx["exit_cost_pct"] or 0.0, ctx["withdrawable_share"] or 0.0
    lines = ["#### Today's call",
             f"{ctx['verdict']} ({ctx['screened_at']}): ({_pct(ctx['expected_loss_pct'])} expected loss - "
             f"{_pct(ctx['yield_pct'] or 0)} yield) x {_pct(f)} withdrawable = {_pct(net)} net avoidable loss, "
             f"{'above' if net > c else 'at or below'} the {_pct(c)} exit cost."]
    if ctx["verdict"] == "REVIEW":
        lines.append(f"Required live readings are missing, so no actionable verdict is given (the model alone says {ctx['model_verdict']}).")
        lines += [f"- {d}" for d in ctx["data_issues"]]
    lines += ["", "#### What changed"]
    trend = ctx["trend"]
    if len(trend) > 1:
        a, b = trend[-2], trend[-1]
        d = (b["net_avoidable_loss_pct"] or 0) - (a["net_avoidable_loss_pct"] or 0)
        lines.append(f"Since {a['at']}: verdict {a['verdict']} -> {b['verdict']}, net avoidable loss "
                     f"{'+' if d >= 0 else ''}{d:.2%}, exit cost {_pct(a['exit_cost_pct'])} -> {_pct(b['exit_cost_pct'])}, "
                     f"alerts {a['max_alert']} -> {b['max_alert']}.")
    else:
        lines.append("This is the first recorded run; a comparison appears after the next one.")
    lines += [f"- [{e['severity']}] {e['message']}" for e in ctx["notifications"][:3]]
    lines += ["", "#### Main risk drivers"]
    for d in ctx["drivers"][:3]:
        lines.append(f"- {d['component']} · {d['failure_mode']}: {_pct(d['probability'])} chance, {d['expected_loss_pct'] or 0:.3%} expected loss"
                     + (f" · {d['alert']}" if d["alert"] and d["alert"] != "none" else "")
                     + (f" ({d['signals'][0]})" if d["signals"] else ""))
    lines += ["", "#### What to watch"]
    if f <= 0:
        lines.append("No part of the position can be exited immediately; check exit routes and liquidity first.")
    elif net > c:
        lines.append(f"The net avoidable loss exceeds the exit cost by {net - c:.2%} of the position.")
    else:
        lines.append(f"The call flips to EXIT if the net avoidable loss rises by {c - net:.2%} of the position, "
                     f"or the exit cost falls below {_pct(net)}.")
    sig = next((d for d in ctx["drivers"] if d["signals"]), None)
    if sig:
        lines.append(f"Watch {sig['component']}: {sig['signals'][0]}.")
    return "\n".join(lines)
