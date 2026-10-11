import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ChevronRight, Play, Plus, Sparkles, Trash2, X } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../api";
import { netLoss, type Analysis, type Explanation, type MonitorEvent, type MonitorRun, type Schedule, type WatchItem, type Watchlist } from "../types";
import { investmentLabel } from "../ui/investments";
import { CHAINS, pct, short, usd } from "../format";

const INTERVALS: [number, string][] = [
  [0, "Off"],
  [900, "Every 15 minutes"],
  [3600, "Every hour"],
  [4 * 3600, "Every 4 hours"],
  [12 * 3600, "Every 12 hours"],
  [86400, "Every day"],
];

const when = (ts: number | null | undefined) =>
  ts ? new Date(ts * 1000).toLocaleString() : "never";

const intervalLabel = (s: number) =>
  INTERVALS.find(([v]) => v === s)?.[1] ?? `Every ${Math.round(s / 60)} minutes`;

function Details({ item }: { item: WatchItem }) {
  const [runs, setRuns] = useState<MonitorRun[] | null>(null);
  const [events, setEvents] = useState<MonitorEvent[]>([]);
  const key = item.position_key;
  const ts = item.latest?.ts;
  useEffect(() => {
    if (!key) return;
    let stale = false;
    Promise.all([api.monitor.history(key), api.monitor.events(key)])
      .then(([h, e]) => {
        if (stale) return;
        setRuns(h);
        setEvents(e);
      })
      .catch(() => !stale && setRuns([]));
    return () => {
      stale = true;
    };
  }, [key, ts]);
  // Kept with the run it was written for, so a newer run drops it.
  const [ai, setAi] = useState<{ ts: number; summary: Explanation } | null>(null);
  const [aiBusy, setAiBusy] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const generateSummary = async () => {
    if (!key || !ts) return;
    setAiBusy(true);
    setAiError(null);
    try {
      setAi({ ts, summary: await api.monitor.explain(key) });
    } catch (e) {
      setAiError(e instanceof Error ? e.message : "Summary request failed.");
    } finally {
      setAiBusy(false);
    }
  };
  const summary = ai && ai.ts === ts ? ai.summary : null;
  const l = item.latest;
  if (!l) return <p className="overview-note">{item.error ?? "Not screened yet. It runs on the next scheduled pass, or press Run now."}</p>;
  const series = (runs ?? []).map((r) => ({
    t: r.ts * 1000,
    net: netLoss(r),
    exit: r.exit_cost_pct,
  }));
  const net = netLoss(l);
  return (
    <div className="monitor-details">
      <div className="ai-summary monitor-ai">
        {summary ? (
          <>
            <div className="monitor-ai-head">
              <span className="muted small">
                {summary.provider === "template"
                  ? "Rule-based daily summary"
                  : `AI daily summary · ${summary.provider} · ${summary.model ?? ""}`}
              </span>
              <button className="btn subtle" onClick={() => void generateSummary()} disabled={aiBusy}>
                <Sparkles size={14} />
                {aiBusy ? "Writing…" : "Regenerate"}
              </button>
            </div>
            {summary.provider === "template" && (
              <p className="execution-note">
                {summary.error
                  ? `The LLM request failed (${summary.error}), so the rule-based summary is shown.`
                  : "No LLM API key is configured on the backend, so the rule-based summary is shown."}
              </p>
            )}
            <div className="md">
              <ReactMarkdown>{summary.text}</ReactMarkdown>
            </div>
          </>
        ) : (
          <button
            className="btn subtle"
            onClick={() => void generateSummary()}
            disabled={aiBusy}
            title="Asks the LLM to summarise the latest run, its trend and notifications. Numbers and the verdict come from the model, not the LLM."
          >
            <Sparkles size={14} />
            {aiBusy ? "Writing summary…" : "Generate AI summary"}
          </button>
        )}
        {aiError && (
          <p className="execution-note" role="alert">
            {aiError}
          </p>
        )}
      </div>
      <div>
        <div className="card-h">Why {l.verdict}</div>
        <p className="mono">
          ({pct(l.expected_loss_pct, 2)} expected loss − {pct(l.yield_pct ?? 0, 2)} yield) × {pct(l.instant_fraction, 0)} withdrawable ={" "}
          {pct(net, 2)} {net > l.exit_cost_pct ? ">" : "≤"} {pct(l.exit_cost_pct, 2)} exit cost
        </p>
        <p className="execution-note">
          The yield is what staying earns over the horizon, less the redeploy days already charged in the exit cost.
          Exited capital is assumed to earn nothing.
        </p>
        {l.verdict === "REVIEW" && (
          <p className="execution-note">
            Required live readings are missing, so no actionable verdict is given
            {l.model_verdict ? ` (the model alone says ${l.model_verdict})` : ""}.
          </p>
        )}
        {l.data_issues.map((d) => (
          <p key={d.node_id} className="execution-note">{d.detail}</p>
        ))}
        <div className="card-h">Largest risk drivers</div>
        <ul className="assume">
          {l.drivers.map((d) => (
            <li key={d.node_id + d.mode}>
              <b>{d.label}</b> · {d.mode_label}: {pct(d.p, 2)} chance, {pct(d.el, 3)} expected loss
              {d.above ? " · above break-even" : ""}
              {d.alert !== "none" ? ` · ${d.alert}` : ""}
              {d.signals[0] ? <small>{d.signals[0]}</small> : null}
            </li>
          ))}
        </ul>
        {item.error && <p className="execution-note">Last run failed: {item.error}</p>}
      </div>
      <div>
        <div className="card-h">Net avoidable loss vs exit cost over time</div>
        {series.length > 1 ? (
          <div className="monitor-chart">
            <ResponsiveContainer>
              <LineChart data={series} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
                <XAxis dataKey="t" type="number" domain={["dataMin", "dataMax"]} stroke="#817b73" fontSize={10}
                  tickFormatter={(v) => new Date(v).toLocaleDateString()} />
                <YAxis stroke="#817b73" fontSize={10} tickFormatter={(v) => pct(v, 1)} />
                <Tooltip labelFormatter={(v: number) => new Date(v).toLocaleString()} formatter={(v: number) => pct(v, 2)}
                  contentStyle={{ background: "#fff", border: "1px solid #e4e0d9", borderRadius: 5, fontSize: 11 }} />
                <Line dataKey="net" name="Net avoidable loss" stroke="#a46a5e" dot={false} strokeWidth={2} />
                <Line dataKey="exit" name="Exit cost" stroke="#8d733d" dot={false} strokeDasharray="4 3" />
              </LineChart>
            </ResponsiveContainer>
          </div>
        ) : (
          <p className="overview-note">{runs === null ? "Loading history…" : "A trend appears after two recorded runs."}</p>
        )}
        <div className="card-h">Recent notifications</div>
        {events.length ? (
          <ul className="assume">
            {events.map((e, i) => (
              <li key={i}>
                [{e.severity}] {e.message}
                <small>{when(e.ts)}</small>
              </li>
            ))}
          </ul>
        ) : (
          <p className="overview-note">No verdict changes or break-even crossings yet.</p>
        )}
      </div>
    </div>
  );
}

