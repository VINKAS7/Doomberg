import { test } from "node:test";
import assert from "node:assert/strict";
import dagre from "@dagrejs/dagre";
import { roundedPath, routeEdges, simplify, stepRoute } from "../src/ui/edgeRoutes.ts";

const W = 280,
  H = 112;

// A position fanning out to several layers, with edges that skip layers and share targets.
const NODES = ["position", "market", "pt", "amm", "oracle", "usdc", "circle", "protocol", "chain", "feed"];
const EDGES = [
  ["position", "market"], ["position", "usdc"], ["market", "pt"], ["market", "oracle"], ["market", "protocol"],
  ["market", "usdc"], ["pt", "amm"], ["pt", "oracle"], ["amm", "protocol"], ["oracle", "feed"],
  ["usdc", "circle"], ["protocol", "chain"], ["position", "chain"],
].map(([source, target]) => ({ id: `${source}>${target}`, source, target }));

function layout() {
  const g = new dagre.graphlib.Graph({ multigraph: true });
  g.setGraph({ rankdir: "LR", ranksep: 130, nodesep: 40, edgesep: 24, marginx: 24, marginy: 24 });
  g.setDefaultEdgeLabel(() => ({}));
  NODES.forEach((id) => g.setNode(id, { width: W, height: H }));
  EDGES.forEach((e) => g.setEdge(e.source, e.target, {}, e.id));
  dagre.layout(g);
  const centers = Object.fromEntries(NODES.map((id) => [id, { x: g.node(id).x, y: g.node(id).y }]));
  const edges = EDGES.map((e) => ({ ...e, points: g.edge({ v: e.source, w: e.target, name: e.id }).points }));
  return { centers, routes: routeEdges(centers, edges, W, H) };
}

const segments = (pts) => pts.slice(1).map((p, i) => [pts[i], p]);

test("routes are orthogonal and run from the source's right side to the target's left side", () => {
  const { centers, routes } = layout();
  for (const e of EDGES) {
    const pts = routes[e.id].points;
    for (const [a, b] of segments(pts))
      assert.ok(Math.abs(a.x - b.x) < 1e-6 || Math.abs(a.y - b.y) < 1e-6, `${e.id} has a diagonal segment`);
    assert.equal(pts[0].x, centers[e.source].x + W / 2);
    assert.equal(pts.at(-1).x, centers[e.target].x - W / 2);
    assert.ok(Math.abs(pts[0].y - centers[e.source].y) < H / 2);
    assert.ok(Math.abs(pts.at(-1).y - centers[e.target].y) < H / 2);
  }
});

test("no edge crosses a card", () => {
  const { centers, routes } = layout();
  for (const e of EDGES)
    for (const [a, b] of segments(routes[e.id].points))
      for (const [id, c] of Object.entries(centers)) {
        const [x0, x1, y0, y1] = [c.x - W / 2 + 1, c.x + W / 2 - 1, c.y - H / 2 + 1, c.y + H / 2 - 1];
        const hits = Math.max(a.x, b.x) > x0 && Math.min(a.x, b.x) < x1 && Math.max(a.y, b.y) > y0 && Math.min(a.y, b.y) < y1;
        assert.ok(!hits, `${e.id} crosses ${id}`);
      }
});

test("edges get their own ports and never share a segment", () => {
  const { routes } = layout();
  const starts = new Map();
  for (const e of EDGES) {
    const key = `${e.source}:${routes[e.id].points[0].y}`;
    assert.ok(!starts.has(key), `${e.id} and ${starts.get(key)} leave ${e.source} at the same point`);
    starts.set(key, e.id);
  }
  const vertical = EDGES.flatMap((e) =>
    segments(routes[e.id].points)
      .filter(([a, b]) => a.x === b.x)
      .map(([a, b]) => ({ id: e.id, x: a.x, lo: Math.min(a.y, b.y), hi: Math.max(a.y, b.y) })),
  );
  for (const a of vertical)
    for (const b of vertical)
      if (a.id !== b.id && Math.abs(a.x - b.x) < 1)
        assert.ok(a.hi <= b.lo || b.hi <= a.lo, `${a.id} and ${b.id} overlap at x=${a.x}`);
});

test("helpers: dragged cards get one turn, straight runs collapse, the label sits halfway", () => {
  assert.deepEqual(stepRoute({ x: 0, y: 0 }, { x: 200, y: 50 }), [
    { x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 50 }, { x: 200, y: 50 },
  ]);
  assert.deepEqual(simplify([{ x: 0, y: 0 }, { x: 5, y: 0 }, { x: 10, y: 0 }, { x: 10, y: 0 }]), [
    { x: 0, y: 0 }, { x: 10, y: 0 },
  ]);
  const { d, mid } = roundedPath([{ x: 0, y: 0 }, { x: 100, y: 0 }, { x: 100, y: 100 }]);
  assert.match(d, /^M 0 0 L 90 0 Q 100 0 100 10 L 100 100$/);
  assert.deepEqual(mid, { x: 100, y: 0 });
});
