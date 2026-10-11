"""Notifications: compare a screening with the previous one and say what changed.

Raised when the verdict flips, when a component's failure probability crosses its break-even (its
cost divided by the loss and withdrawable share), when an alert level rises, and when the expected loss
doubles. Events are stored in the run history and, when ``ALERT_WEBHOOK_URL`` is set, posted as
``{"text": ...}`` (the Slack / Mattermost incoming-webhook format).
"""

from __future__ import annotations

import logging
import os

import httpx

from .signals import LEVEL_RANK

log = logging.getLogger("risk_engine.notify")
SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


def _row_key(r: dict) -> str:
    return f"{r['node_id']}|{r['mode']}"


def _net(run: dict) -> float:
    """The quantity the verdict compares with the exit cost; runs recorded before the yield credit lack it."""
    net = run.get("net_loss_pct")
    return run["avoidable_loss_pct"] if net is None else net


def diff(prev: dict | None, cur: dict, now: float) -> list[dict]:
    """Events between the previous run (``History.last_run``) and the current run dict."""
    base = {"ts": now, "position_key": cur["position_key"], "label": cur.get("label")}
    rows, prev_rows = cur["summary"]["rows"], (prev or {}).get("summary", {}).get("rows", {})
    ev: list[dict] = []

    def add(kind: str, severity: str, message: str, **data):
        ev.append({**base, "kind": kind, "severity": severity, "message": message, "data": data})

    if prev is None:
        if cur["verdict"] == "EXIT":
            add("verdict", "critical", f"First screening says EXIT: avoidable expected loss net of yield {_net(cur):.2%} "
                                       f"> exit cost {cur['exit_cost_pct']:.2%}")
    elif prev["verdict"] != cur["verdict"]:
        add("verdict", "critical" if cur["verdict"] == "EXIT" else "warning" if cur["verdict"] == "REVIEW" else "info",
            f"Verdict flipped {prev['verdict']} -> {cur['verdict']}: avoidable expected loss net of yield {_net(cur):.2%} "
            f"vs exit cost {cur['exit_cost_pct']:.2%} (was {_net(prev):.2%} vs {prev['exit_cost_pct']:.2%})",
            before=prev["verdict"], after=cur["verdict"])

    missing = cur.get("summary", {}).get("data_issues", [])
    prev_missing = (prev or {}).get("summary", {}).get("data_issues", [])
    if missing != prev_missing:
        add("data_quality", "warning" if missing else "info",
            "Monitoring data incomplete: " + "; ".join(i["detail"] for i in missing) if missing else "Monitoring data restored",
            issues=missing)

    for k, r in rows.items():
        p0 = prev_rows.get(k)
        name = f"{r['label']} / {r['mode_label']}"
        if not missing and r["above"] and not (p0 and p0["above"]):
            add("break_even", "warning", f"{name}: probability {r['p']:.2%} crossed break-even {r['break_even_p']:.2%}"
                + (f" (was {p0['p']:.2%})" if p0 else ""), key=k, p=r["p"], break_even=r["break_even_p"])
        elif p0 and p0["above"] and not r["above"]:
            add("break_even", "info", f"{name}: probability {r['p']:.2%} back below break-even", key=k, p=r["p"])
        lv0 = (p0 or {}).get("alert", "none")
        if LEVEL_RANK[r["alert"]] > LEVEL_RANK[lv0]:
            add("alert", "warning" if LEVEL_RANK[r["alert"]] >= LEVEL_RANK["alert"] else "info",
                f"{name}: alert level {lv0} -> {r['alert']} ({'; '.join(r.get('signals') or [])})", key=k, before=lv0, after=r["alert"])
        elif p0 and LEVEL_RANK[r["alert"]] < LEVEL_RANK[lv0]:
            add("alert", "info", f"{name}: alert level {lv0} -> {r['alert']}", key=k, before=lv0, after=r["alert"])

    if prev and prev["expected_loss_pct"] > 0:
        ratio = cur["expected_loss_pct"] / prev["expected_loss_pct"]
        if ratio >= 2 and cur["expected_loss_pct"] - prev["expected_loss_pct"] > 0.0005:
            add("trend", "warning", f"Expected loss x{ratio:.1f} since the last run: {prev['expected_loss_pct']:.2%} -> "
                                    f"{cur['expected_loss_pct']:.2%}", ratio=ratio)
    return ev


def deliver(events: list[dict], min_severity: str = "warning") -> int:
    """Log every event; post the important ones to the webhook. Returns how many were posted."""
    for e in events:
        log.log(logging.WARNING if SEVERITY_RANK[e["severity"]] else logging.INFO, "[%s] %s: %s", e["severity"], e.get("label"), e["message"])
    url = os.getenv("ALERT_WEBHOOK_URL")
    send = [e for e in events if SEVERITY_RANK[e["severity"]] >= SEVERITY_RANK[min_severity]]
    if not url or not send:
        return 0
    text = "\n".join(f"*{e['severity'].upper()}* {e.get('label') or e['position_key']}: {e['message']}" for e in send)
    try:
        httpx.post(url, json={"text": text}, timeout=10).raise_for_status()
    except httpx.HTTPError as err:
        log.error("webhook delivery failed: %s", err)
        return 0
    return len(send)