export default function MonitorPanel({
  open,
  current,
  onClose,
}: {
  open: boolean;
  current: Analysis | null;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [schedule, setSchedule] = useState<Schedule | null>(null);
  const [list, setList] = useState<Watchlist | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [busy, setBusy] = useState<"run" | "add" | "schedule" | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  const load = useCallback(async () => {
    try {
      const [s, w] = await Promise.all([api.monitor.schedule(), api.monitor.watchlist()]);
      setSchedule(s);
      setList(w);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Monitor unavailable.");
    }
  }, []);
  useEffect(() => {
    if (!open) return;
    void load();
    const timer = setInterval(load, schedule?.running ? 5_000 : 30_000);
    return () => clearInterval(timer);
  }, [open, load, schedule?.running]);
  const act = async (kind: "run" | "add" | "schedule", fn: () => Promise<unknown>) => {
    setBusy(kind);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      setBusy(null);
    }
  };
  const addCurrent = () =>
    current &&
    act("add", () =>
      api.monitor.watch({
        ...current.request,
        horizon_days: current.screening.horizon_days,
        label: investmentLabel(current),
      }),
    );
  const currentKey = current && list?.items.find(
    (i) =>
      i.address.toLowerCase() === current.request.address.toLowerCase() &&
      i.chain_id === current.request.chain_id &&
      i.size_usd === current.request.size_usd &&
      (i.horizon_days ?? 30) === current.screening.horizon_days,
  );
  const saved = list?.source === "saved";
  return (
    <dialog ref={dialog} className="position-dialog overview-dialog" onCancel={onClose} onClose={onClose}>
      <div className="dialog-header">
        <div>
          <span className="eyebrow">Live monitoring</span>
          <h2>Scheduled STAY / EXIT</h2>
        </div>
        <button onClick={onClose} aria-label="Close monitoring">
          <X size={18} />
        </button>
      </div>
      <div className="overview-content">
        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button aria-label="Dismiss error" onClick={() => setError(null)}>
              <X size={15} />
            </button>
          </div>
        )}
        <div className="monitor-controls">
          <label>
            Schedule{" "}
            <select
              value={schedule?.interval_s ?? 0}
              disabled={!schedule || busy !== null}
              onChange={(e) => void act("schedule", () => api.monitor.setSchedule(Number(e.target.value)))}
            >
              {schedule && !INTERVALS.some(([v]) => v === schedule.interval_s) && (
                <option value={schedule.interval_s}>{intervalLabel(schedule.interval_s)}</option>
              )}
              {INTERVALS.map(([v, label]) => (
                <option key={v} value={v}>{label}</option>
              ))}
            </select>
          </label>
          <button
            className="btn subtle"
            disabled={busy !== null || schedule?.running || !list?.items.length}
            onClick={() => void act("run", api.monitor.run)}
          >
            <Play size={14} /> {busy === "run" || schedule?.running ? "Screening…" : "Run now"}
          </button>
          {current && (
            <button
              className="btn subtle"
              disabled={busy !== null || (saved && !!currentKey)}
              onClick={() => void addCurrent()}
              title={saved && currentKey ? "Already monitored" : undefined}
            >
              <Plus size={14} /> {saved && currentKey ? "Current investment monitored" : `Monitor ${investmentLabel(current)}`}
            </button>
          )}
          <span className="overview-note">
            {schedule?.running
              ? "Screening the watchlist with live data…"
              : `Last run ${when(schedule?.last_run)}${schedule?.next_run ? ` · next ${when(schedule.next_run)}` : schedule?.interval_s === 0 ? " · schedule off" : ""}`}
          </span>
        </div>
        {list && !saved && (
          <p className="overview-note">
            No investments selected yet, so the schedule screens{" "}
            {list.source === "file" ? "the WATCHLIST file" : "the built-in examples"}. Add one to monitor only your selection.
          </p>
        )}
        <div className="table-scroll monitor-table">
          <table className="workspace-table">
            <thead>
              <tr>
                <th>Investment</th>
                <th>Verdict</th>
                <th>Net avoidable loss vs exit</th>
                <th>Alerts</th>
                <th>Since last run</th>
                <th>Screened</th>
                {saved && <th />}
              </tr>
            </thead>
            <tbody>
              {list?.items.map((item) => {
                const l = item.latest,
                  rowKey = item.position_key ?? item.address,
                  isOpen = expanded === rowKey;
                return (
                  <Fragment key={rowKey}>
                    <tr className={isOpen ? "current-row" : ""}>
                      <td>
                        <button className="history-pick" onClick={() => setExpanded(isOpen ? null : rowKey)}>
                          {isOpen ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                          {item.label || short(item.address)}
                        </button>
                        <small>
                          {item.kind === "market" ? "Morpho market" : item.kind === "borrow" ? "Morpho borrow" : "Held PT"} · {CHAINS[item.chain_id] ?? item.chain_id} ·{" "}
                          {usd(item.size_usd)} · {item.horizon_days ?? 30}-day horizon
                        </small>
                      </td>
                      <td>
                        {l ? (
                          <span className={`decision-tag ${l.verdict.toLowerCase()}`}>{l.verdict}</span>
                        ) : item.error ? (
                          <span className="decision-tag review">FAILED</span>
                        ) : (
                          "Pending"
                        )}
                      </td>
                      <td>
                        {l ? (
                          <>
                            {usd(netLoss(l) * l.size_usd)} vs {usd(l.exit_cost_pct * l.size_usd)}
                            <small>{pct(netLoss(l), 2)} vs {pct(l.exit_cost_pct, 2)}</small>
                          </>
                        ) : "–"}
                      </td>
                      <td>
                        {l ? (
                          <>
                            {l.max_alert === "none" ? "None" : l.max_alert}
                            <small>{l.drivers.find((d) => d.alert !== "none")?.label ?? `Contagion ×${(l.contagion ?? 1).toFixed(2)}`}</small>
                          </>
                        ) : "–"}
                      </td>
                      <td>
                        <Trend item={item} />
                      </td>
                      <td>{l ? when(l.ts) : "–"}</td>
                      {saved && (
                        <td>
                          {item.id !== null && (
                            <button
                              className="icon-button"
                              aria-label={`Stop monitoring ${item.label ?? item.address}`}
                              title="Stop monitoring"
                              disabled={busy !== null}
                              onClick={() => void act("add", () => api.monitor.unwatch(item.id!))}
                            >
                              <Trash2 size={14} />
                            </button>
                          )}
                        </td>
                      )}
                    </tr>
                    {isOpen && (
                      <tr>
                        <td colSpan={saved ? 7 : 6}>
                          <Details item={item} />
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="overview-note">
          Each run discovers the dependencies again with live data, applies the monitoring signals and records the verdict.
          EXIT means the expected loss you can still avoid by withdrawing now, net of the yield you give up, exceeds the cost of exiting. The model sets
          every number; AI summaries are only written when you ask for one (open a row, or a scenario result).
        </p>
      </div>
    </dialog>
  );
}

function Trend({ item }: { item: WatchItem }) {
  const [prev, setPrev] = useState<MonitorRun | null>(null);
  const key = item.position_key,
    ts = item.latest?.ts;
  useEffect(() => {
    if (!key || !ts) return;
    let stale = false;
    api.monitor
      .history(key, 2)
      .then((runs) => !stale && setPrev(runs.length > 1 ? runs[0] : null))
      .catch(() => {});
    return () => {
      stale = true;
    };
  }, [key, ts]);
  const l = item.latest;
  if (!l || !prev) return <>–</>;
  const delta = netLoss(l) - netLoss(prev);
  return (
    <>
      {prev.verdict !== l.verdict ? `${prev.verdict} → ${l.verdict}` : "No change"}
      <small>
        Net loss {delta >= 0 ? "+" : ""}
        {pct(delta, 2)}
      </small>
    </>
  );
}
