"""Screen and record a watchlist: run it from cron (or with --every) for daily monitoring.

    uv --directory backend run python scripts/monitor.py                     # once, the saved watchlist (else the presets)
    uv --directory backend run python scripts/monitor.py --watchlist w.json  # once, your positions
    uv --directory backend run python scripts/monitor.py --every 3600        # hourly, until stopped
    uv --directory backend run python scripts/monitor.py --calibration       # how good were past forecasts?

Settings come from backend/.env: HISTORY_DB, WATCHLIST, ALERT_WEBHOOK_URL.
"""

import argparse
import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from risk_engine import History, RiskEngine, calibration
from risk_engine.engine import PRESETS

BACKEND = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ap = argparse.ArgumentParser()
ap.add_argument("--watchlist", default=os.getenv("WATCHLIST"), help="JSON list shaped like the presets")
ap.add_argument("--horizon", type=float, default=30)
ap.add_argument("--every", type=float, default=0, help="repeat every N seconds")
ap.add_argument("--calibration", action="store_true", help="print the calibration report and exit")
args = ap.parse_args()

history = History(os.getenv("HISTORY_DB") or BACKEND / "data" / "history.sqlite3")
if args.calibration:
    now = time.time()
    print(json.dumps(calibration.report(history.forecasts(now), history.outcomes(), now), indent=2))
    raise SystemExit

engine = RiskEngine(history=history)
while True:
    # Re-read each pass so positions added from the UI's Monitor panel are picked up.
    watchlist = (json.loads(Path(args.watchlist).read_text(encoding="utf-8")) if args.watchlist
                 else history.watchlist() or PRESETS)
    for r in engine.run_watchlist(watchlist, args.horizon):
        if not r["ok"]:
            print(f"FAILED  {r['label']}: {r['error']}")
            continue
        print(f"{r['verdict']:6}  {r['label'][:50]:50}  EL {r['expected_loss_pct']:.2%} - yield {r['yield_pct']:.2%}"
              f" -> net {r['net_loss_pct']:.2%} vs exit {r['exit_cost_pct']:.2%}  alert {r['max_alert']:12}  {r['regime']}")
        for e in r["events"]:
            print(f"      [{e['severity']}] {e['message']}")
    if not args.every:
        break
    time.sleep(args.every)
