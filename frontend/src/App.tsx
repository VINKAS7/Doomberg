import {
  memo,
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
} from "react";
import { X } from "lucide-react";
import { api } from "./api";
import type { Analysis, Catalog } from "./types";
import PositionForm from "./components/PositionForm";
import InvestmentWorkspace from "./components/InvestmentWorkspace";
import InvestmentTabs from "./components/InvestmentTabs";
import InvestmentOverview from "./components/InvestmentOverview";
import MonitorPanel from "./components/MonitorPanel";
import {
  BACKUP_KEY,
  mergeBackup,
  parseWorkspace,
  serializeWorkspace,
  STORAGE_KEY,
} from "./ui/storage";
import {
  newWorkspaceId,
  type PositionRole,
  type ScenarioRecord,
} from "./ui/workspace";
import {
  emptyInvestments,
  investmentsReducer,
  type Investment,
  type InvestmentsAction,
  type InvestmentsState,
} from "./ui/investments";

// Keep workspaces mounted when switching tabs, preserving graph controls,
// scenario inputs and results. Each workspace owns its own async requests.
// Only the active workspace gets the tab list, so its ids stay unique and
// memo skips re-rendering hidden workspaces when another tab changes.
const WorkspaceTab = memo(function WorkspaceTab({
  item,
  active,
  catalog,
  service,
  tabs,
  onAdd,
  dispatch,
  onOverview,
  onMonitor,
  saveError,
  notice,
  onDismissNotice,
}: {
  item: Investment;
  active: boolean;
  catalog: Catalog | null;
  service: string;
  tabs: ReactNode;
  onAdd: () => void;
  dispatch: Dispatch<InvestmentsAction>;
  onOverview: () => void;
  onMonitor: () => void;
  saveError: string | null;
  notice: string | null;
  onDismissNotice: () => void;
}) {
  const onAnalysis = useCallback(
    (analysis: Analysis, role: PositionRole, refreshedAt: string) =>
      dispatch({ type: "analysis", id: item.id, analysis, role, refreshedAt }),
    [dispatch, item.id],
  );
  const onScenario = useCallback(
    (record: ScenarioRecord) =>
      dispatch({ type: "scenario", id: item.id, record }),
    [dispatch, item.id],
  );
  const onInvalid = useCallback(
    () => dispatch({ type: "invalid", id: item.id }),
    [dispatch, item.id],
  );
  const onBusy = useCallback(
    (busy: boolean) => dispatch({ type: "busy", id: item.id, busy }),
    [dispatch, item.id],
  );
  return (
    <div
      role="tabpanel"
      id={`investment-panel-${item.id}`}
      aria-labelledby={`investment-tab-${item.id}`}
      hidden={!active}
    >
      <InvestmentWorkspace
        initialAnalysis={item.analysis}
        active={active}
        catalog={catalog}
        service={service}
        investmentTabs={tabs}
        onAdd={onAdd}
        onAnalysis={onAnalysis}
        onBusy={onBusy}
        role={item.role}
        refreshedAt={item.refreshedAt}
        needsRefresh={item.needsRefresh}
        history={item.history}
        onScenario={onScenario}
        onInvalid={onInvalid}
        onOverview={onOverview}
        onMonitor={onMonitor}
        saveError={saveError}
        notice={notice}
        onDismissNotice={onDismissNotice}
      />
    </div>
  );
});

const noop = () => {};

function loadWorkspace() {
  let raw: string | null = null;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
    let saved = parseWorkspace(raw);
    // Records an earlier build left out are restored once they validate.
    const backupRaw = window.localStorage.getItem(BACKUP_KEY);
    let recovered = 0,
      remaining = 0;
    if (backupRaw) {
      ({ saved, recovered, remaining } = mergeBackup(saved, backupRaw));
    }
    // Don't overwrite a backup that still holds records nothing could restore.
    if (saved.skipped && !(backupRaw && remaining))
      window.localStorage.setItem(BACKUP_KEY, raw!);
    else if (backupRaw && !remaining && !saved.skipped)
      window.localStorage.removeItem(BACKUP_KEY);
    const plural = (n: number) => `record${n === 1 ? "" : "s"}`;
    return {
      ...saved,
      error: saved.skipped
        ? `${saved.skipped} saved investment or scenario ${plural(saved.skipped)} could not be restored and ${saved.skipped === 1 ? "was" : "were"} left out. The original data is kept in this browser under ${BACKUP_KEY}.`
        : recovered
          ? `Restored ${recovered} investment or scenario ${plural(recovered)} that an earlier version left out.`
          : null,
    };
  } catch {
    try {
      if (raw) window.localStorage.setItem(BACKUP_KEY, raw);
    } catch {
      /* storage unavailable */
    }
    return {
      state: emptyInvestments,
      closed: [],
      skipped: 0,
      error: raw
        ? `Saved workspace could not be restored. The original data is kept in this browser under ${BACKUP_KEY}.`
        : null,
    };
  }
}

// The parts of the workspace that are saved. Busy flags and selection-only
// renders don't change them, so they don't trigger a save.
function savedParts(state: InvestmentsState) {
  return [
    state.activeId,
    ...state.items.flatMap((i) => [
      i.id,
      i.analysis,
      i.history,
      i.role,
      i.refreshedAt,
    ]),
  ];
}

