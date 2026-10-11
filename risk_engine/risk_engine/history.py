"""Run history in SQLite: what each screening said, which signals fired, and what actually failed.

Tables:
- ``runs``: one row per screening of a position (verdict, expected loss, exit cost, alert level).
- ``forecasts``: every (component, mode) probability of every run, the input to calibration.
- ``signals``: monitoring signals per run, used for "since when" and for contagion episodes.
- ``metrics``: raw live readings per component (supply, liquidity, prices), used for 24h-change rules.
- ``outcomes``: failures that actually happened, recorded by a person (the calibration labels).
- ``events``: notifications raised when a verdict flips or a probability crosses break-even.
- ``watchlist``: positions the scheduled monitor screens, one per ``position_key``.
- ``settings``: monitor settings changed at runtime (the schedule interval).

Each call opens its own connection, so one ``History`` can be shared by the API worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .signals import LEVEL_RANK

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, position_key TEXT NOT NULL, label TEXT, kind TEXT, address TEXT,
    chain_id INTEGER, size_usd REAL, horizon_days REAL, verdict TEXT, expected_loss_pct REAL, baseline_loss_pct REAL, avoidable_loss_pct REAL,
    p_any REAL, exit_cost_pct REAL, instant_fraction REAL, max_alert TEXT, contagion REAL, summary TEXT);
CREATE INDEX IF NOT EXISTS runs_pos ON runs(position_key, ts);
CREATE TABLE IF NOT EXISTS forecasts (
    run_id INTEGER NOT NULL, ts REAL NOT NULL, position_key TEXT NOT NULL, node_id TEXT NOT NULL, node_class TEXT,
    mode TEXT NOT NULL, horizon_days REAL NOT NULL, p REAL NOT NULL, prior_p REAL, alert TEXT, likelihood_ratio REAL,
    rate_multiplier REAL);
CREATE INDEX IF NOT EXISTS forecasts_ts ON forecasts(ts);
CREATE TABLE IF NOT EXISTS signals (
    run_id INTEGER NOT NULL, ts REAL NOT NULL, position_key TEXT NOT NULL, node_id TEXT NOT NULL, node_class TEXT,
    label TEXT, mode TEXT NOT NULL, rule TEXT NOT NULL, level TEXT NOT NULL, value REAL, detail TEXT);
CREATE INDEX IF NOT EXISTS signals_key ON signals(node_id, mode, rule, ts);
CREATE INDEX IF NOT EXISTS signals_ts ON signals(ts);
CREATE TABLE IF NOT EXISTS metrics (ts REAL NOT NULL, node_id TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL);
CREATE INDEX IF NOT EXISTS metrics_key ON metrics(node_id, metric, ts);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, node_id TEXT NOT NULL, node_class TEXT, mode TEXT NOT NULL,
    severity REAL, note TEXT, recorded_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, ts REAL NOT NULL, position_key TEXT, label TEXT, kind TEXT NOT NULL, severity TEXT NOT NULL,
    message TEXT NOT NULL, data TEXT);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS watchlist (
    id INTEGER PRIMARY KEY, position_key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, address TEXT NOT NULL,
    chain_id INTEGER NOT NULL, size_usd REAL NOT NULL, book_usd REAL NOT NULL, horizon_days REAL NOT NULL,
    label TEXT, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# A signal not seen for this long has cleared; the next sighting starts a new episode.
EPISODE_GAP_S = 36 * 3600


class History:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        else:
            # An in-memory database lives only as long as its connection, so keep one open (tests).
            self._mem = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem.row_factory = sqlite3.Row
        with self._conn() as c:
            c.executescript(SCHEMA)
            if "origin_node_id" not in {r[1] for r in c.execute("PRAGMA table_info(signals)")}:
                c.execute("ALTER TABLE signals ADD COLUMN origin_node_id TEXT")
            cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
            for col in ("yield_pct", "net_loss_pct"):
                if col not in cols:
                    c.execute(f"ALTER TABLE runs ADD COLUMN {col} REAL")
            cols = {r[1] for r in c.execute("PRAGMA table_info(watchlist)")}
            for col, typ in (("ltv", "REAL"), ("deploy_address", "TEXT"), ("account", "TEXT")):
                if col not in cols:
                    c.execute(f"ALTER TABLE watchlist ADD COLUMN {col} {typ}")

    @contextmanager
    def _conn(self):
        if self.path == ":memory:":
            yield self._mem
            self._mem.commit()
            return
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        try:
            c.execute("PRAGMA journal_mode=WAL")
            yield c
            c.commit()
        finally:
            c.close()

    # ------------------------------------------------------------------ write
    def record_run(self, run: dict, forecasts: list[dict], signals: list[dict], metrics: list[tuple[str, str, float]]) -> int:
        ts = run["ts"]
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO runs (ts, position_key, label, kind, address, chain_id, size_usd, horizon_days, verdict, "
                "expected_loss_pct, baseline_loss_pct, avoidable_loss_pct, p_any, exit_cost_pct, instant_fraction, max_alert, contagion, summary, "
                "yield_pct, net_loss_pct) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, run["position_key"], run.get("label"), run.get("kind"), run.get("address"), run.get("chain_id"),
                 run.get("size_usd"), run["horizon_days"], run["verdict"], run["expected_loss_pct"], run["baseline_loss_pct"], run["avoidable_loss_pct"],
                 run["p_any"], run["exit_cost_pct"], run["instant_fraction"], run["max_alert"], run.get("contagion"),
                 json.dumps(run.get("summary") or {}), run.get("yield_pct"), run.get("net_loss_pct")))
            rid = cur.lastrowid
            c.executemany(
                "INSERT INTO forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rid, ts, run["position_key"], f["node_id"], f.get("node_class"), f["mode"], run["horizon_days"], f["p"],
                  f.get("prior_p"), f.get("alert"), f.get("likelihood_ratio"), f.get("rate_multiplier")) for f in forecasts])
            c.executemany(
                "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rid, ts, run["position_key"], s["node_id"], s.get("node_class"), s.get("label"), s["mode"], s["rule"],
                  s.get("effective_level", s["level"]), s.get("value"), s.get("detail"),
                  s.get("origin_node_id", s["node_id"])) for s in signals])
            c.executemany("INSERT INTO metrics VALUES (?,?,?,?)", [(ts, nid, m, v) for nid, m, v in metrics])
        return rid

    def add_outcome(self, node_id: str, mode: str, ts: float | None = None, node_class: str | None = None,
                    severity: float | None = None, note: str | None = None) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO outcomes (ts, node_id, node_class, mode, severity, note, recorded_at) VALUES (?,?,?,?,?,?,?)",
                             (ts or time.time(), node_id, node_class, mode, severity, note, time.time())).lastrowid

    def add_events(self, events: list[dict]) -> None:
        with self._conn() as c:
            c.executemany("INSERT INTO events (ts, position_key, label, kind, severity, message, data) VALUES (?,?,?,?,?,?,?)",
                          [(e["ts"], e.get("position_key"), e.get("label"), e["kind"], e["severity"], e["message"],
                            json.dumps(e.get("data") or {})) for e in events])

    def add_watch(self, item: dict, position_key: str) -> int:
        """Add a position to the watchlist; re-adding the same position updates its label and book."""
        with self._conn() as c:
            c.execute("INSERT INTO watchlist (position_key, kind, address, chain_id, size_usd, book_usd, horizon_days, label, created_at, "
                      "ltv, deploy_address, account) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(position_key) DO UPDATE SET label=excluded.label, book_usd=excluded.book_usd",
                      (position_key, item["kind"], item["address"].strip(), item["chain_id"], item["size_usd"], item["book_usd"],
                       item["horizon_days"], item.get("label"), time.time(), item.get("ltv"),
                       (item.get("deploy_address") or "").strip() or None, (item.get("account") or "").strip() or None))
            return c.execute("SELECT id FROM watchlist WHERE position_key=?", (position_key,)).fetchone()[0]

    def remove_watch(self, watch_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM watchlist WHERE id=?", (watch_id,)).rowcount > 0

    def set_setting(self, key: str, value) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))

    # ------------------------------------------------------------------ read
    def last_run(self, position_key: str, before: float | None = None) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM runs WHERE position_key=? AND ts<? ORDER BY ts DESC LIMIT 1",
                            (position_key, before if before is not None else float("inf"))).fetchone()
        return _run(row) if row else None

    def runs(self, position_key: str | None = None, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            if position_key:
                rows = c.execute("SELECT * FROM runs WHERE position_key=? ORDER BY ts DESC LIMIT ?", (position_key, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM runs ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [_run(r, with_summary=False) for r in reversed(rows)]

    def positions(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT r.* FROM runs r JOIN (SELECT position_key, MAX(ts) AS ts FROM runs GROUP BY position_key) l "
                             "ON r.position_key=l.position_key AND r.ts=l.ts ORDER BY r.ts DESC").fetchall()
        return [_run(r, with_summary=False) for r in rows]

    def window(self, node_id: str, metric: str, start: float, end: float) -> list[float]:
        with self._conn() as c:
            rows = c.execute("SELECT value FROM metrics WHERE node_id=? AND metric=? AND ts>=? AND ts<=?",
                             (node_id, metric, start, end)).fetchall()
        return [r[0] for r in rows]

    def signal_first_seen(self, node_id: str, mode: str, rule: str, now: float) -> float | None:
        """Start of the episode this signal belongs to: walk back through sightings until a gap."""
        with self._conn() as c:
            ts = [r[0] for r in c.execute("SELECT DISTINCT ts FROM signals WHERE node_id=? AND mode=? AND rule=? AND ts<? "
                                          "ORDER BY ts DESC LIMIT 500", (node_id, mode, rule, now))]
        first, last = None, now
        for t in ts:
            if last - t > EPISODE_GAP_S:
                break
            first, last = t, t
        return first

    def alert_episodes(self, since: float, until: float) -> list[dict]:
        """Episodes of alert-level signals (any position), one event per (component, mode) episode."""
        with self._conn() as c:
            rows = c.execute("SELECT ts, COALESCE(origin_node_id,node_id) AS node_id, node_class, label, mode, level "
                             "FROM signals WHERE ts>=? AND ts<? ORDER BY COALESCE(origin_node_id,node_id), mode, ts",
                             (since, until)).fetchall()
        out: list[dict] = []
        cur: dict | None = None
        for r in rows:
            if LEVEL_RANK.get(r["level"], 0) < LEVEL_RANK["alert"]:
                continue
            key = (r["node_id"], r["mode"])
            if cur and cur["key"] == key and r["ts"] - cur["last"] <= EPISODE_GAP_S:
                cur["last"] = r["ts"]
                continue
            cur = {"key": key, "ts": r["ts"], "last": r["ts"], "node_id": r["node_id"], "node_class": r["node_class"],
                   "label": r["label"], "mode": r["mode"], "kind": "alert"}
            out.append(cur)
        return [{k: v for k, v in e.items() if k != "key"} for e in out]

    def outcomes(self, since: float | None = None) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM outcomes WHERE ts>=? ORDER BY ts", (since or 0,)).fetchall()
        return [dict(r) for r in rows]

    def forecasts(self, until: float | None = None) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM forecasts WHERE ts<=? ORDER BY ts", (until if until is not None else float("inf"),)).fetchall()
        return [dict(r) for r in rows]

    def events(self, limit: int = 100, position_key: str | None = None) -> list[dict]:
        with self._conn() as c:
            if position_key:
                rows = c.execute("SELECT * FROM events WHERE position_key=? ORDER BY ts DESC, id DESC LIMIT ?", (position_key, limit)).fetchall()
            else:
                rows = c.execute("SELECT * FROM events ORDER BY ts DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [{**dict(r), "data": json.loads(r["data"] or "{}")} for r in rows]

    def watchlist(self) -> list[dict]:
        """Watchlist entries, shaped like the engine presets so ``run_watchlist`` can screen them."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM watchlist ORDER BY created_at, id").fetchall()
        return [dict(r) for r in rows]

    def setting(self, key: str, default=None):
        with self._conn() as c:
            row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default


def _run(row: sqlite3.Row, with_summary: bool = True) -> dict:
    d = dict(row)
    s = d.pop("summary", None)
    if with_summary:
        d["summary"] = json.loads(s or "{}")
    return d
