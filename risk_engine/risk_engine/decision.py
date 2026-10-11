"""Stage 4 - Decide.  Exit if  P(failure) x E[loss | failure] x share you can get out  >  cost of exiting.

Only the instantly withdrawable share of a market supply escapes the loss by exiting; the rest stays
exposed either way, so it does not count toward the benefit of exiting. A held PT is fully sellable.
"""

from __future__ import annotations

import numpy as np


def decide(size: float, book: float, p_samples: np.ndarray, loss_samples: np.ndarray, exit_cost_pct: float,
           exit_cost_samples: np.ndarray | None, instant_fraction: float, probability_override: float | None = None) -> dict:
    lgf = float(loss_samples.mean())                        # expected loss if it fails
    typical = float(np.median(loss_samples))
    bad = float(np.quantile(loss_samples, 0.95))
    p = float(probability_override) if probability_override is not None else float(p_samples.mean())
    el_pct = p * lgf
    f = min(max(instant_fraction, 0.0), 1.0)
    avoidable_pct = el_pct * f
    c = exit_cost_pct
    break_even = c / (lgf * f) if lgf * f > 1e-9 else None
    exit_ = avoidable_pct > c

    if probability_override is not None:
        confidence = 1.0 if (break_even is None or abs(p - break_even) > 0.05) else 0.5 + abs(p - break_even) * 10
    else:
        costs = exit_cost_samples if exit_cost_samples is not None else np.full_like(p_samples, c)
        n = min(len(p_samples), len(costs))
        stay_loss = p_samples[:n] * lgf * f
        confidence = float(((stay_loss > costs[:n]) == exit_).mean())

    reasons = []
    if f == 0:
        reasons.append("No immediate complete exit is available; this route cannot avoid the position's loss")
    elif break_even is None or break_even >= 1:
        reasons.append("Exiting costs more than the loss even if the failure is certain")
    else:
        reasons.append(f"Exit pays off if the failure is more likely than {break_even:.1%}; the model estimates {p:.1%}")
    if f < 0.999:
        reasons.append(f"Only {f:.0%} of the position can be withdrawn now; the rest stays exposed until liquidity returns, "
                       f"so exiting avoids {avoidable_pct:.2%} of the {el_pct:.2%} expected loss")
    if bad > 2 * max(lgf, 1e-9) and bad > 0.02:
        reasons.append(f"Fat tail: the bad case ({bad:.0%}) is far worse than the average ({lgf:.0%})")

    return {
        "verdict": "EXIT" if exit_ else "STAY",
        "confidence": float(min(1.0, max(0.0, confidence))),
        "probability": p,
        "probability_source": "override" if probability_override is not None else "model",
        "loss_given_failure": {"mean": lgf, "typical": typical, "bad_case": bad},
        "expected_loss_pct": el_pct,
        "expected_loss_usd": el_pct * size,
        "bad_case_usd": bad * size,
        "exit_cost_pct": c,
        "exit_cost_usd": c * size,
        "break_even_probability": break_even,
        "instant_fraction": f,
        "avoidable_loss_pct": avoidable_pct,
        "avoidable_loss_usd": avoidable_pct * size,
        "book_impact_pct": el_pct * size / book if book else None,
        "position_share_of_book": size / book if book else None,
        "reasons": reasons,
        "break_even_table": [
            {"loss_if_fails": lf, "exit_above": (c / (lf * f) if lf * f > c else None)}
            for lf in (1.0, 0.5, 0.25, 0.10)
        ],
    }
