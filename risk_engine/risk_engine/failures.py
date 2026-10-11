"""Failure-mode catalog: which node types can fail how, with priors for frequency and severity.

Frequency priors are expressed as (k events, T component-years) from the hand-compiled incident
record in ``data/incidents.json`` and public post-mortems. They feed a Gamma-Poisson model in
``probability.py``. Severity priors are Beta distributions (mean, concentration) over the fraction
of value lost at the failing component. All of these are judgement calls and are surfaced as
assumptions in every result.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FailureMode:
    id: str
    label: str
    applies_to: tuple[str, ...]
    description: str
    alert_lr: float        # likelihood ratio of a monitoring alert for this failure


MODES: dict[str, FailureMode] = {m.id: m for m in [
    FailureMode("oracle_wrong_price", "Oracle shows a wrong price", ("oracle", "feed"),
                "Feed is manipulated or misconfigured and reports a price above the real one.", 40),
    FailureMode("oracle_stale", "Oracle stops updating (stale)", ("oracle", "feed"),
                "Feed freezes while the market moves; liquidations stop working.", 40),
    FailureMode("depeg", "Token loses its peg / backing value", ("asset",),
                "The token trades below what it should be worth.", 20),
    FailureMode("unbacked_mint", "Token minted without backing", ("asset", "mint_authority"),
                "New units appear with nothing behind them (bug, stolen key, tricked bridge).", 50),
    FailureMode("issuer_default", "Issuer / custodian fails or freezes redemptions", ("issuer",),
                "The off-chain entity behind the token cannot honour redemptions.", 15),
    FailureMode("bridge_hack", "Bridge hacked or frozen", ("bridge",),
                "Locked collateral is stolen or the bridge issues copies with nothing locked.", 30),
    # Morpho markets share the protocol code, so code risk lives on the protocol node.
    FailureMode("exploit", "Smart-contract exploit", ("protocol", "amm"),
                "A bug is abused to take funds from the contract.", 25),
    FailureMode("chain_halt", "Chain halts or is congested", ("chain",),
                "Blocks stop; nobody can liquidate, repay, withdraw or sell.", 50),
    FailureMode("liquidity_drain", "Liquidity drains (bank run)", ("market", "amm"),
                "Everyone withdraws at once; what is left is slow or costly to exit.", 10),
]}

# Asset classes used for depeg priors. (k, T) -> annual hazard, (mean, kappa) -> severity Beta.
DEPEG_PRIORS: dict[str, dict] = {
    "native_wrapped":   {"k": 0, "T": 60, "sev": (0.02, 40), "vol": 0.70},
    "fiat_stable":      {"k": 3, "T": 45, "sev": (0.06, 12), "vol": 0.02},
    "eur_stable":       {"k": 1, "T": 20, "sev": (0.05, 12), "vol": 0.03},
    "yield_stable":     {"k": 8, "T": 40, "sev": (0.35, 3), "vol": 0.06},
    "synthetic_stable": {"k": 8, "T": 40, "sev": (0.35, 3), "vol": 0.06},
    "rwa":              {"k": 2, "T": 30, "sev": (0.25, 4), "vol": 0.04},
    "lst":              {"k": 3, "T": 30, "sev": (0.08, 8), "vol": 0.70},
    "lrt":              {"k": 4, "T": 20, "sev": (0.15, 5), "vol": 0.75},
    "wrapped_btc":      {"k": 2, "T": 25, "sev": (0.10, 5), "vol": 0.55},
    "gold":             {"k": 0, "T": 15, "sev": (0.05, 10), "vol": 0.18},
    "pt":               {"k": 2, "T": 40, "sev": (0.10, 6), "vol": 0.08},
    # Own-risk of a share/staked wrapper (sUSDe, wstETH): discount to its underlying during a cooldown or
    # redemption queue. The underlying's depeg is its own node and reaches the wrapper through backed_by.
    "wrapper":          {"k": 2, "T": 40, "sev": (0.10, 6), "vol": 0.08},
    "volatile":         {"k": 4, "T": 30, "sev": (0.30, 3), "vol": 0.90},
    "unknown":          {"k": 6, "T": 40, "sev": (0.30, 3), "vol": 0.30},
}

BASE: dict[str, dict] = {
    "oracle_wrong_price":    {"k": 6, "T": 1500, "sev": (0.35, 3)},
    "oracle_stale":          {"k": 4, "T": 1500, "sev": (24.0, 0)},       # hours frozen (exponential mean)
    "unbacked_mint":         {"k": 5, "T": 300, "sev": (0.60, 2.5)},
    "issuer_default":        {"k": 2, "T": 60, "sev": (0.40, 3)},
    "bridge_hack":           {"k": 10, "T": 150, "sev": (0.70, 3)},
    "exploit":               {"k": 40, "T": 1000, "sev": (0.45, 2.5)},
    "chain_halt":            {"k": 2, "T": 40, "sev": (4.0, 0)},          # hours halted (exponential mean)
    "liquidity_drain":       {"k": 5, "T": 100, "sev": (0.08, 6)},
}

CHAIN_HALT = {  # (k, T, mean hours)
    "ethereum": (0, 40, 2.0),
    "l2_centralised_sequencer": (2, 6, 1.5),
    "young_l1": (1, 2, 6.0),
    "l1": (2, 20, 5.0),
}


def applicable_modes(node_type: str, node_attrs: dict | None = None) -> list[FailureMode]:
    attrs = node_attrs or {}
    if node_type == "market" and attrs.get("role") in ("collateral_venue", "borrow_market"):
        # A run on another market's lenders does not hurt a PT holder; that market reaches the PT
        # through its oracle (wrong or stale price) and the liquidations it triggers. A borrower is hit
        # the same way: through the collateral, the oracle, the loan asset and Morpho Blue, not a lender run.
        return []
    out = []
    for m in MODES.values():
        if node_type not in m.applies_to:
            continue
        if m.id == "unbacked_mint" and node_type == "asset" and attrs.get("kind") in ("pt",):
            continue
        out.append(m)
    return out


def depeg_class(attrs: dict) -> str:
    kind = attrs.get("kind", "unknown")
    return "wrapper" if attrs.get("wraps") and kind != "pt" else kind


def _below(value: float | None, limit: float) -> bool:
    """True when a known value is under the limit; a missing value (None) never triggers, a real 0 does."""
    return value is not None and value < limit


def hazard_prior(mode: str, node_type: str, attrs: dict, rf: dict) -> tuple[float, float, list[str]]:
    """Return (shape, rate) of a Gamma prior on the annual failure rate, plus the reasons for multipliers.

    Only structural factors live here. What is happening right now (a stale feed, a peg slipping) is a
    monitoring signal: ``signals.py`` turns it into an alert level and ``probability.py`` applies it as a
    likelihood ratio, so it is not counted twice.
    """
    reasons: list[str] = []
    if mode == "depeg":
        cls = depeg_class(attrs)
        p = DEPEG_PRIORS.get(cls, DEPEG_PRIORS["unknown"])
        k, T = p["k"], p["T"]
        if cls == "wrapper":
            reasons.append(f"base rate for wrapper discounts: {k} events / {T} token-years; "
                           f"a {attrs['wraps']} depeg is modelled on its own node")
        else:
            reasons.append(f"base rate for {cls} tokens: {k} events / {T} token-years")
    elif mode == "chain_halt":
        k, T, _ = CHAIN_HALT.get(attrs.get("chain_class", "l1"), CHAIN_HALT["l1"])
        reasons.append(f"halts for {attrs.get('chain_class', 'l1')}: {k} / {T} chain-years")
    else:
        b = BASE[mode]
        k, T = b["k"], b["T"]
        reasons.append(f"base rate: {k} events / {T} component-years")

    mult = 1.0

    def m(f: float, why: str):
        nonlocal mult
        mult *= f
        reasons.append(f"x{f:g} {why}")

    if mode in ("oracle_wrong_price", "oracle_stale"):
        if rf.get("tracks_market") is False:
            if mode == "oracle_wrong_price":
                m(0.5, "exchange-rate / fixed oracle is hard to manipulate")
            else:
                m(3.0, "oracle does not follow the market price at all")
        if rf.get("single_feed"):
            m(1.5, "single price source")
        if _below(rf.get("collateral_tvl_usd"), 10e6):
            m(2.0, "thinly traded collateral is cheaper to manipulate")
        if _below(rf.get("age_days"), 180):
            m(1.5, "market younger than 6 months")
    elif mode == "depeg":
        if rf.get("offchain_backing"):
            m(1.3, "backing held off-chain, not verifiable on-chain")
        if rf.get("unverified"):
            m(1.5, "issuer not in curated registry")
        if rf.get("young"):
            m(1.5, "token younger than 1 year")
    elif mode == "unbacked_mint":
        ctl = rf.get("controller_kind")
        if rf.get("upgradeable"):
            m(1.5, "upgradeable proxy: admin can change the code")
        if ctl == "eoa" and rf.get("issuer_tier") == "regulated":
            m(1.5, "single key (EOA) held by a regulated issuer - institutional custody assumed, not verifiable")
        elif ctl == "eoa":
            m(4.0, "controlled by a single key (EOA)")
        elif ctl == "multisig":
            thr = rf.get("threshold") or 1
            m(0.7 if thr >= 3 else 1.2, f"multisig {thr}-of-{rf.get('signers') or '?'}")
        elif ctl == "timelock":
            m(0.5, "behind a timelock")
        elif ctl == "none_detected":
            m(0.5, "no owner() or proxy admin found (role-based minting not ruled out)")
        if rf.get("immutable"):
            m(0.1, "no owner and not upgradeable")
        if rf.get("bridge_minted"):
            m(2.0, "mint controlled by a bridge")
    elif mode == "issuer_default":
        tier = rf.get("issuer_tier")
        if tier == "regulated":
            m(0.25, "regulated issuer with audited reserves")
        elif tier == "unknown":
            m(2.0, "issuer unknown / unverified")
    elif mode == "bridge_hack":
        if rf.get("canonical"):
            m(0.2, "canonical rollup bridge secured by L1")
    elif mode == "exploit":
        if rf.get("immutable_core"):
            m(0.15, "immutable, heavily audited core contract")
        if rf.get("audited_major"):
            m(0.4, "major protocol with multiple audits")
        if _below(rf.get("age_days"), 365):
            m(2.0, "deployed less than a year ago")
        if (rf.get("tvl_usd") or 0) > 1e9:
            m(0.7, "survived with > $1B TVL (Lindy)")
    elif mode == "liquidity_drain":
        u = rf.get("utilization") or 0
        if u > 0.85:
            f = 1 + 10 * (u - 0.85)
            m(round(f, 2), f"utilization already {u:.0%}")

    # Gamma(k + 0.5, T) is the Jeffreys posterior for a Poisson rate; the multiplier scales the rate.
    return k + 0.5, T / mult, reasons


def severity_prior(mode: str, node_type: str, attrs: dict) -> tuple[float, float]:
    """(mean, kappa) of a Beta over loss fraction; kappa == 0 means an exponential in hours."""
    if mode == "depeg":
        return DEPEG_PRIORS.get(depeg_class(attrs), DEPEG_PRIORS["unknown"])["sev"]
    if mode == "chain_halt":
        return (CHAIN_HALT.get(attrs.get("chain_class", "l1"), CHAIN_HALT["l1"])[2], 0)
    if mode == "exploit" and attrs.get("immutable_core"):
        return (0.30, 2.5)
    if mode == "unbacked_mint" and attrs.get("issuer_tier") == "regulated":
        # The key risk is real (Paxos's single key minted $300T PYUSD in Oct 2025), but a regulated issuer
        # can blacklist and burn the excess and redeem holders from audited reserves: it burned it in 22 minutes.
        return (0.10, 4)
    return BASE[mode]["sev"]


def asset_vol(kind: str) -> float:
    return DEPEG_PRIORS.get(kind, DEPEG_PRIORS["unknown"])["vol"]
