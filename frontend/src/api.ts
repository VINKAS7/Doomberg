import type {
  Analysis,
  BorrowFields,
  Catalog,
  Explanation,
  MonitorEvent,
  MonitorRun,
  PositionKind,
  Preset,
  Schedule,
  ShockResult,
  Watchlist,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
async function call<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
  method?: "PUT" | "DELETE",
): Promise<T> {
  const r = await fetch(`/api${path}`, {
    signal,
    ...(body === undefined
      ? { method }
      : {
          method: method ?? "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(body),
        }),
  });
  if (!r.ok) {
    let msg = `${r.status} ${r.statusText}`;
    try {
      const j = await r.json();
      msg =
        typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* keep status text */
    }
    throw new ApiError(msg, r.status);
  }
  return r.json();
}

export const api = {
  health: (signal?: AbortSignal) =>
    call<{ ok: boolean; llm: string }>("/health", undefined, signal),
  presets: () => call<Preset[]>("/presets"),
  catalog: () => call<Catalog>("/catalog"),
  analyze: (body: {
    kind: PositionKind;
    address: string;
    chain_id: number;
    size_usd: number;
    book_usd: number;
    horizon_days: number;
    label?: string;
  } & BorrowFields) => call<Analysis>("/analyze", body),
  shock: (body: {
    position_id: string;
    node_id: string;
    mode: string;
    alert: string;
    probability_override?: number | null;
    exit_cost_override?: number | null;
    horizon_days?: number;
  }) => call<ShockResult>("/shock", body),
  replay: (body: {
    position_id: string;
    incident_id: string;
    exit_cost_override?: number | null;
  }) => call<ShockResult>("/replay", body),
  explain: (result: ShockResult) =>
    call<Explanation>("/explain", { result }),
  monitor: {
    watchlist: () => call<Watchlist>("/monitor/watchlist"),
    watch: (body: {
      kind: PositionKind;
      address: string;
      chain_id: number;
      size_usd: number;
      book_usd: number;
      horizon_days: number;
      label?: string | null;
    } & BorrowFields) => call<{ id: number; position_key: string }>("/monitor/watchlist", body),
    unwatch: (id: number) =>
      call<{ ok: boolean }>(`/monitor/watchlist/${id}`, undefined, undefined, "DELETE"),
    schedule: () => call<Schedule>("/monitor/schedule"),
    setSchedule: (interval_s: number) =>
      call<Schedule>("/monitor/schedule", { interval_s }, undefined, "PUT"),
    run: () => call<unknown[]>("/monitor/run", {}),
    history: (positionKey: string, limit = 48) =>
      call<MonitorRun[]>(
        `/monitor/history?position_key=${encodeURIComponent(positionKey)}&limit=${limit}`,
      ),
    explain: (positionKey: string) =>
      call<Explanation>("/monitor/explain", { position_key: positionKey }),
    events: (positionKey: string, limit = 10) =>
      call<MonitorEvent[]>(
        `/monitor/events?position_key=${encodeURIComponent(positionKey)}&limit=${limit}`,
      ),
  },
};
