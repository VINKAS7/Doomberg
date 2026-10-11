import type { Analysis, GNode, GEdge } from "../types";

export const GROUPS = {
  controls: { label: "Token controls", types: ["mint_authority"] },
  pricing: { label: "Price inputs", types: ["feed"] },
  infrastructure: { label: "Infrastructure", types: ["protocol", "chain"] },
};
export type GroupKey = keyof typeof GROUPS;
export type ViewGraph = {
  nodes: GNode[];
  edges: GEdge[];
  groups: Record<string, GroupKey>;
};

// Presentation only: the original analysis remains the source for all calculations.
export function graphView(
  analysis: Analysis,
  expanded: Set<GroupKey>,
  all: boolean,
  reveal: string[] = [],
): ViewGraph {
  const groups: Record<string, GroupKey> = {};
  const mapping = new Map<string, string>();
  const nodes: GNode[] = [];
  const revealGroups = new Set<GroupKey>();
  for (const [key, group] of Object.entries(GROUPS) as [
    GroupKey,
    (typeof GROUPS)[GroupKey],
  ][]) {
    if (
      analysis.graph.nodes.some(
        (n) => reveal.includes(n.id) && group.types.includes(n.type),
      )
    )
      revealGroups.add(key);
  }
  for (const [key, group] of Object.entries(GROUPS) as [
    GroupKey,
    (typeof GROUPS)[GroupKey],
  ][]) {
    if (all || expanded.has(key) || revealGroups.has(key)) continue;
    const members = analysis.graph.nodes.filter((n) =>
      group.types.includes(n.type),
    );
    if (!members.length) continue;
    const id = `ui-group:${key}`;
    groups[id] = key;
    members.forEach((n) => mapping.set(n.id, id));
    nodes.push({
      id,
      type: "group",
      label: group.label,
      chain_id: null,
      address: null,
      layer: 0,
      attrs: {
        subtitle: `${members.length} components · click to expand`,
        members: members.map((n) => n.id),
        groupKey: key,
      },
      risk_factors: {
        unverified: members.some((n) => n.risk_factors.unverified),
      },
      evidence: [],
      modes: [],
    });
  }
  for (const n of analysis.graph.nodes)
    if (!mapping.has(n.id)) {
      mapping.set(n.id, n.id);
      nodes.push(n);
    }
  const edges: GEdge[] = [];
  const seen = new Set<string>();
  for (const edge of analysis.graph.edges) {
    const source = mapping.get(edge.source)!;
    const target = mapping.get(edge.target)!;
    if (source === target) continue;
    const id = `${source}|${edge.type}|${target}`;
    if (seen.has(id)) continue;
    seen.add(id);
    edges.push({ ...edge, id, source, target });
  }
  return { nodes, edges, groups };
}
