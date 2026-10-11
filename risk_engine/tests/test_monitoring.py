"""Offline tests for the monitoring layer: signals, history, notifications, contagion, calibration."""

import numpy as np

from risk_engine import calibration, contagion
from risk_engine.discovery import PositionRequest
from risk_engine.engine import RiskEngine
from risk_engine.graph import Edge, Node
from risk_engine.history import History
from risk_engine.probability import failure_probability
from risk_engine.signals import alert_levels, detect
from test_engine import add, build, build_market

DAY = 86400.0
T0 = 1_800_000_000.0
TOKEN = PositionRequest("token", "0xpt", 1, 1_000_000)
MARKET = PositionRequest("market", "0xmarket", 1, 1_000_000)


def level(g, node, mode, now=T0, history=None):
    lv = alert_levels(detect(g, now, history)).get((node, mode))
    return lv["level"] if lv else "none"


def test_calm_graph_raises_no_signals():
    assert detect(build_market(util=0.9), T0) == []


def test_peg_deviation_thresholds():
    g = build()
    g.nodes["usdx"].risk_factors["price_deviation"] = 0.003
    assert level(g, "usdx", "depeg") == "none"
    g.nodes["usdx"].risk_factors["price_deviation"] = 0.008
    assert level(g, "usdx", "depeg") == "watch"
    g.nodes["usdx"].risk_factors["price_deviation"] = 0.03
    assert level(g, "usdx", "depeg") == "alert"


def test_utilization_pinned_and_bad_debt_corroborate():
    g = build_market(util=0.98)
    assert level(g, "market", "liquidity_drain") == "watch"
    g.nodes["market"].attrs.update({"supply_usd": 10e6, "bad_debt_usd": 200_000})  # 2% of supply
    assert level(g, "market", "liquidity_drain") == "corroborated"


def test_same_family_signals_do_not_corroborate():
    # A PT price drop and its implied-APY jump are one move seen twice.
    g = build_market()
    g.nodes["market"].attrs["daily_price_variation"] = -0.06
    h = History(":memory:")
    h.record_run(_run(T0 - 3600), [], [], [("amm", "implied_apy", 0.08)])
    g.nodes["amm"].attrs["implied_apy"] = 0.20
    sig = detect(g, T0, h)
    assert {s.rule for s in sig if s.node_id == "pt"} == {"collateral_drop", "implied_apy_jump"}
    assert level(g, "pt", "depeg", history=h) == "alert"


def test_stale_feed_alerts_feed_and_oracle():
    g = build_market()
    add(g, [Node("feed", "feed", "PT / USD", attrs={"stale": True, "age_s": 30 * 3600})], [])
    g.add_edge(Edge("oracle", "feed", "reads_feed", attrs={"role": "baseFeedOne"}))
    assert level(g, "feed", "oracle_stale") == "alert"
    assert level(g, "oracle", "oracle_stale") == "alert"


def test_oracle_above_market_flags_wrong_price_only_when_overpricing():
    g = build_market()
    g.nodes["oracle"].attrs.update({"oracle_price": 1.0, "market_price": 0.9, "oracle_dex_gap": 1 / 0.9 - 1})
    assert level(g, "oracle", "oracle_wrong_price") == "alert"
    g.nodes["oracle"].attrs["oracle_dex_gap"] = -0.10
    assert level(g, "oracle", "oracle_wrong_price") == "none"


def test_supply_outflow_needs_history():
    g = build_market()
    g.nodes["market"].attrs["supply_usd"] = 6e6
    assert level(g, "market", "liquidity_drain") == "none"
    h = History(":memory:")
    h.record_run(_run(T0 - 6 * 3600), [], [], [("market", "supply_usd", 10e6)])
    assert level(g, "market", "liquidity_drain", history=h) == "alert"  # 40% withdrawn in 24h
    old = History(":memory:")
    old.record_run(_run(T0 - 3 * DAY), [], [], [("market", "supply_usd", 10e6)])
    assert level(g, "market", "liquidity_drain", history=old) == "none"  # outside the 24h window


def test_volatile_assets_ignore_price_drop_rule():
    g = build()
    g.nodes["usdx"].attrs.update({"kind": "volatile", "price_usd": 80.0})
    h = History(":memory:")
    h.record_run(_run(T0 - 3600), [], [], [("usdx", "price_usd", 100.0)])
    assert level(g, "usdx", "depeg", history=h) == "none"
    g.nodes["usdx"].attrs["kind"] = "synthetic_stable"
    assert level(g, "usdx", "depeg", history=h) == "alert"


