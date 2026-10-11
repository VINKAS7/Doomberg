import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ArrowRight,
  Check,
  ChevronRight,
  CircleHelp,
  GitBranch,
  Plus,
  Shield,
  X,
  FlaskConical,
  FileText,
  RefreshCw,
  LayoutList,
  Activity,
} from "lucide-react";
import { api, ApiError } from "../api";
import type { Analysis, Catalog, ShockResult } from "../types";
import Graph, { initialGraphUi, type ColorMode, type GraphUi } from "./Graph";
import PositionForm from "./PositionForm";
import TopRisks from "./TopRisks";
import Inspector from "./Inspector";
import Answer from "./Answer";
import { CHAINS, pct, usd } from "../format";
import {
  ROLE_LABELS,
  newWorkspaceId,
  samePosition,
  type PositionRole,
  type ScenarioRecord,
} from "../ui/workspace";
import Freshness from "./Freshness";
import ScenarioHistory from "./ScenarioHistory";
import LeverageCard from "./LeverageCard";

type View = "dependencies" | "scenarios" | "results";
export default function InvestmentWorkspace({
  initialAnalysis,
  catalog,
  service,
  active,
  investmentTabs,
  onAdd,
  onAnalysis,
  onBusy,
  role,
  refreshedAt,
  needsRefresh,
  history,
  onScenario,
  onInvalid,
  onOverview,
  onMonitor,
  saveError,
  notice,
  onDismissNotice,
}: {
  initialAnalysis: Analysis | null;
  catalog: Catalog | null;
  service: string;
  active: boolean;
  investmentTabs: ReactNode;
  onAdd: () => void;
  onAnalysis: (
    analysis: Analysis,
    role: PositionRole,
    refreshedAt: string,
  ) => void;
  onBusy: (busy: boolean) => void;
  role: PositionRole;
  refreshedAt: string;
  needsRefresh: boolean;
  history: ScenarioRecord[];
  onScenario: (record: ScenarioRecord) => void;
  onInvalid: () => void;
  onOverview: () => void;
  onMonitor: () => void;
  saveError: string | null;
  notice: string | null;
  onDismissNotice: () => void;
}) {
  const [analysis, setAnalysis] = useState<Analysis | null>(initialAnalysis);
  const [record, setRecord] = useState<ScenarioRecord | null>(
    history[0] ?? null,
  );
  const result = record?.result ?? null;
  const [selected, setSelected] = useState<string | null>(null);
  const [preMode, setPreMode] = useState<string | null>(null);
  const [busy, setBusy] = useState<"analyze" | "shock" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [colorMode, setColorMode] = useState<ColorMode>("type");
  const [view, setView] = useState<View>("dependencies");
  const [positionOpen, setPositionOpen] = useState(false);
  const requestBusy = useRef(false);
  const restoreAttempted = useRef(false);
  const [graphUi, setGraphUi] = useState<GraphUi>(initialGraphUi);
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    onBusy(!!busy);
  }, [busy, onBusy]);
  useEffect(() => {
    if (!active) setPositionOpen(false);
  }, [active]);
  useEffect(() => {
    if (positionOpen) dialog.current?.showModal();
    else dialog.current?.close();
  }, [positionOpen]);
  const analyze = async (
    req: Parameters<typeof api.analyze>[0],
    nextRole: PositionRole = role,
  ) => {
    if (requestBusy.current) return;
    requestBusy.current = true;
    setBusy("analyze");
    setError(null);
    try {
      const next = await api.analyze(req);
      // Saved runs for a different market or token don't apply to this one;
      // the reducer clears this tab's history for the same reason.
      if (analysis && !samePosition(analysis.request, next.request))
        setRecord(null);
      setAnalysis(next);
      onAnalysis(next, nextRole, new Date().toISOString());
      setSelected(null);
      setPreMode(null);
      setView("dependencies");
      setGraphUi(initialGraphUi);
      setPositionOpen(false);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setBusy(null);
      requestBusy.current = false;
    }
  };
  const refresh = () =>
    analysis &&
    analyze({
      ...analysis.request,
      label: analysis.request.label ?? undefined,
      horizon_days: analysis.screening.horizon_days,
    });
  useEffect(() => {
    if (
      active &&
      needsRefresh &&
      !busy &&
      service === "API online" &&
      !restoreAttempted.current
    ) {
      restoreAttempted.current = true;
      void refresh();
    }
  }, [active, needsRefresh, service, busy]);
  const remember = (r: ShockResult) => {
    if (!analysis) return;
    const next: ScenarioRecord = {
      id: newWorkspaceId(),
      createdAt: new Date().toISOString(),
      analysisId: analysis.position_id,
      refreshedAt,
      request: { ...analysis.request },
      horizonDays: r.probability.horizon_days,
      result: r,
    };
    setRecord(next);
    onScenario(next);
    setView("results");
  };
  const reportError = (e: unknown) => {
    if (
      e instanceof ApiError &&
      e.status === 404 &&
      e.message.includes("position_id")
    ) {
      onInvalid();
      setError(
        "The backend analysis expired. Refresh this investment before running a new scenario.",
      );
    } else setError(e instanceof Error ? e.message : "Request failed.");
  };
  const shock = async (r: {
    node_id: string;
    mode: string;
    alert: string;
    probability_override: number | null;
    exit_cost_override: number | null;
  }) => {
    if (!analysis || needsRefresh || requestBusy.current) return;
    requestBusy.current = true;
    setBusy("shock");
    setError(null);
    try {
      remember(await api.shock({ position_id: analysis.position_id, ...r }));
    } catch (e: any) {
      reportError(e);
    } finally {
      setBusy(null);
      requestBusy.current = false;
    }
  };
  const replay = async (
    incident_id: string,
    exit_cost_override: number | null,
  ) => {
    if (!analysis || needsRefresh || requestBusy.current) return;
    requestBusy.current = true;
    setBusy("shock");
    setError(null);
    try {
      const r = await api.replay({
        position_id: analysis.position_id,
        incident_id,
        exit_cost_override,
      });
      remember(r);
      setSelected(r.shock.node_id);
    } catch (e: any) {
      reportError(e);
    } finally {
      setBusy(null);
      requestBusy.current = false;
    }
  };
  const pt = analysis?.graph.nodes.find(
    (n) => n.type === "asset" && n.attrs.kind === "pt",
  );
  const market = analysis?.graph.nodes.find((n) => n.type === "market");
  const graphResult =
    record?.analysisId === analysis?.position_id && !needsRefresh
      ? result
      : null;
  const navigate = (next: View) => {
    if (next === "results" && !record) setRecord(history[0] ?? null);
    setView(next);
  };
  const choose = (id: string | null) => {
    setSelected(id);
    setPreMode(null);
  };
  const tabs: { id: View; label: string; icon: typeof GitBranch }[] = [
    { id: "dependencies", label: "Dependencies", icon: GitBranch },
    { id: "scenarios", label: "Scenarios", icon: FlaskConical },
    { id: "results", label: "Results", icon: FileText },
  ];
  return (
    <div className="app">
      <aside className="workspace-nav">
        <div className="workspace-brand">
          <span className="brand-mark">D.</span>
          <div>
            <b>Doomberg Terminal</b>
            <small>Belem Capital</small>
          </div>
        </div>
        <span className="nav-caption">Risk workspace</span>
        <nav aria-label="Workspace">
          {tabs.map((t) => (
            <button
              key={t.id}
              className={`nav-item ${view === t.id ? "active" : ""}`}
              disabled={t.id === "results" && !result && !history.length}
              onClick={() => navigate(t.id)}
            >
              <t.icon size={16} />
              {t.label}
              {t.id === "results" && result && <span className="nav-dot" />}
            </button>
          ))}
        </nav>
        {!analysis && (
          <button className="new-position" onClick={onAdd}>
            <Plus size={15} /> Add investment
          </button>
        )}
        {analysis && (
          <div className="nav-risk">
            <TopRisks
              analysis={analysis}
              onPick={(n, m) => {
                setSelected(n);
                setPreMode(m);
                setView("scenarios");
              }}
            />
          </div>
        )}
        {analysis && (
          <button className="nav-item overview-link" onClick={onOverview}>
            <LayoutList size={16} />
            Investment overview
          </button>
        )}
        <button className="nav-item overview-link" onClick={onMonitor}>
          <Activity size={16} />
          Scheduled monitoring
        </button>
        {investmentTabs}
        <div className="nav-footer">
          <Shield size={15} />
          <span>
            Belem Capital
            <small>
              {saveError
                ? "Workspace not saved"
                : "Workspace saved in this browser"}
            </small>
          </span>
        </div>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <span>
            Belem Capital <ChevronRight size={13} /> Risk workspace{" "}
            <ChevronRight size={13} />{" "}
            <b>{tabs.find((t) => t.id === view)?.label}</b>
          </span>
          <span
            title="API connectivity only; this does not indicate oracle health or live monitoring."
            className={`service-state ${service === "API online" ? "connected" : "offline"}`}
          >
            <i />
            {service}
          </span>
        </header>
        {saveError && (
          <div className="error-banner" role="status">
            {saveError}
          </div>
        )}
        {notice && !saveError && (
          <div className="error-banner" role="status">
            {notice}
            <button aria-label="Dismiss notice" onClick={onDismissNotice}>
              <X size={15} />
            </button>
          </div>
        )}
        {error && !positionOpen && (
          <div className="error-banner" role="alert">
            {error}
            <button aria-label="Dismiss error" onClick={() => setError(null)}>
              <X size={15} />
            </button>
          </div>
        )}
        {!analysis ? (
          <div className="welcome">
            <div className="welcome-icon">
              <GitBranch size={30} strokeWidth={1.5} />
            </div>
            <span className="eyebrow">Belem Capital / Investment research</span>
            <h1>
              Understand what your
              <br />
              investment depends on.
            </h1>
            <p>
              Trace a Pendle PT position. Explore its risks. Compare staying
              with exiting.
            </p>
            <button className="btn primary welcome-button" onClick={onAdd}>
              <Plus size={16} /> Add investment <ArrowRight size={15} />
            </button>
            <div className="welcome-steps">
              <span>
                <GitBranch size={17} />
                Trace dependencies
              </span>
              <span>
                <FlaskConical size={17} />
                Test a scenario
              </span>
              <span>
                <Check size={17} />
                Review the result
              </span>
            </div>
            <span className="welcome-note">
              Live discovery · Modeled scenarios · No trades executed
            </span>
          </div>
        ) : (
          <>
            <div className="page-heading">
              <div>
                <span className="eyebrow">
                  {analysis.request.kind === "market"
                    ? "Morpho lending position"
                    : analysis.request.kind === "borrow"
                      ? "Morpho borrow position"
                      : "Pendle PT position"}{" "}
                  / {CHAINS[analysis.request.chain_id]}
                </span>
                <h1>
                  {(analysis.request.kind === "borrow"
                    ? analysis.graph.nodes.find((n) => n.id === analysis.graph.root)?.label
                    : null) ||
                    market?.label ||
                    pt?.label ||
                    "Investment analysis"}
                </h1>
                <span className="heading-meta">
                  {analysis.screening.horizon_days}-day horizon
                  {pt?.attrs.expiry ? ` · Matures ${pt.attrs.expiry}` : ""} ·
                  Last refreshed {new Date(refreshedAt).toLocaleString()}
                </span>
                <div className="snapshot-meta">
                  <span className="position-role">
                    {ROLE_LABELS[role]} · user-entered size
                  </span>
                  <Freshness
                    refreshedAt={refreshedAt}
                    needsRefresh={needsRefresh}
                  />
                </div>
              </div>
              <div className="heading-actions">
                <button
                  className="btn subtle"
                  onClick={() => void refresh()}
                  disabled={!!busy}
                >
                  <RefreshCw
                    size={14}
                    className={busy === "analyze" ? "spin" : ""}
                  />
                  {busy === "analyze" ? "Refreshing…" : "Refresh investment"}
                </button>
                <button
                  className="btn subtle"
                  onClick={() => setPositionOpen(true)}
                  disabled={!!busy}
                >
                  Change investment
                </button>
              </div>
            </div>
            <section className="summary-strip" aria-label="Investment summary">
              <div>
                <small>
                  {analysis.request.kind === "borrow" ? "Equity at risk" : "Entered position size"}
                </small>
                <b>{usd(analysis.request.size_usd)}</b>
              </div>
              <div title={exitTitle(analysis)}>
                <small>
                  {analysis.request.kind === "market"
                    ? "Withdrawable now"
                    : analysis.request.kind === "borrow"
                      ? "Unwindable now"
                      : "Sellable now"}
                </small>
                <b>{pct(analysis.exit_now.instant_fraction, 0)}</b>
                <small className="tile-note">{exitNote(analysis)}</small>
              </div>
              <div>
                <small>Baseline modeled loss</small>
                <b>
                  {usd(
                    analysis.screening.total_expected_loss_pct *
                      analysis.request.size_usd,
                  )}
                </b>
              </div>
              <div title="Components are the protocols, tokens, oracles, markets and chains the position depends on (the position itself is not counted). Links are the relationships between them; there are more links than components because several components share a dependency, e.g. most of them run on the same chain.">
                <small>Dependencies</small>
                <b>
                  {analysis.stats.nodes - 1}
                  <span> components</span>
                </b>
                <small className="tile-note">
                  {analysis.stats.edges} links · {analysis.stats.depth} layers deep
                </small>
              </div>
            </section>
            {analysis.leverage && (
              <LeverageCard lev={analysis.leverage} horizon={analysis.screening.horizon_days} />
            )}
            <details className="mobile-contributors">
              <summary>Modeled risk contributors</summary>
              <TopRisks
                analysis={analysis}
                onPick={(n, m) => {
                  setSelected(n);
                  setPreMode(m);
                  setView("scenarios");
                }}
              />
            </details>
            {needsRefresh && (
              <div className="snapshot-notice" role="status">
                Saved snapshot.{" "}
                {busy === "analyze"
                  ? "Fetching a fresh analysis…"
                  : "Refresh required before running new scenarios."}
              </div>
            )}
            {view === "results" && result ? (
              <>
                <ScenarioHistory
                  history={history}
                  selectedId={record?.id ?? null}
                  analysisId={analysis.position_id}
                  onSelect={(saved) => {
                    setRecord(saved);
                    setSelected(
                      analysis.graph.nodes.some(
                        (n) => n.id === saved.result.shock.node_id,
                      )
                        ? saved.result.shock.node_id
                        : null,
                    );
                  }}
                />
                <div className="result-snapshot-note">
                  Saved scenario ·{" "}
                  {new Date(record!.createdAt).toLocaleString()} · Analysis
                  snapshot {new Date(record!.refreshedAt).toLocaleString()}
                  {record?.analysisId !== analysis.position_id || needsRefresh
                    ? " · Earlier snapshot; not a current recommendation"
                    : ""}
                </div>
                <Answer
                  r={result}
                  onClose={() => {
                    setRecord(null);
                    setView("dependencies");
                  }}
                  onInspectPath={() => setView("scenarios")}
                  canInspectPath={!!graphResult}
                />
              </>
            ) : (
              <div
                className={`research-area ${selected || view === "scenarios" ? "with-inspector" : ""}`}
              >
                <section className="graph-panel" aria-label="Dependency graph">
                  <div className="graph-heading">
                    <div>
                      <b>
                        {view === "scenarios"
                          ? "Scenario workspace"
                          : "Dependency map"}
                      </b>
                      <small>
                        {view === "scenarios"
                          ? "Select a component to test a failure."
                          : "Select a component. Expand groups for more detail."}
                      </small>
                    </div>
                    <select
                      aria-label="Graph colour"
                      value={colorMode}
                      onChange={(e) =>
                        setColorMode(e.target.value as ColorMode)
                      }
                    >
                      <option value="type">Category colours</option>
                      <option value="risk">Modeled risk contribution</option>
                    </select>
                  </div>
                  <Graph
                    analysis={analysis}
                    result={graphResult}
                    selected={selected}
                    onSelect={choose}
                    colorMode={colorMode}
                    active={active}
                    ui={graphUi}
                    onUi={setGraphUi}
                  />
                  {busy === "shock" && (
                    <div className="overlay" role="status">
                      Calculating scenario…
                    </div>
                  )}
                </section>
                <aside
                  className="right"
                  hidden={!selected && view !== "scenarios"}
                >
                  <div className="inspector-toolbar">
                    <b>
                      {view === "scenarios"
                        ? "Run scenario"
                        : "Component details"}
                    </b>
                    {selected && (
                      <button
                        aria-label="Close component"
                        onClick={() => choose(null)}
                      >
                        <X size={16} />
                      </button>
                    )}
                  </div>
                  <Inspector
                    analysis={analysis}
                    catalog={catalog}
                    selected={selected}
                    preMode={preMode}
                    busy={busy === "shock"}
                    blocked={needsRefresh || busy === "analyze"}
                    onShock={shock}
                    onReplay={replay}
                    scenarioView={view === "scenarios"}
                    onScenario={() => setView("scenarios")}
                  />
                </aside>
              </div>
            )}
            <details className="analysis-details">
              <summary>
                <CircleHelp size={14} /> Analysis details & discovery notes
              </summary>
              <div>
                {analysis.stats.evidence} evidence records · Discovery took{" "}
                {analysis.stats.discovery_seconds}s · Model probabilities are
                assumptions, not observed incidents.
                {analysis.stats.warnings.map((w, i) => (
                  <p key={i}>{w}</p>
                ))}
                <p>
                  Unavailable or inferred dependencies require verification.
                  This workspace evaluates scenarios and never executes trades.
                  Recurring STAY / EXIT checks run from Scheduled monitoring.
                </p>
              </div>
            </details>
          </>
        )}
      </main>
      <dialog
        ref={dialog}
        className="position-dialog"
        onCancel={(event) => {
          if (busy === "analyze") event.preventDefault();
          else setPositionOpen(false);
        }}
        onClose={() => setPositionOpen(false)}
      >
        <div className="dialog-header">
          <div>
            <span className="eyebrow">Investment research</span>
            <h2>{analysis ? "Change investment" : "Add investment"}</h2>
          </div>
          <button
            aria-label="Close investment form"
            disabled={busy === "analyze"}
            onClick={() => setPositionOpen(false)}
          >
            <X size={18} />
          </button>
        </div>
        {error && (
          <div className="error-banner" role="alert">
            {error}
            <button aria-label="Dismiss error" onClick={() => setError(null)}>
              <X size={15} />
            </button>
          </div>
        )}
        {positionOpen && (
          <PositionForm
            onAnalyze={analyze}
            initialRole={role}
            key={analysis?.position_id ?? "empty"}
            busy={busy === "analyze"}
            initial={
              analysis
                ? {
                    ...analysis.request,
                    label: analysis.request.label ?? undefined,
                    horizon_days: analysis.screening.horizon_days,
                  }
                : undefined
            }
          />
        )}
      </dialog>
    </div>
  );
}

