import type { Analysis } from "../types.ts";
import {
  HISTORY_LIMIT,
  samePosition,
  type PositionRole,
  type ScenarioRecord,
} from "./workspace.ts";

export type Investment = {
  id: string;
  analysis: Analysis;
  busy: boolean;
  role: PositionRole;
  refreshedAt: string;
  needsRefresh: boolean;
  history: ScenarioRecord[];
};
export type InvestmentsState = { items: Investment[]; activeId: string | null };
export type InvestmentsAction =
  | {
      type: "open";
      id: string;
      analysis: Analysis;
      role?: PositionRole;
      refreshedAt?: string;
    }
  | { type: "select"; id: string }
  | { type: "close"; id: string }
  | {
      type: "analysis";
      id: string;
      analysis: Analysis;
      role?: PositionRole;
      refreshedAt?: string;
    }
  | { type: "scenario"; id: string; record: ScenarioRecord }
  | { type: "invalid"; id: string }
  | { type: "busy"; id: string; busy: boolean }
  // Another browser tab saved the workspace: add its investments and scenario
  // runs, and drop investments closed there.
  | { type: "merge"; items: Investment[]; closed: string[] };

export const emptyInvestments: InvestmentsState = { items: [], activeId: null };

export function investmentsReducer(
  state: InvestmentsState,
  action: InvestmentsAction,
): InvestmentsState {
  if (action.type === "open") {
    return {
      items: [
        ...state.items,
        {
          id: action.id,
          analysis: action.analysis,
          busy: false,
          role: action.role ?? "entered",
          refreshedAt: action.refreshedAt ?? new Date().toISOString(),
          needsRefresh: false,
          history: [],
        },
      ],
      activeId: action.id,
    };
  }
  if (action.type === "merge") return merge(state, action.items, action.closed);
  const index = state.items.findIndex((item) => item.id === action.id);
  // A completed request must never recreate a tab the user has closed.
  if (index === -1) return state;
  if (action.type === "select") {
    return state.activeId === action.id
      ? state
      : { ...state, activeId: action.id };
  }
  if (action.type === "close") {
    if (state.items[index].busy) return state;
    const items = state.items.filter((item) => item.id !== action.id);
    return {
      items,
      activeId:
        state.activeId === action.id
          ? (items[Math.min(index, items.length - 1)]?.id ?? null)
          : state.activeId,
    };
  }
  if (action.type === "busy" && state.items[index].busy === action.busy)
    return state;
  return {
    ...state,
    items: state.items.map((item) => {
      if (item.id !== action.id) return item;
      if (action.type === "analysis")
        return {
          ...item,
          analysis: action.analysis,
          role: action.role ?? item.role,
          refreshedAt: action.refreshedAt ?? new Date().toISOString(),
          needsRefresh: false,
          // Runs from a different market or token don't describe this one.
          history: samePosition(item.analysis.request, action.analysis.request)
            ? item.history
            : [],
        };
      if (action.type === "scenario")
        return {
          ...item,
          history: [
            action.record,
            ...item.history.filter((r) => r.id !== action.record.id),
          ].slice(0, HISTORY_LIMIT),
        };
      if (action.type === "invalid") return { ...item, needsRefresh: true };
      return { ...item, busy: action.busy };
    }),
  };
}

function merge(
  state: InvestmentsState,
  incoming: Investment[],
  closed: string[],
): InvestmentsState {
  const gone = new Set(closed);
  const theirs = new Map(incoming.map((item) => [item.id, item]));
  let changed = false;
  const items: Investment[] = [];
  for (const item of state.items) {
    if (gone.has(item.id) && !item.busy) {
      changed = true;
      continue;
    }
    const other = theirs.get(item.id);
    const history = other ? mergeHistory(item, other.history) : item.history;
    if (history !== item.history) changed = true;
    items.push(history === item.history ? item : { ...item, history });
  }
  for (const other of incoming)
    if (!gone.has(other.id) && !state.items.some((i) => i.id === other.id)) {
      items.push(other);
      changed = true;
    }
  if (!changed) return state;
  return {
    items,
    activeId: items.some((item) => item.id === state.activeId)
      ? state.activeId
      : (items[0]?.id ?? null),
  };
}

// Union of both tabs' runs for this position, newest first. Returns the
// existing array when nothing is new, so merging an echo changes nothing.
function mergeHistory(
  item: Investment,
  theirs: ScenarioRecord[],
): ScenarioRecord[] {
  const known = new Set(item.history.map((r) => r.id));
  const added = theirs.filter(
    (r) =>
      !known.has(r.id) && samePosition(r.request, item.analysis.request),
  );
  const merged = [...item.history, ...added]
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
    .slice(0, HISTORY_LIMIT);
  return merged.every((r, i) => r === item.history[i]) &&
    merged.length === item.history.length
    ? item.history
    : merged;
}

export function investmentLabel(analysis: Analysis): string {
  return (
    analysis.request.label ||
    (analysis.request.kind === "borrow"
      ? analysis.graph.nodes.find((node) => node.id === analysis.graph.root)?.label
      : analysis.request.kind === "market"
      ? analysis.graph.nodes.find((node) => node.type === "market")?.label
      : analysis.graph.nodes.find(
          (node) => node.type === "asset" && node.attrs.kind === "pt",
        )?.label) ||
    "Investment"
  );
}
