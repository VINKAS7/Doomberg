// Orthogonal edge routes for a left-to-right layered layout (dagre, rankdir LR).
//
// Dagre places a dummy node wherever an edge crosses a layer, at a height no
// card occupies, so following those points keeps edges off the cards. Edges
// only turn inside the gaps between layers, each turn in its own lane, and
// each edge leaves and enters a card at its own port, so no two edges share a
// segment and every line can be followed from end to end.

export type Point = { x: number; y: number };
export type LayoutEdge = { id: string; source: string; target: string; points: Point[] };
export type Route = { points: Point[]; sourcePort: number; targetPort: number };

const PORT_GAP = 12; // vertical distance between two edges on the same card side
const LANE_GAP = 12; // horizontal distance between two turns in the same gap
const LEAD = 16; // straight run out of a card before the first turn
const ARROW_ROOM = 26; // straight run into a card, so the arrowhead sits on a straight line

const near = (a: number, b: number) => Math.abs(a - b) < 1;

// Spread n items around 0, `gap` apart, clamped to +-limit.
function spread(n: number, gap: number, limit: number): number[] {
  const step = n > 1 ? Math.min(gap, (2 * limit) / (n - 1)) : 0;
  return Array.from({ length: n }, (_, i) => (i - (n - 1) / 2) * step);
}

export function routeEdges(
  centers: Record<string, Point>,
  edges: LayoutEdge[],
  width: number,
  height: number,
): Record<string, Route> {
  const columns = [...new Set(Object.values(centers).map((c) => Math.round(c.x)))].sort((a, b) => a - b);
  const column = (x: number) => columns.findIndex((c) => near(c, x));

  // 1. Waypoints: card centre -> dummies sitting in a layer column -> card centre.
  const paths = edges.map((e) => {
    const s = centers[e.source],
      t = centers[e.target];
    const inner = e.points.filter((p) => column(p.x) >= 0 && p.x > s.x + 1 && p.x < t.x - 1);
    return { e, s, t, inner, forward: t.x > s.x + 1 };
  });

  // 2. Ports: order a card's edges by the height of the other end, so they don't cross at the card.
  const outs = new Map<string, { id: string; y: number }[]>();
  const ins = new Map<string, { id: string; y: number }[]>();
  for (const p of paths) {
    const next = p.inner[0]?.y ?? p.t.y,
      prev = p.inner[p.inner.length - 1]?.y ?? p.s.y;
    (outs.get(p.e.source) ?? outs.set(p.e.source, []).get(p.e.source)!).push({ id: p.e.id, y: next });
    (ins.get(p.e.target) ?? ins.set(p.e.target, []).get(p.e.target)!).push({ id: p.e.id, y: prev });
  }
  const port = new Map<string, number>();
  const assign = (side: Map<string, { id: string; y: number }[]>, key: string) => {
    for (const list of side.values()) {
      list.sort((a, b) => a.y - b.y);
      spread(list.length, PORT_GAP, height / 2 - 16).forEach((off, i) => port.set(`${key}${list[i].id}`, off));
    }
  };
  assign(outs, "s:");
  assign(ins, "t:");

  // 3. Turns: every change of height happens in a gap between two layers, in its own lane.
  type Turn = { edge: string; index: number; gap: number; from: number; to: number };
  const turns: Turn[] = [];
  const stops = new Map<string, Point[]>();
  for (const p of paths) {
    const sp = port.get(`s:${p.e.id}`) ?? 0,
      tp = port.get(`t:${p.e.id}`) ?? 0;
    const pts = [{ x: p.s.x + width / 2, y: p.s.y + sp }, ...p.inner, { x: p.t.x - width / 2, y: p.t.y + tp }];
    stops.set(p.e.id, pts);
    if (!p.forward) continue;
    for (let i = 0; i < pts.length - 1; i++) {
      if (near(pts[i].y, pts[i + 1].y)) continue;
      const from = i === 0 ? column(p.s.x) : column(pts[i].x);
      turns.push({ edge: p.e.id, index: i, gap: from, from: pts[i].y, to: pts[i + 1].y });
    }
  }
  const laneX = new Map<string, number>();
  const byGap = new Map<number, Turn[]>();
  for (const t of turns) (byGap.get(t.gap) ?? byGap.set(t.gap, []).get(t.gap)!).push(t);
  for (const [gap, list] of byGap) {
    // A turn going down that starts lower sits further left, and the mirror for turns going up:
    // that ordering lets turns heading the same way pass without crossing.
    const down = list.filter((t) => t.to > t.from).sort((a, b) => b.from - a.from);
    const up = list.filter((t) => t.to < t.from).sort((a, b) => a.from - b.from);
    const ordered = [...down, ...up];
    const left = columns[gap] + width / 2 + LEAD,
      right = (columns[gap + 1] ?? columns[gap] + width) - width / 2 - ARROW_ROOM;
    const mid = (left + right) / 2;
    spread(ordered.length, LANE_GAP, Math.max(0, (right - left) / 2)).forEach((off, i) =>
      laneX.set(`${ordered[i].edge}:${ordered[i].index}`, mid + off),
    );
  }

  // 4. Orthogonal polyline through the lanes.
  const routes: Record<string, Route> = {};
  for (const p of paths) {
    const pts = stops.get(p.e.id)!;
    const out: Point[] = [pts[0]];
    if (p.forward) {
      for (let i = 0; i < pts.length - 1; i++) {
        const x = laneX.get(`${p.e.id}:${i}`);
        if (x !== undefined) out.push({ x, y: pts[i].y }, { x, y: pts[i + 1].y });
        out.push(pts[i + 1]);
      }
    } else {
      out.push(...pts.slice(1)); // a back edge (none expected in a DAG) keeps dagre's own points
    }
    routes[p.e.id] = {
      points: simplify(out),
      sourcePort: port.get(`s:${p.e.id}`) ?? 0,
      targetPort: port.get(`t:${p.e.id}`) ?? 0,
    };
  }
  return routes;
}

