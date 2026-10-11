from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from risk_engine import PositionRequest, RiskEngine, calibration
from risk_engine.engine import INCIDENTS, PRESETS, position_key, request_from
from risk_engine.explain import explain, explain_monitor
from risk_engine.failures import MODES
from risk_engine.history import History
from risk_engine.probability import ALERT_LEVELS
from risk_engine.sources.http import SourceUnreachable

BACKEND = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND / ".env")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("risk_api")

@asynccontextmanager
async def lifespan(_: FastAPI):
    _start_monitor()
    yield


app = FastAPI(title="DeFi Dependency Risk Engine", version="0.1.0", lifespan=lifespan,
              description="Belem Capital x EPFL Hackathon 2026 - discover, shock, price the exit, decide.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
history = History(os.getenv("HISTORY_DB") or BACKEND / "data" / "history.sqlite3")
engine = RiskEngine(history=history)


def load_watchlist() -> tuple[list[dict], str]:
    """Positions screened on schedule: the saved watchlist, else the JSON list at WATCHLIST, else the presets."""
    saved = history.watchlist()
    if saved:
        return saved, "saved"
    path = os.getenv("WATCHLIST")
    if path:
        return json.loads(Path(path).read_text(encoding="utf-8")), "file"
    return PRESETS, "presets"


def watch_key(it: dict) -> str | None:
    """The ``position_key`` ``run_watchlist`` records this entry under (same defaults)."""
    try:
        return position_key(request_from(it), float(it.get("horizon_days", 30)))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


class BorrowFields(BaseModel):
    """Only for ``kind == "borrow"``: the LTV, the PT the loan is deployed into, or a borrower to read them from."""
    ltv: float | None = Field(None, gt=0, lt=1)
    deploy_address: str | None = None
    account: str | None = None


class AnalyzeBody(BorrowFields):
    kind: Literal["token", "market", "borrow"] = "token"
    address: str
    chain_id: int = 1
    size_usd: float = Field(1_000_000, gt=0)
    book_usd: float = Field(10_000_000, gt=0)
    label: str | None = None
    horizon_days: float = Field(30, gt=0, le=365)
    record: bool = False  # store in the run history and raise notifications (the scheduled monitor does this)


class ShockBody(BaseModel):
    position_id: str
    node_id: str
    mode: str
    alert: Literal["auto", "none", "watch", "alert", "corroborated", "confirmed"] = "auto"
    probability_override: float | None = Field(None, ge=0, le=1)
    exit_cost_override: float | None = Field(None, ge=0, le=1)
    horizon_days: float | None = Field(None, gt=0, le=365)
    use_llm: bool = False  # the UI asks for the LLM narrative separately, through /api/explain


class ReplayBody(BaseModel):
    position_id: str
    incident_id: str
    exit_cost_override: float | None = Field(None, ge=0, le=1)
    use_llm: bool = False


class ExplainBody(BaseModel):
    result: dict  # a /api/shock or /api/replay response


class MonitorExplainBody(BaseModel):
    position_key: str


class ModesBody(BaseModel):
    position_id: str
    node_id: str


class OutcomeBody(BaseModel):
    node_id: str
    mode: str
    ts: float | None = None          # when it happened (unix seconds); default now
    node_class: str | None = None
    severity: float | None = Field(None, ge=0)
    note: str | None = None


class MonitorRunBody(BaseModel):
    horizon_days: float = Field(30, gt=0, le=365)


class WatchBody(BorrowFields):
    kind: Literal["token", "market", "borrow"] = "token"
    address: str = Field(min_length=1)
    chain_id: int = Field(1, gt=0)
    size_usd: float = Field(1_000_000, gt=0)
    book_usd: float = Field(10_000_000, gt=0)
    horizon_days: float = Field(30, gt=0, le=365)
    label: str | None = None


class ScheduleBody(BaseModel):
    interval_s: float = Field(ge=0, le=7 * 86400)  # 0 pauses the schedule


def _llm_provider() -> str:
    if os.getenv("OPENAI_API_KEY"):
        return f"openai:{os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}"
    if os.getenv("ANTHROPIC_API_KEY"):
        return f"anthropic:{os.getenv('ANTHROPIC_MODEL', 'claude-sonnet-4-5')}"
    return "template"


@app.get("/api/health")
def health():
    return {"ok": True, "llm": _llm_provider(), "positions_in_memory": len(engine.store),
            "history_db": history.path, "monitor_interval_s": monitor_interval() or None}


@app.get("/api/presets")
def presets():
    return PRESETS


@app.get("/api/catalog")
def catalog():
    return {
        "modes": [{"id": m.id, "label": m.label, "applies_to": m.applies_to, "description": m.description} for m in MODES.values()],
        "alerts": [{"id": "auto", "label": "Live signals (automatic)", "lr_scale": None}]
                  + [{"id": k, **v} for k, v in ALERT_LEVELS.items()],
        "incidents": INCIDENTS,
    }


@app.post("/api/analyze")
def analyze(body: AnalyzeBody):
    req = PositionRequest(body.kind, body.address.strip(), body.chain_id, body.size_usd, body.book_usd, body.label,
                          body.ltv, (body.deploy_address or "").strip() or None, (body.account or "").strip() or None)
    try:
        return engine.analyze(req, body.horizon_days, record=body.record)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except SourceUnreachable as e:
        raise HTTPException(503, f"Discovery failed: {e}")
    except Exception as e:
        raise HTTPException(502, f"Discovery failed: {type(e).__name__}: {e}")


@app.post("/api/modes")
def modes(body: ModesBody):
    try:
        return engine.node_modes(body.position_id, body.node_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.post("/api/shock")
def shock(body: ShockBody):
    try:
        return engine.shock(body.position_id, body.node_id, body.mode, alert=body.alert,
                            probability_override=body.probability_override, exit_cost_override=body.exit_cost_override,
                            horizon_days=body.horizon_days, use_llm=body.use_llm)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/replay")
def replay(body: ReplayBody):
    try:
        return engine.replay(body.position_id, body.incident_id, exit_cost_override=body.exit_cost_override, use_llm=body.use_llm)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/explain")
def explain_result(body: ExplainBody):
    """LLM narrative for a scenario result, on request. Falls back to the template without an API key."""
    try:
        return explain(body.result)
    except (KeyError, TypeError, IndexError) as e:
        raise HTTPException(422, f"Not a scenario result: missing {e}")


# ---------------------------------------------------------------------- monitoring
@app.get("/api/monitor/positions")
def monitor_positions():
    """Latest recorded run of every monitored position."""
    return history.positions()


@app.get("/api/monitor/history")
def monitor_history(position_key: str, limit: int = 365):
    return history.runs(position_key, limit)


@app.get("/api/monitor/events")
def monitor_events(limit: int = 100, position_key: str | None = None):
    return history.events(limit, position_key)


@app.get("/api/monitor/calibration")
def monitor_calibration():
    now = time.time()
    return calibration.report(history.forecasts(now), history.outcomes(), now)


@app.post("/api/monitor/outcomes")
def monitor_outcome(body: OutcomeBody):
    """Record a failure that actually happened: the label calibration and contagion learn from."""
    if body.mode not in MODES:
        raise HTTPException(422, f"Unknown failure mode {body.mode}")
    return {"id": history.add_outcome(body.node_id, body.mode, body.ts, body.node_class, body.severity, body.note)}


@app.post("/api/monitor/run")
def monitor_run(body: MonitorRunBody):
    """Screen and record the whole watchlist now."""
    return _run_monitor(body.horizon_days)


@app.post("/api/monitor/explain")
def monitor_explain(body: MonitorExplainBody):
    """LLM note on the latest scheduled run of a position, its recent trend and notifications. Template without a key."""
    latest = history.last_run(body.position_key)
    if not latest:
        raise HTTPException(404, "No recorded run for this position yet; run the monitor first")
    return explain_monitor(latest, history.runs(body.position_key, 10), history.events(8, body.position_key))


@app.get("/api/monitor/watchlist")
def monitor_watchlist():
    """Watched positions with their latest recorded verdict and its main drivers."""
    items, source = load_watchlist()
    out = []
    for it in items:
        key = watch_key(it)
        run = history.last_run(key) if key else None
        out.append({**{k: it.get(k) for k in ("id", "kind", "address", "chain_id", "size_usd", "book_usd", "horizon_days", "label",
                                              "ltv", "deploy_address", "account")},
                    "position_key": key, "latest": _brief(run), "error": _status["errors"].get(key)})
    return {"source": source, "items": out}


@app.post("/api/monitor/watchlist")
def monitor_watch(body: WatchBody):
    item = body.model_dump()
    key = watch_key(item)
    return {"id": history.add_watch(item, key), "position_key": key}


@app.delete("/api/monitor/watchlist/{watch_id}")
def monitor_unwatch(watch_id: int):
    if not history.remove_watch(watch_id):
        raise HTTPException(404, "Unknown watchlist entry")
    return {"ok": True}


@app.get("/api/monitor/schedule")
def monitor_schedule():
    return _schedule()


@app.put("/api/monitor/schedule")
def monitor_set_schedule(body: ScheduleBody):
    if 0 < body.interval_s < MIN_INTERVAL_S:
        raise HTTPException(422, f"The interval must be 0 (paused) or at least {MIN_INTERVAL_S}s: every run queries live sources")
    history.set_setting("monitor_interval_s", body.interval_s)
    _wake.set()
    return _schedule()


def _brief(run: dict | None) -> dict | None:
    """A recorded run without the per-row summary, plus its five largest expected-loss drivers."""
    if not run:
        return None
    s = run.pop("summary", None) or {}
    drivers = sorted(s.get("rows", {}).values(), key=lambda r: -(r.get("el") or 0))[:5]
    return {**run, "model_verdict": s.get("model_verdict"), "data_issues": s.get("data_issues") or [], "drivers": drivers}


DEFAULT_INTERVAL_S = float(os.getenv("MONITOR_INTERVAL_S") or 0)
MIN_INTERVAL_S = 300
_monitor_lock = threading.Lock()
_wake = threading.Event()  # set when the schedule changes or a manual run finishes, so the loop re-plans
_status: dict = {"running": False, "next_run": None, "errors": {}}


def monitor_interval() -> float:
    return float(history.setting("monitor_interval_s", DEFAULT_INTERVAL_S) or 0)


def _schedule() -> dict:
    _, source = load_watchlist()
    return {"interval_s": monitor_interval(), "min_interval_s": MIN_INTERVAL_S, "running": _status["running"],
            "last_run": history.setting("monitor_last_run"), "next_run": _status["next_run"], "watchlist_source": source}


def _run_monitor(horizon_days: float = 30) -> list[dict]:
    with _monitor_lock:
        _status["running"] = True
        try:
            items, _ = load_watchlist()
            res = engine.run_watchlist(items, horizon_days)
        finally:
            _status["running"] = False
            history.set_setting("monitor_last_run", time.time())
            _wake.set()
        _status["errors"] = {watch_key(it): r["error"] for it, r in zip(items, res) if not r["ok"]}
    log.info("monitor: screened %d positions, %d failed", len(res), sum(not r["ok"] for r in res))
    return res


def _monitor_loop():
    """Runs the watchlist every ``monitor_interval()`` seconds, counted from the last run (also across restarts)."""
    while True:
        interval = monitor_interval()
        if interval > 0:
            _status["next_run"] = (history.setting("monitor_last_run") or 0) + interval
            wait = max(0.0, _status["next_run"] - time.time())
        else:
            _status["next_run"] = wait = None
        if _wake.wait(wait):
            _wake.clear()
            continue
        try:
            _run_monitor()
        except Exception:  # noqa: BLE001 - the loop must survive a bad watchlist file
            log.exception("monitor run failed")


def _start_monitor():
    threading.Thread(target=_monitor_loop, name="monitor", daemon=True).start()
    log.info("monitor: interval %.0fs (0 = paused)", monitor_interval())


DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")