def test_base_p_shares_draws_with_multiplied_p():
    node = Node("coll", "asset", "COLL", attrs={"kind": "synthetic_stable"})
    one = failure_probability(node, "depeg", 30, np.random.default_rng(0))
    two = failure_probability(node, "depeg", 30, np.random.default_rng(0), rate_multiplier=2.0)
    assert one["p"] == one["base_p"] == two["base_p"]
    assert two["p"] > two["base_p"]


def liquid():
    """Held PT on a deep AMM, so the exit is cheap (constant-product impact ~0.4% on $1M)."""
    g = build()
    g.nodes["amm"].attrs["liquidity_usd"] = 500e6
    for node in g.nodes.values():
        if node.type == "asset":
            node.attrs["price_usd"] = 1.0
    return g


def test_signals_move_screening_probability_and_verdict(monkeypatch):
    monkeypatch.setattr("risk_engine.engine.exit_cost", lambda *a, **k: {"cost_pct": .03, "instant_fraction": 1})
    eng = RiskEngine(history=History(":memory:"))
    calm = eng.evaluate(liquid(), TOKEN, record=True, now=T0)
    assert calm["monitor"]["verdict"] == "STAY" and calm["monitor"]["events"] == []
    g = liquid()
    g.nodes["usdx"].risk_factors["price_deviation"] = 0.04
    hot = eng.evaluate(g, TOKEN, record=True, now=T0 + DAY)
    row = next(r for r in hot["screening"]["top"] if r["node_id"] == "usdx" and r["mode"] == "depeg")
    assert row["alert"] == "alert" and row["p"] > 3 * row["base_p"]
    m = hot["monitor"]
    assert m["verdict"] == "EXIT" and m["previous"]["verdict"] == "STAY"
    kinds = {(e["kind"], e["severity"]) for e in m["events"]}
    assert ("verdict", "critical") in kinds and ("break_even", "warning") in kinds and ("alert", "warning") in kinds
    assert {e["kind"] for e in eng.history.events()} >= {"verdict", "break_even", "alert"}


def test_daily_summary_uses_recorded_runs(monkeypatch):
    from risk_engine.explain import explain_monitor
    monkeypatch.setattr("risk_engine.engine.exit_cost", lambda *a, **k: {"cost_pct": .03, "instant_fraction": 1})
    eng = RiskEngine(history=History(":memory:"))
    key = eng.evaluate(liquid(), TOKEN, record=True, now=T0)["monitor"]["position_key"]
    g = liquid()
    g.nodes["usdx"].risk_factors["price_deviation"] = 0.04
    eng.evaluate(g, TOKEN, record=True, now=T0 + DAY)
    h = eng.history
    out = explain_monitor(h.last_run(key), h.runs(key, 10), h.events(8, key), use_llm=False)
    assert out["provider"] == "template"
    assert "EXIT" in out["text"] and "STAY -> EXIT" in out["text"] and "What to watch" in out["text"]


def test_unrecorded_runs_leave_history_untouched():
    eng = RiskEngine(history=History(":memory:"))
    eng.evaluate(build(), TOKEN, now=T0)
    assert eng.history.runs() == [] and eng.history.events() == []


def test_shock_auto_alert_uses_live_signals():
    eng = RiskEngine()
    g = build()
    g.nodes["usdx"].risk_factors["price_deviation"] = 0.04
    pid = eng.evaluate(g, TOKEN, now=T0)["position_id"]
    auto = eng.shock(pid, "usdx", "depeg", use_llm=False, n=2000)
    assert auto["shock"]["alert"] == "alert" and auto["shock"]["alert_source"] == "signals"
    quiet = eng.shock(pid, "issuer", "issuer_default", use_llm=False, n=2000)
    assert quiet["shock"]["alert"] == "none"
    assert auto["explanation"]["provider"] == "template" and auto["explanation"]["text"]


def test_watchlist_and_settings_persist():
    h = History(":memory:")
    item = {"kind": "token", "address": " 0xPT ", "chain_id": 1, "size_usd": 1e6, "book_usd": 1e7, "horizon_days": 30, "label": "PT"}
    wid = h.add_watch(item, "k1")
    assert h.add_watch({**item, "label": "PT renamed"}, "k1") == wid  # same position: updated, not duplicated
    assert [(w["id"], w["address"], w["label"]) for w in h.watchlist()] == [(wid, "0xPT", "PT renamed")]
    assert h.remove_watch(wid) and not h.remove_watch(wid) and h.watchlist() == []
    assert h.setting("monitor_interval_s", 0) == 0
    h.set_setting("monitor_interval_s", 3600)
    h.set_setting("monitor_interval_s", 900)
    assert h.setting("monitor_interval_s") == 900


