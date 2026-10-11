export type Evidence = { source: string; detail: string; ref?: string | null; block?: number | null };

export type NodeRisk = {
  expected_loss_pct: number;
  share: number;
  top_mode: string | null;
  modes: { mode: string; mode_label: string; p: number; loss_given_failure: number; expected_loss_pct: number; share: number; alert: AlertLevel }[];
};

export type GNode = {
  id: string;
  type: string;
  label: string;
  chain_id: number | null;
  address: string | null;
  layer: number;
  attrs: Record<string, any>;
  risk_factors: Record<string, any>;
  evidence: Evidence[];
  risk?: NodeRisk | null;
  modes: { id: string; label: string }[];
};

export type GEdge = { id: string; source: string; target: string; type: string; weight: number; attrs: Record<string, any>; evidence: Evidence[] };

export type AlertLevel = "none" | "watch" | "alert" | "corroborated" | "confirmed";

export type ScreenRow = {
  node_id: string; label: string; type: string; node_class: string; mode: string; mode_label: string;
  p: number; prior_p: number; base_p: number; annual_rate: number; loss_given_failure: number; expected_loss_pct: number; share: number;
  reasons: string[]; alert: AlertLevel; likelihood_ratio: number | null; rate_multiplier: number; signals: string[];
};

export type Signal = {
  node_id: string; label: string; mode: string; rule: string; rule_label: string; level: "watch" | "alert";
  value: number; threshold: number; detail: string; first_seen: number | null; node_class: string;
};

export type MonitorEvent = { ts: number; position_key: string; label: string | null; kind: string; severity: "info" | "warning" | "critical"; message: string; data: Record<string, any> };

export type Monitor = {
  position_key: string; label: string; verdict: "STAY" | "EXIT" | "REVIEW";
  model_verdict?: "STAY" | "EXIT"; data_status?: "complete" | "incomplete";
  data_issues?: { node_id: string; label: string; fields: string[]; detail: string }[];
  decision_rule?: string; execution_status?: "blocked" | "available";
  expected_loss_pct: number; baseline_loss_pct: number; excess_loss_pct: number; avoidable_loss_pct: number;
  yield_pct: number; net_loss_pct: number; liquidation_loss_pct?: number;
  exit_cost_pct: number; instant_fraction: number; max_alert: AlertLevel; regime: "calm" | "elevated" | "stressed"; contagion_max: number;
  signals: Signal[];
  alerts: { node_id: string; label: string; mode: string; mode_label: string; level: AlertLevel; signals: Signal[] }[];
  above_break_even: { node_id: string; label: string; mode: string; mode_label: string; p: number; break_even_p: number; alert: AlertLevel }[];
  previous: { ts: number; verdict: "STAY" | "EXIT" | "REVIEW"; expected_loss_pct: number; baseline_loss_pct: number; avoidable_loss_pct: number;
    p_any: number; yield_pct: number | null; net_loss_pct: number | null; exit_cost_pct: number; max_alert: AlertLevel } | null;
  events: MonitorEvent[];
  recorded: boolean;
};

export type ExitInfo = {
  cost_pct: number; cost_pct_ci: [number, number]; cost_usd: number; instant_fraction: number; instant_fraction_calm: number;
  stressed: boolean; steps: { step: string; source: string; detail: string; cost_pct: number }[]; assumptions: string[]; override?: number;
  // Market supply only: idle liquidity, and the share still withdrawable if other lenders race for it.
  liquidity_usd?: number; instant_fraction_run?: number;
};

export type Analysis = {
  position_id: string;
  request: { kind: PositionKind; address: string; chain_id: number; size_usd: number; book_usd: number; label?: string | null } & BorrowFields;
  graph: { root: string; nodes: GNode[]; edges: GEdge[] };
  screening: { horizon_days: number; total_expected_loss_pct: number; p_any_loss_event: number; top: ScreenRow[] };
  monitor: Monitor;
  exit_now: ExitInfo;
  leverage?: Leverage | null;
  stats: { nodes: number; edges: number; evidence: number; depth: number; discovery_seconds: number; warnings: string[] };
};

export type Hop = { node_id: string; label: string; type: string; mean_loss: number; edge_type: string | null; edge_weight: number | null; why: string | null };

