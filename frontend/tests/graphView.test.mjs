import { test } from "node:test";
import assert from "node:assert/strict";
import { graphView } from "../src/ui/graphView.ts";
const node = (id, type) => ({
  id,
  type,
  label: id,
  chain_id: 1,
  address: null,
  layer: 0,
  attrs: {},
  risk_factors: {},
  evidence: [],
  modes: [],
});
const edge = (source, target, type) => ({
  id: `${source}|${type}|${target}`,
  source,
  target,
  type,
  weight: 1,
  attrs: {},
  evidence: [],
});
const analysis = {
  graph: {
    root: "position",
    nodes: [
      node("position", "position"),
      node("pt", "asset"),
      node("oracle", "oracle"),
      node("feed", "feed"),
      node("control", "mint_authority"),
      node("pendle", "protocol"),
      node("ethereum", "chain"),
    ],
    edges: [
      edge("position", "pt", "holds"),
      edge("pt", "control", "mintable_by"),
      edge("pt", "pendle", "runs_on"),
      edge("pendle", "ethereum", "settles_on"),
      edge("position", "oracle", "priced_by"),
      edge("oracle", "feed", "reads_feed"),
    ],
  },
};
function freeze(o) {
  Object.freeze(o);
  for (const v of Object.values(o))
    if (v && typeof v === "object" && !Object.isFrozen(v)) freeze(v);
  return o;
}
freeze(analysis);
test("simplified graph retains every dependency in a real node or expandable group, without modifying analysis", () => {
  const before = JSON.stringify(analysis);
  const view = graphView(analysis, new Set(), false);
  const covered = view.nodes.flatMap((n) =>
    n.type === "group" ? n.attrs.members : [n.id],
  );
  assert.deepEqual(
    [...covered].sort(),
    analysis.graph.nodes.map((n) => n.id).sort(),
  );
  assert.ok(view.nodes.length < analysis.graph.nodes.length);
  assert.equal(JSON.stringify(analysis), before);
  const ids = new Set(view.nodes.map((n) => n.id));
  assert.ok(
    view.edges.every(
      (e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target,
    ),
  );
});
test("all components restores the exact original nodes and connections", () => {
  const view = graphView(analysis, new Set(), true);
  assert.deepEqual(view.nodes, analysis.graph.nodes);
  assert.deepEqual(view.edges, analysis.graph.edges);
  assert.deepEqual(view.groups, {});
});
test("a selected or shocked dependency is revealed with the rest of its category", () => {
  for (const id of ["control", "feed", "ethereum"]) {
    const view = graphView(analysis, new Set(), false, [id]);
    assert.ok(view.nodes.some((n) => n.id === id));
    assert.ok(view.edges.some((e) => e.target === id));
  }
  const view = graphView(analysis, new Set(), false, ["ethereum"]);
  assert.ok(view.nodes.some((n) => n.id === "pendle"));
});
test("explicit expansion reveals controls while leaving other groups collapsed", () => {
  const view = graphView(analysis, new Set(["controls"]), false);
  assert.ok(view.nodes.some((n) => n.id === "control"));
  assert.ok(view.nodes.some((n) => n.id === "ui-group:infrastructure"));
});
