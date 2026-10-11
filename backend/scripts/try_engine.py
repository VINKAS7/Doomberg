import sys
import time

from risk_engine import RiskEngine
from risk_engine.engine import PRESETS, request_from

preset = next(p for p in PRESETS if p["id"] == (sys.argv[1] if len(sys.argv) > 1 else "pt-reusd-market"))
eng = RiskEngine()
t = time.time()
a = eng.analyze(request_from(preset))
print(f"analyse: {a['stats']} in {time.time() - t:.1f}s")
if a.get("leverage"):
    print("leverage:", {k: v for k, v in a["leverage"].items() if k != "live"})
m = a["monitor"]
print(f"monitor: {m['verdict']} EL {m['expected_loss_pct']:.3%} (liquidation {m['liquidation_loss_pct']:.3%}) "
      f"yield {m['yield_pct']:.3%} exit {m['exit_cost_pct']:.3%}")
s = a["screening"]
print(f"total EL {s['total_expected_loss_pct']:.4%} / {s['horizon_days']}d, P(any loss event) {s['p_any_loss_event']:.2%}")
for r in s["top"][:8]:
    print(f"  {r['share']:6.1%}  {r['label'][:30]:30} {r['mode']:22} p={r['p']:.3%} L|f={r['loss_given_failure']:.2%}")
print("exit now:", a["exit_now"]["cost_pct"], [st["detail"] for st in a["exit_now"]["steps"]])

top = s["top"][0]
t = time.time()
r = eng.shock(a["position_id"], top["node_id"], top["mode"])
d = r["decision"]
print(f"\nSHOCK {r['shock']['node_label']} / {r['shock']['mode_label']} in {time.time() - t:.1f}s")
print(f"  verdict {d['verdict']} conf {d['confidence']:.0%}  p={d['probability']:.2%}  L|f={d['loss_given_failure']}")
print(f"  EL ${d['expected_loss_usd']:,.0f}  exit ${d['exit_cost_usd']:,.0f} ({d['exit_cost_pct']:.2%})  break-even {d['break_even_probability']}")
for p in r["paths"][:2]:
    print("  path:", " -> ".join(f"{h['label']}[{h['mean_loss']:.1%}]" for h in p["hops"]))
print(r["explanation"]["provider"]); print(r["explanation"]["text"])

for inc in ["usdc-svb-2023", "stream-xusd-2025", "mango-2022"]:
    try:
        rr = eng.replay(a["position_id"], inc, use_llm=False)
        dd = rr["decision"]
        print(f"\nREPLAY {inc} on {rr['shock']['node_label']}: {dd['verdict']} loss mean {dd['loss_given_failure']['mean']:.2%} "
              f"EL ${dd['expected_loss_usd']:,.0f} exit {dd['exit_cost_pct']:.2%}")
    except Exception as e:
        print("REPLAY", inc, "->", e)