export default function App() {
  const [loaded] = useState(loadWorkspace);
  const [investments, dispatch] = useReducer(investmentsReducer, loaded.state);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [service, setService] = useState("Checking API");
  const [saveError, setSaveError] = useState<string | null>(null);
  // Kept apart from saveError, which every successful save clears.
  const [restoreNotice, setRestoreNotice] = useState(loaded.error);
  const onDismissNotice = useCallback(() => setRestoreNotice(null), []);
  const [overviewOpen, setOverviewOpen] = useState(false);
  const [monitorOpen, setMonitorOpen] = useState(false);
  const [positionOpen, setPositionOpen] = useState(false);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const addingRef = useRef(false);
  const onAdd = useCallback(() => {
    setError(null);
    setPositionOpen(true);
  }, []);
  const onOverview = useCallback(() => setOverviewOpen(true), []);
  const onMonitor = useCallback(() => setMonitorOpen(true), []);
  // Investments closed here, saved so other open tabs drop them on merge.
  const closed = useRef(new Set(loaded.closed));
  // Start from what was loaded, so mounting doesn't rewrite unchanged data.
  const savedRef = useRef(savedParts(loaded.state));
  useEffect(() => {
    const parts = savedParts(investments);
    if (
      parts.length === savedRef.current.length &&
      parts.every((part, i) => part === savedRef.current[i])
    )
      return;
    savedRef.current = parts;
    try {
      window.localStorage.setItem(
        STORAGE_KEY,
        serializeWorkspace(investments, [...closed.current]),
      );
      setSaveError(null);
    } catch {
      setSaveError(
        "Workspace changes could not be saved in this browser. Your open tabs still work; do not rely on reload restoring the latest runs.",
      );
    }
  }, [investments]);
  // Every tab saves the whole workspace to one key, so merge what other tabs
  // save instead of letting the last writer discard their investments.
  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== STORAGE_KEY || !event.newValue) return;
      try {
        const saved = parseWorkspace(event.newValue);
        saved.closed.forEach((id) => closed.current.add(id));
        dispatch({
          type: "merge",
          items: saved.state.items,
          closed: [...closed.current],
        });
      } catch {
        /* ignore data this tab cannot read */
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(() => {
    api
      .catalog()
      .then(setCatalog)
      .catch(() => {});
    let stopped = false;
    let controller: AbortController | null = null;
    const check = async () => {
      controller?.abort();
      const requestController = new AbortController();
      controller = requestController;
      const timeout = setTimeout(() => requestController.abort(), 5000);
      try {
        const response = await api.health(requestController.signal);
        if (!stopped && controller === requestController)
          setService(response.ok ? "API online" : "API offline");
      } catch {
        if (!stopped && controller === requestController)
          setService("API offline");
      } finally {
        clearTimeout(timeout);
      }
    };
    void check();
    const timer = setInterval(check, 15_000);
    window.addEventListener("focus", check);
    return () => {
      stopped = true;
      clearInterval(timer);
      controller?.abort();
      window.removeEventListener("focus", check);
    };
  }, []);
  useEffect(() => {
    if (positionOpen) dialog.current?.showModal();
    else dialog.current?.close();
  }, [positionOpen]);

  const addInvestment = async (
    request: Parameters<typeof api.analyze>[0],
    role: PositionRole,
  ) => {
    if (addingRef.current) return;
    addingRef.current = true;
    setAdding(true);
    setError(null);
    try {
      const analysis = await api.analyze(request);
      dispatch({ type: "open", id: newWorkspaceId(), analysis, role });
      setPositionOpen(false);
    } catch (error) {
      setError(
        error instanceof Error
          ? error.message
          : "Unable to analyse this investment.",
      );
    } finally {
      addingRef.current = false;
      setAdding(false);
    }
  };
  const tabs = (
    <InvestmentTabs
      items={investments.items}
      activeId={investments.activeId}
      onAdd={onAdd}
      onSelect={(id) => dispatch({ type: "select", id })}
      onClose={(id) => {
        closed.current.add(id);
        dispatch({ type: "close", id });
      }}
    />
  );
  return (
    <>
      {!investments.items.length && (
        <InvestmentWorkspace
          initialAnalysis={null}
          active
          catalog={catalog}
          service={service}
          investmentTabs={null}
          onAdd={onAdd}
          onAnalysis={noop}
          onBusy={noop}
          role="entered"
          refreshedAt=""
          needsRefresh={false}
          history={[]}
          onScenario={noop}
          onInvalid={noop}
          onOverview={onOverview}
          onMonitor={onMonitor}
          saveError={saveError}
          notice={restoreNotice}
          onDismissNotice={onDismissNotice}
        />
      )}
      {investments.items.map((item) => (
        <WorkspaceTab
          key={item.id}
          item={item}
          active={item.id === investments.activeId}
          catalog={catalog}
          service={service}
          tabs={item.id === investments.activeId ? tabs : null}
          onAdd={onAdd}
          dispatch={dispatch}
          onOverview={onOverview}
          onMonitor={onMonitor}
          saveError={saveError}
          notice={restoreNotice}
          onDismissNotice={onDismissNotice}
        />
      ))}
      <MonitorPanel
        open={monitorOpen}
        current={
          investments.items.find((i) => i.id === investments.activeId)
            ?.analysis ?? null
        }
        onClose={() => setMonitorOpen(false)}
      />
      <InvestmentOverview
        open={overviewOpen}
        items={investments.items}
        onClose={() => setOverviewOpen(false)}
        onSelect={(id) => {
          dispatch({ type: "select", id });
          setOverviewOpen(false);
        }}
      />
      <dialog
        ref={dialog}
        className="position-dialog"
        onCancel={(event) => {
          if (addingRef.current) event.preventDefault();
          else setPositionOpen(false);
        }}
        onClose={() => setPositionOpen(false)}
      >
        <div className="dialog-header">
          <div>
            <span className="eyebrow">Investment research</span>
            <h2>Add investment</h2>
          </div>
          <button
            aria-label="Close investment form"
            disabled={adding}
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
          <PositionForm onAnalyze={addInvestment} busy={adding} />
        )}
      </dialog>
    </>
  );
}