// A route between two arbitrary points: out, one turn halfway, in. Used once a card has been dragged.
export function stepRoute(a: Point, b: Point): Point[] {
  const x = b.x > a.x + LEAD + ARROW_ROOM ? (a.x + b.x) / 2 : a.x + LEAD;
  return simplify([a, { x, y: a.y }, { x, y: b.y }, b]);
}

// Drops repeated points and points in the middle of a straight run.
export function simplify(pts: Point[]): Point[] {
  const out: Point[] = [];
  for (const p of pts) {
    if (out.length && near(out[out.length - 1].x, p.x) && near(out[out.length - 1].y, p.y)) continue;
    if (out.length >= 2) {
      const a = out[out.length - 2],
        b = out[out.length - 1];
      if ((near(a.x, b.x) && near(b.x, p.x)) || (near(a.y, b.y) && near(b.y, p.y))) out.pop();
    }
    out.push(p);
  }
  return out;
}

// SVG path with rounded corners, plus the point halfway along it for the label.
export function roundedPath(pts: Point[], radius = 10): { d: string; mid: Point } {
  let d = `M ${pts[0].x} ${pts[0].y}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [a, p, b] = [pts[i - 1], pts[i], pts[i + 1]];
    const r = Math.min(radius, Math.hypot(p.x - a.x, p.y - a.y) / 2, Math.hypot(b.x - p.x, b.y - p.y) / 2);
    const toward = (q: Point) => {
      const len = Math.hypot(q.x - p.x, q.y - p.y) || 1;
      return { x: p.x + ((q.x - p.x) / len) * r, y: p.y + ((q.y - p.y) / len) * r };
    };
    const [s, e] = [toward(a), toward(b)];
    d += ` L ${s.x} ${s.y} Q ${p.x} ${p.y} ${e.x} ${e.y}`;
  }
  const last = pts[pts.length - 1];
  d += ` L ${last.x} ${last.y}`;
  const lengths = pts.slice(1).map((p, i) => Math.hypot(p.x - pts[i].x, p.y - pts[i].y));
  let half = lengths.reduce((s, l) => s + l, 0) / 2,
    mid = pts[0];
  for (let i = 0; i < lengths.length; i++) {
    if (half <= lengths[i]) {
      const f = lengths[i] ? half / lengths[i] : 0;
      mid = { x: pts[i].x + (pts[i + 1].x - pts[i].x) * f, y: pts[i].y + (pts[i + 1].y - pts[i].y) * f };
      break;
    }
    half -= lengths[i];
  }
  return { d, mid };
}
