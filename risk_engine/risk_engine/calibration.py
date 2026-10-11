"""Calibration: were the stored forecasts right?

A forecast "p that (component, mode) fails within H days", made at time t, matures at t + H. It
counts as a hit if an outcome for that component and mode was recorded in [t, t + H]. Repeated
screenings of the same position on the same day are one forecast (the first of the day), so the
UI re-analysing a position does not inflate the sample.

The report gives the Brier score against climatology, a reliability table (do 5% forecasts fail
about 5% of the time?), and per alert level the empirical likelihood ratio
P(level | failure) / P(level | no failure). Once enough outcomes exist, that is the data to
replace the hand-set ``alert_lr`` values in ``failures.py``.
"""

from __future__ import annotations

import math
from collections import defaultdict

DAY = 86400.0
BINS = (0.0, 0.001, 0.005, 0.01, 0.02, 0.05, 0.10, 0.20, 1.0)
MIN_EVENTS = 10


def _jeffreys(k: int, n: int) -> list[float]:
    """90% Jeffreys interval for a binomial rate, via a normal approximation on the Beta(k+.5, n-k+.5)."""
    a, b = k + 0.5, n - k + 0.5
    mean = a / (a + b)
    sd = math.sqrt(a * b / ((a + b) ** 2 * (a + b + 1)))
    return [max(0.0, mean - 1.645 * sd), min(1.0, mean + 1.645 * sd)]


def matured(forecasts: list[dict], outcomes: list[dict], now: float) -> list[dict]:
    hits: dict[tuple[str, str], list[float]] = defaultdict(list)
    for o in outcomes:
        hits[(o["node_id"], o["mode"])].append(o["ts"])
    seen, out = set(), []
    for f in sorted(forecasts, key=lambda f: f["ts"]):
        end = f["ts"] + f["horizon_days"] * DAY
        if end > now:
            continue
        day = (f["position_key"], f["node_id"], f["mode"], int(f["ts"] // DAY))
        if day in seen:
            continue
        seen.add(day)
        y = any(f["ts"] <= t <= end for t in hits.get((f["node_id"], f["mode"]), ()))
        out.append({**f, "y": int(y)})
    return out


def report(forecasts: list[dict], outcomes: list[dict], now: float) -> dict:
    daily_rows = matured(forecasts, outcomes, now)
    # A component's overlapping horizons share outcomes, even in different positions.
    # Use disjoint windows per component/mode for scores and binomial intervals.
    rows, last_end = [], {}
    for r in daily_rows:
        key = (r["node_id"], r["mode"])
        if r["ts"] <= last_end.get(key, -math.inf):
            continue
        rows.append(r)
        last_end[key] = r["ts"] + r["horizon_days"] * DAY
    n = len(rows)
    pending = sum(1 for f in forecasts if f["ts"] + f["horizon_days"] * DAY > now)
    if not n:
        return {"n": 0, "daily_forecasts": len(daily_rows), "pending": pending, "events": 0,
                "positive_forecasts": 0, "note": "No matured forecasts yet: a forecast matures after its horizon."}
    k = sum(r["y"] for r in rows)
    incidents = {(o["node_id"], o["mode"], o["ts"]) for o in outcomes if any(
        r["node_id"] == o["node_id"] and r["mode"] == o["mode"] and
        r["ts"] <= o["ts"] <= r["ts"] + r["horizon_days"] * DAY for r in rows
    )}
    event_count = len(incidents)
    eps = 1e-6
    brier = sum((r["p"] - r["y"]) ** 2 for r in rows) / n
    base = k / n
    brier_clim = sum((base - r["y"]) ** 2 for r in rows) / n
    logloss = -sum(r["y"] * math.log(max(r["p"], eps)) + (1 - r["y"]) * math.log(max(1 - r["p"], eps)) for r in rows) / n

    reliability = []
    for lo, hi in zip(BINS, BINS[1:]):
        b = [r for r in rows if lo <= r["p"] < hi or (hi == 1.0 and r["p"] == 1.0)]
        if not b:
            continue
        kb = sum(r["y"] for r in b)
        reliability.append({"bin": [lo, hi], "n": len(b), "mean_p": sum(r["p"] for r in b) / len(b),
                            "observed": kb / len(b), "observed_ci": _jeffreys(kb, len(b)), "events": kb})

    by_alert, fails, oks = [], k, n - k
    for level in ("none", "watch", "alert", "corroborated", "confirmed"):
        b = [r for r in rows if (r.get("alert") or "none") == level]
        if not b:
            continue
        kb = sum(r["y"] for r in b)
        # Jeffreys-smoothed P(level | failure) / P(level | no failure).
        lr = ((kb + 0.5) / (fails + 1)) / ((len(b) - kb + 0.5) / (oks + 1))
        assumed = [r["likelihood_ratio"] for r in b if r.get("likelihood_ratio")]
        by_alert.append({"alert": level, "n": len(b), "events": kb, "mean_p": sum(r["p"] for r in b) / len(b),
                         "observed": kb / len(b), "empirical_lr": lr,
                         "assumed_lr": sum(assumed) / len(assumed) if assumed else None})

    by_mode = defaultdict(lambda: [0, 0, 0.0])
    for r in rows:
        m = by_mode[r["mode"]]
        m[0] += 1
        m[1] += r["y"]
        m[2] += r["p"]
    return {
        "n": n, "daily_forecasts": len(daily_rows), "events": event_count, "positive_forecasts": k,
        "sampling": "nonoverlapping_component_windows", "pending": pending, "brier": brier, "brier_climatology": brier_clim,
        "brier_skill": 1 - brier / brier_clim if brier_clim > 0 else None, "log_loss": logloss,
        "mean_p": sum(r["p"] for r in rows) / n, "observed_rate": base,
        "reliability": reliability, "by_alert": by_alert,
        "by_mode": [{"mode": m, "n": v[0], "events": v[1], "mean_p": v[2] / v[0], "observed": v[1] / v[0]} for m, v in sorted(by_mode.items())],
        "note": (f"Only {event_count} distinct incidents: too few to judge calibration (want >= {MIN_EVENTS}). "
                 if event_count < MIN_EVENTS else "") +
                "Scores use non-overlapping windows per component/mode, shared positions are deduplicated. "
                "Different components may still be correlated. Outcomes must be recorded comprehensively; absence of a record is treated as no failure.",
    }
