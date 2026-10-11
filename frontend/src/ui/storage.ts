import type { Investment, InvestmentsState } from "./investments.ts";
import { emptyInvestments } from "./investments.ts";
import { HISTORY_LIMIT, type ScenarioRecord } from "./workspace.ts";

export const STORAGE_KEY = "doomberg.workspace.v1";
// A copy of saved data that could not be fully restored, kept for recovery.
export const BACKUP_KEY = `${STORAGE_KEY}.backup`;
// Investments closed in this browser, so other open tabs drop them on merge.
const CLOSED_LIMIT = 100;

export type SavedWorkspace = {
  state: InvestmentsState;
  closed: string[];
  // Investments and scenario runs that failed validation and were left out.
  skipped: number;
};

export function serializeWorkspace(
  state: InvestmentsState,
  closed: string[] = [],
): string {
  return JSON.stringify({
    version: 1,
    activeId: state.activeId,
    items: state.items.map(({ busy, needsRefresh, ...item }) => item),
    closed: closed.slice(-CLOSED_LIMIT),
  });
}

function validInvestment(item: Investment): boolean {
  const a = item?.analysis;
  return !(
    typeof item.id !== "string" ||
    !["example", "entered", "holding"].includes(item.role) ||
    !Number.isFinite(Date.parse(item.refreshedAt)) ||
    !a ||
    typeof a.position_id !== "string" ||
    !a.request ||
    !["market", "token", "borrow"].includes(a.request.kind) ||
    typeof a.request.address !== "string" ||
    !Number.isFinite(a.request.size_usd) ||
    a.request.size_usd <= 0 ||
    !Number.isFinite(a.request.book_usd) ||
    a.request.book_usd <= 0 ||
    !Number.isFinite(a.request.chain_id) ||
    !a.screening ||
    !a.exit_now ||
    !a.stats ||
    !Number.isFinite(a.screening.horizon_days) ||
    a.screening.horizon_days <= 0 ||
    !Array.isArray(a.screening.top) ||
    !Array.isArray(a.stats.warnings) ||
    !Array.isArray(a.graph?.nodes) ||
    !Array.isArray(a.graph?.edges) ||
    !Array.isArray(item.history) ||
    a.graph.nodes.some(
      (node) =>
        typeof node?.id !== "string" ||
        !node.attrs ||
        !node.risk_factors ||
        !Array.isArray(node.evidence) ||
        !Array.isArray(node.modes),
    )
  );
}

function validRecord(record: ScenarioRecord): boolean {
  return !(
    !record ||
    typeof record.id !== "string" ||
    !Number.isFinite(Date.parse(record.createdAt)) ||
    !Number.isFinite(Date.parse(record.refreshedAt)) ||
    typeof record.analysisId !== "string" ||
    !record.request ||
    typeof record.request.address !== "string" ||
    !Number.isFinite(record.horizonDays) ||
    !record.result?.decision ||
    !record.result?.shock ||
    !record.result?.position ||
    !record.result?.probability ||
    !record.result?.exit ||
    !record.result?.loss ||
    !Array.isArray(record.result.paths) ||
    !Array.isArray(record.result.loss.histogram) ||
    !Array.isArray(record.result.exit.steps) ||
    !Array.isArray(record.result.decision.reasons) ||
    !Array.isArray(record.result.decision.break_even_table) ||
    !Array.isArray(record.result.assumptions) ||
    !Array.isArray(record.result.findings)
  );
}

// Throws only when the data is unreadable as a whole. Individual investments
// or scenario runs that fail validation are skipped so the rest still load.
export function parseWorkspace(raw: string | null): SavedWorkspace {
  if (!raw) return { state: emptyInvestments, closed: [], skipped: 0 };
  const saved = JSON.parse(raw);
  if (saved?.version !== 1 || !Array.isArray(saved.items))
    throw new Error("Unsupported saved workspace.");
  const ids = new Set<string>();
  let skipped = 0;
  const items: Investment[] = [];
  for (const item of saved.items as Investment[]) {
    if (!item || !validInvestment(item) || ids.has(item.id)) {
      skipped++;
      continue;
    }
    ids.add(item.id);
    const history = item.history.filter(validRecord);
    skipped += item.history.length - history.length;
    // Server analysis IDs cannot be trusted across reloads or process restarts.
    items.push({
      ...item,
      busy: false,
      needsRefresh: true,
      history: history.slice(0, HISTORY_LIMIT),
    });
  }
  return {
    state: {
      items,
      activeId: items.some((item) => item.id === saved.activeId)
        ? saved.activeId
        : (items[0]?.id ?? null),
    },
    closed: Array.isArray(saved.closed)
      ? saved.closed.filter((id: unknown) => typeof id === "string")
      : [],
    skipped,
  };
}

// Brings back investments and scenario runs that an earlier load left out but
// that now validate (e.g. borrow positions, which older builds rejected).
// Investments closed since then stay closed. `remaining` counts records the
// backup still holds that cannot be restored.
export function mergeBackup(
  saved: SavedWorkspace,
  backupRaw: string | null,
): { saved: SavedWorkspace; recovered: number; remaining: number } {
  let backup: SavedWorkspace;
  try {
    backup = parseWorkspace(backupRaw);
  } catch {
    return { saved, recovered: 0, remaining: 1 };
  }
  const closed = new Set(saved.closed);
  const byId = new Map(saved.state.items.map((item) => [item.id, item]));
  let recovered = 0;
  const items = saved.state.items.map((item) => {
    const old = backup.state.items.find((b) => b.id === item.id);
    if (!old) return item;
    const have = new Set(item.history.map((r) => r.id));
    const extra = old.history.filter((r) => !have.has(r.id));
    if (!extra.length) return item;
    recovered += extra.length;
    const history = [...item.history, ...extra]
      .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
      .slice(0, HISTORY_LIMIT);
    return { ...item, history };
  });
  for (const item of backup.state.items) {
    if (byId.has(item.id) || closed.has(item.id)) continue;
    items.push(item);
    recovered++;
  }
  return {
    saved: {
      ...saved,
      state: { items, activeId: saved.state.activeId ?? items[0]?.id ?? null },
    },
    recovered,
    remaining: backup.skipped,
  };
}

export function restoreWorkspace(raw: string | null): InvestmentsState {
  return parseWorkspace(raw).state;
}
