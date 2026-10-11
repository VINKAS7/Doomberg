"""Statistical failure-probability model.

1. Prior: failures of a component class arrive as a Poisson process with annual rate lambda.
   lambda ~ Gamma(k + 0.5, T) (Jeffreys posterior from k historical events over T component-years),
   scaled by multiplicative risk factors read from the graph (proportional-hazards style), and by a
   time-varying contagion multiplier from recent related events (``contagion.py``).
2. Horizon: P(failure within H days) = 1 - exp(-lambda * H / 365), sampled to give a credible band.
3. Alert update: when monitoring fires, posterior odds = prior odds x likelihood ratio of that alert.
   The alert level is set by the signal rules in ``signals.py``, or by hand for a what-if shock.
"""

from __future__ import annotations

import numpy as np

from .failures import MODES, hazard_prior
from .graph import Node

ALERT_LEVELS = {
    "none":         {"label": "No alert (base rate)", "lr_scale": None},
    "watch":        {"label": "Watchlist signal", "lr_scale": 0.25},
    "alert":        {"label": "Monitoring alert", "lr_scale": 1.0},
    "corroborated": {"label": "Alert + independent confirmation", "lr_scale": 5.0},
    "confirmed":    {"label": "Incident confirmed", "lr_scale": "confirmed"},
}


def failure_probability(node: Node, mode: str, horizon_days: float, rng: np.random.Generator,
                        alert: str = "none", n: int = 4000, rate_multiplier: float = 1.0,
                        multiplier_reasons: list[str] | None = None) -> dict:
    shape, rate, reasons = hazard_prior(mode, node.type, node.attrs, node.risk_factors)
    reasons = reasons + list(multiplier_reasons or [])
    lam_base = rng.gamma(shape, 1.0 / rate, n)
    lam = lam_base * rate_multiplier          # same draws, so p - base_p is the effect of the multiplier, not noise
    base_p = float((1.0 - np.exp(-lam_base * horizon_days / 365.0)).mean())
    p = 1.0 - np.exp(-lam * horizon_days / 365.0)
    prior_mean = float(p.mean())
    level = ALERT_LEVELS.get(alert, ALERT_LEVELS["none"])
    lr = None
    if level["lr_scale"] == "confirmed":
        p = rng.uniform(0.85, 0.99, n)
        reasons = reasons + ["incident confirmed: probability set to 85-99%"]
    elif level["lr_scale"]:
        lr = MODES[mode].alert_lr * level["lr_scale"]
        odds = p / np.clip(1 - p, 1e-12, None) * lr
        p = odds / (1 + odds)
        reasons = reasons + [f"alert likelihood ratio x{lr:g} ({level['label']})"]
    return {
        "annual_rate": shape / rate,
        "annual_rate_ci": [float(np.quantile(lam, 0.05)), float(np.quantile(lam, 0.95))],
        "base_p": base_p,
        "prior_p": prior_mean,
        "p": float(p.mean()),
        "p_ci": [float(np.quantile(p, 0.05)), float(np.quantile(p, 0.95))],
        "likelihood_ratio": lr,
        "alert": alert,
        "rate_multiplier": rate_multiplier,
        "horizon_days": horizon_days,
        "reasons": reasons,
        "samples": p,
    }
