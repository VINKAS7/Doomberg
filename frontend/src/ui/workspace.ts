import type { Analysis, Evidence, GNode, ShockResult } from "../types.ts";

export type PositionRole = "example" | "entered" | "holding";
export const ROLE_LABELS: Record<PositionRole, string> = {
  example: "Example position",
  entered: "Entered position",
  holding: "Declared holding",
};
export type ScenarioRecord = {
  id: string;
  createdAt: string;
  analysisId: string;
  refreshedAt: string;
  request: Analysis["request"];
  horizonDays: number;
  result: ShockResult;
};
export const HISTORY_LIMIT = 20;

// Whether two requests describe the same position. Size, book and horizon may
// differ (a refresh or an edited size); a different market or token may not.
export function samePosition(
  a: Analysis["request"],
  b: Analysis["request"],
): boolean {
  return (
    a.kind === b.kind &&
    a.chain_id === b.chain_id &&
    a.address.toLowerCase() === b.address.toLowerCase() &&
    (a.deploy_address ?? "").toLowerCase() === (b.deploy_address ?? "").toLowerCase()
  );
}
export const REFRESH_AFTER_MS = 5 * 60_000;

// These identify saved UI records, not cryptographic keys. The fallback also
// works on LAN HTTP URLs where browsers do not expose crypto.randomUUID.
export function newWorkspaceId(): string {
  return (
    globalThis.crypto?.randomUUID?.() ??
    `workspace-${Date.now()}-${Math.random().toString(36).slice(2)}`
  );
}

export function ageLabel(timestamp: string, now = Date.now()): string {
  const elapsed = Math.max(0, now - Date.parse(timestamp));
  if (!Number.isFinite(elapsed)) return "Unknown age";
  if (elapsed < 60_000) return "Just now";
  if (elapsed < 3_600_000) return `${Math.floor(elapsed / 60_000)}m ago`;
  if (elapsed < 86_400_000) return `${Math.floor(elapsed / 3_600_000)}h ago`;
  return `${Math.floor(elapsed / 86_400_000)}d ago`;
}
export function refreshSuggested(timestamp: string, now = Date.now()): boolean {
  const elapsed = now - Date.parse(timestamp);
  return !Number.isFinite(elapsed) || elapsed >= REFRESH_AFTER_MS;
}
export function evidenceSource(e: Evidence): string {
  if (e.source === "rpc") return "On-chain evidence";
  if (e.source === "heuristic") return "Inferred";
  if (e.source === "registry") return "Registry reference";
  return `${e.source} evidence`;
}
export function evidenceStatus(node: GNode): string {
  if (node.risk_factors.unverified || !node.evidence.length)
    return "Unverified";
  if (node.evidence.some((e) => e.source === "heuristic")) return "Inferred";
  if (node.evidence.some((e) => e.source === "rpc")) return "On-chain evidence";
  if (node.evidence.some((e) => e.source === "registry"))
    return "Registry reference";
  return "API evidence";
}
export function evidenceSummary(node: GNode): string {
  const sources = [...new Set(node.evidence.map(evidenceSource))];
  return `${evidenceStatus(node)}. Sources: ${sources.join(", ") || "none"}. Evidence for an attribute does not verify every dependency or permission.`;
}
export function exitAmounts(result: ShockResult) {
  const fraction = Math.max(0, Math.min(1, result.exit.instant_fraction));
  return {
    withdrawable: result.position.size_usd * fraction,
    remaining: result.position.size_usd * (1 - fraction),
  };
}
export function relationLabel(type: string): string {
  const labels: Record<string, string> = {
    deposits_in: "Deposits in",
    denominated_in: "Loan asset",
    lends: "Lends",
    collateral_in: "Collateral",
    priced_by: "Priced by",
    reads_feed: "Reads feed",
    backed_by: "Backed by",
    issued_by: "Issued by",
    mintable_by: "Token controls",
    traded_on: "Traded on",
    liquidated_in: "Collateral in",
    runs_on: "Runs on",
    settles_on: "Settles on",
    holds: "Holds",
    borrows_from: "Borrows from",
    deploys_into: "Deploys into",
    bridged_via: "Bridge",
  };
  return labels[type] || type.replace(/_/g, " ");
}
// Selection affects highlighting only. Equivalent visible graphs share a
// layout key, avoiding a fresh layout and viewport movement on every click.
export function topologyKey(graph: {
  nodes: { id: string }[];
  edges: { source: string; target: string; type: string }[];
}): string {
  return JSON.stringify([
    graph.nodes.map((n) => n.id).sort(),
    graph.edges.map((e) => [e.source, e.target, e.type]).sort(),
  ]);
}