export type ShockResult = {
  position: { label: string; size_usd: number; book_usd: number; kind: PositionKind };
  shock: { node_id: string; node_label: string; node_type: string; mode: string; mode_label: string; description: string; alert: string; alert_label: string;
    alert_source: "signals" | "manual"; signals: Signal[]; severity: Summary; incident?: any };
  probability: { annual_rate: number; annual_rate_ci: [number, number]; base_p: number; prior_p: number; p: number; p_ci: [number, number];
    likelihood_ratio: number | null; alert: string; rate_multiplier: number; horizon_days: number; reasons: string[] };
  loss: Summary & { histogram: { x0: number; x1: number; share: number }[] };
  exit: ExitInfo;
  decision: {
    verdict: "STAY" | "EXIT"; confidence: number; probability: number; probability_source: string;
    loss_given_failure: { mean: number; typical: number; bad_case: number };
    expected_loss_pct: number; expected_loss_usd: number; bad_case_usd: number; exit_cost_pct: number; exit_cost_usd: number; instant_fraction: number; instant_fraction_calm: number;
    break_even_probability: number | null; avoidable_loss_pct?: number; avoidable_loss_usd: number; book_impact_pct: number | null; position_share_of_book: number | null;
    reasons: string[]; break_even_table: { loss_if_fails: number; exit_above: number | null }[];
  };
  paths: { hops: Hop[]; strength: number }[];
  impacted: { node_id: string; label: string; type: string; mean_loss: number }[];
  node_losses: Record<string, number>;
  leverage?: Leverage | null;
  findings: string[];
  assumptions: string[];
  explanation: Explanation | null;
};

export type Explanation = { text: string; provider: string; model: string | null; error: string | null };

export type Verdict = "STAY" | "EXIT" | "REVIEW";

export type MonitorRun = {
  id: number; ts: number; position_key: string; label: string | null; kind: PositionKind; address: string; chain_id: number;
  size_usd: number; horizon_days: number; verdict: Verdict; expected_loss_pct: number; baseline_loss_pct: number;
  avoidable_loss_pct: number; p_any: number; exit_cost_pct: number; instant_fraction: number; max_alert: AlertLevel; contagion: number | null;
  // Null on runs recorded before the yield credit; there the verdict compared avoidable_loss_pct.
  yield_pct: number | null; net_loss_pct: number | null;
};

export const netLoss = (r: { avoidable_loss_pct: number; net_loss_pct: number | null }) =>
  r.net_loss_pct ?? r.avoidable_loss_pct;

export type WatchItem = {
  id: number | null; kind: PositionKind; address: string; chain_id: number; size_usd: number; book_usd: number | null;
  horizon_days: number | null; label: string | null; position_key: string | null; error: string | null;
  ltv?: number | null; deploy_address?: string | null; account?: string | null;
  latest: (MonitorRun & {
    model_verdict: "STAY" | "EXIT" | null;
    data_issues: { node_id: string; label: string; fields: string[]; detail: string }[];
    drivers: { node_id: string; mode: string; label: string; mode_label: string; p: number; break_even_p: number | null;
      above: boolean; alert: AlertLevel; el: number; signals: string[] }[];
  }) | null;
};

export type Watchlist = { source: "saved" | "file" | "presets"; items: WatchItem[] };

export type Schedule = {
  interval_s: number; min_interval_s: number; running: boolean; last_run: number | null; next_run: number | null;
  watchlist_source: Watchlist["source"];
};

export type Summary = { mean: number; p05: number; p50: number; p95: number; p99: number; prob_any_loss: number };

export type PositionKind = "token" | "market" | "borrow";

/** Only for a borrow position: the LTV and the PT the loan is deployed into (none = PT loop), or a borrower to read them from. */
export type BorrowFields = { ltv?: number | null; deploy_address?: string | null; account?: string | null };

/** Mirrors ``leverage.health`` in the engine. */
export type Leverage = {
  structure: "loop" | "deploy"; equity_usd: number; collateral_usd: number; debt_usd: number; deployed_usd: number;
  collateral_label: string | null; loan_label: string | null; deployed_label: string | null;
  leverage: number; ltv: number; lltv: number; health_factor: number; drop_to_liquidation: number; liquidation_price_usd: number | null;
  liquidation_incentive: number; seen_vol: number; vol_reason: string; oracle_sees_depeg: boolean; p_liquidation: number;
  expected_penalty_pct: number; penalty_if_liquidated_pct: number; pt_apy: number; borrow_apy: number; net_apy: number;
  live: { account: string; health_factor_live: number | null } | null;
};

export type Preset = { id: string; label: string; kind: PositionKind; chain_id: number; address: string; size_usd: number; note: string } & BorrowFields;

export type Catalog = {
  modes: { id: string; label: string; applies_to: string[]; description: string }[];
  alerts: { id: string; label: string }[];
  incidents: { id: string; name: string; date: string; mode: string; kind: string; severity: number; duration_h: number; summary: string; source: string }[];
};