// The share of the position that can leave right now, in calm markets. A held
// PT or a borrow unwind is all-or-nothing: 100% when a complete sale route was
// confirmed (its slippage is the exit cost), 0% otherwise. A market supply is
// capped by the market's idle liquidity.
function exitNote(a: Analysis): string {
  const e = a.exit_now;
  if (a.request.kind === "market") {
    const liq = e.liquidity_usd ?? 0;
    return `${usd(liq)} idle (${(liq / a.request.size_usd).toFixed(1)}× size) · ${pct(e.instant_fraction_run ?? 0, 0)} in a lender run`;
  }
  return e.instant_fraction > 0
    ? `Full sale route confirmed · ${pct(e.cost_pct, 2)} exit cost`
    : "No confirmed sale route into USDC/USDT";
}
function exitTitle(a: Analysis): string {
  return a.request.kind === "market"
    ? "Share of the supplied amount that can be withdrawn immediately: the market's idle (unborrowed) liquidity divided by the position size, capped at 100%. It reaches 100% whenever idle liquidity exceeds the position. The 'lender run' figure is the expected share left if other lenders withdraw first during an incident."
    : "Share of the position that can be exited immediately. For a held PT or a borrow unwind this is 100% when a complete sale route into USDC/USDT was confirmed (the price impact is charged as exit cost, not as a lower share) and 0% when no route was found.";
}