def test_contagion_from_recent_failure_decays():
    g = build_market()
    keys = [("usdx", "depeg"), ("usdc", "depeg"), ("chain", "chain_halt")]
    other = {"ts": T0 - 5 * DAY, "node_id": "asset:1:0xother", "node_class": "asset:synthetic_stable", "mode": "depeg",
             "kind": "outcome", "label": "OTHERUSD"}
    m = contagion.multipliers(g, keys, [other], T0)
    assert m[("usdx", "depeg")]["multiplier"] > m[("usdc", "depeg")]["multiplier"] > 1  # same class > elsewhere
    assert m[("usdx", "depeg")]["reasons"]
    stale = contagion.multipliers(g, keys, [{**other, "ts": T0 - 365 * DAY}], T0)
    assert stale[("usdx", "depeg")]["multiplier"] < 1.01


def test_own_live_alert_is_not_double_counted_as_contagion():
    g = build()
    g.nodes["usdx"].risk_factors["price_deviation"] = 0.04
    events = contagion.gather_events(g, detect(g, T0), T0, None)
    m = contagion.multipliers(g, [("usdx", "depeg"), ("issuer", "issuer_default")], events, T0)
    assert m[("usdx", "depeg")]["multiplier"] == 1.0
    assert m[("issuer", "issuer_default")]["multiplier"] > 1.0


def test_history_outcomes_feed_contagion_in_screening():
    h = History(":memory:")
    eng = RiskEngine(history=h)
    base = eng.evaluate(build(), TOKEN, now=T0)
    h.add_outcome("asset:1:0xother", "depeg", ts=T0 - 2 * DAY, node_class="asset:synthetic_stable", note="OTHERUSD depeg")
    after = eng.evaluate(build(), TOKEN, now=T0)
    p = lambda a: next(r["p"] for r in a["screening"]["top"] if r["node_id"] == "usdx" and r["mode"] == "depeg")
    assert p(after) > 1.5 * p(base)
    assert after["monitor"]["regime"] != "calm"


def test_alert_episodes_merge_consecutive_sightings():
    h = History(":memory:")
    for i, ts in enumerate((T0, T0 + DAY, T0 + 2 * DAY, T0 + 10 * DAY)):
        h.record_run(_run(ts), [], [{"node_id": "usdx", "mode": "depeg", "rule": "peg_deviation", "level": "alert",
                                     "node_class": "asset:synthetic_stable", "label": "USDX"}], [])
    eps = h.alert_episodes(T0 - DAY, T0 + 20 * DAY)
    assert [e["ts"] for e in eps] == [T0, T0 + 10 * DAY]
    assert h.signal_first_seen("usdx", "depeg", "peg_deviation", T0 + 2.5 * DAY) == T0


def test_calibration_counts_one_forecast_per_day_and_scores():
    fc = []
    for d in range(60):
        for rep in range(3):  # three screenings a day collapse into one forecast
            fc.append({"ts": T0 + d * DAY + rep * 3600, "position_key": "k", "node_id": "usdx", "mode": "depeg",
                       "horizon_days": 30, "p": 0.1, "alert": "none", "likelihood_ratio": None})
    outcomes = [{"node_id": "usdx", "mode": "depeg", "ts": T0 + 40 * DAY}]
    r = calibration.report(fc, outcomes, now=T0 + 90 * DAY)
    assert r["n"] == 2 and r["daily_forecasts"] == 60 and r["pending"] == 0
    assert r["events"] == 1 and r["positive_forecasts"] == 1  # one distinct incident
    assert 0 <= r["brier"] <= 1 and r["reliability"][0]["n"] == 2
    assert calibration.report(fc, outcomes, now=T0)["n"] == 0


def _run(ts: float) -> dict:
    return {"ts": ts, "position_key": "test", "horizon_days": 30, "verdict": "STAY", "expected_loss_pct": 0.0,
            "baseline_loss_pct": 0.0, "avoidable_loss_pct": 0.0, "p_any": 0.0, "exit_cost_pct": 0.0, "instant_fraction": 1.0,
            "max_alert": "none"}
