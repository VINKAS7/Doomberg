import { test } from "node:test";
import assert from "node:assert/strict";
import {
  emptyInvestments,
  investmentLabel,
  investmentsReducer as reduce,
} from "../src/ui/investments.ts";

const analysis = (id, kind = "market") => ({
  position_id: id,
  request: { kind, address: "0xabc", chain_id: 1, size_usd: 1000000 },
  graph: {
    nodes: [
      { type: "market", label: `Market ${id}`, attrs: {} },
      { type: "asset", label: `PT ${id}`, attrs: { kind: "pt" } },
    ],
  },
});
const open = (state, id) =>
  reduce(state, { type: "open", id, analysis: analysis(id) });

test("opening and switching investments preserves their independent analyses", () => {
  const first = open(emptyInvestments, "a");
  const second = open(first, "b");
  assert.equal(second.activeId, "b");
  assert.equal(second.items[0], first.items[0]);
  const switched = reduce(second, { type: "select", id: "a" });
  assert.equal(switched.activeId, "a");
  assert.equal(switched.items, second.items);
});

test("an async analysis update applies to its original tab after switching", () => {
  const state = open(open(emptyInvestments, "a"), "b");
  const updatedAnalysis = analysis("a-new");
  const updated = reduce(state, {
    type: "analysis",
    id: "a",
    analysis: updatedAnalysis,
  });
  assert.equal(updated.activeId, "b");
  assert.equal(updated.items[0].analysis, updatedAnalysis);
  assert.equal(updated.items[1], state.items[1]);
  assert.equal(state.items[0].analysis.position_id, "a");
});

test("closing active tabs selects the next neighbour, then the previous, then the empty workspace", () => {
  let state = open(open(open(emptyInvestments, "a"), "b"), "c");
  state = reduce(state, { type: "select", id: "b" });
  state = reduce(state, { type: "close", id: "b" });
  assert.equal(state.activeId, "c");
  state = reduce(state, { type: "close", id: "c" });
  assert.equal(state.activeId, "a");
  state = reduce(state, { type: "close", id: "a" });
  assert.deepEqual(state, emptyInvestments);
});

test("closing an inactive tab leaves the current investment selected", () => {
  const state = open(open(emptyInvestments, "a"), "b");
  const closed = reduce(state, { type: "close", id: "a" });
  assert.equal(closed.activeId, "b");
  assert.deepEqual(
    closed.items.map((item) => item.id),
    ["b"],
  );
});

test("busy tabs cannot be closed, and stale callbacks cannot reopen a closed tab", () => {
  let state = open(emptyInvestments, "a");
  state = reduce(state, { type: "busy", id: "a", busy: true });
  assert.equal(reduce(state, { type: "close", id: "a" }), state);
  state = reduce(state, { type: "busy", id: "a", busy: false });
  state = reduce(state, { type: "close", id: "a" });
  assert.equal(
    reduce(state, { type: "analysis", id: "a", analysis: analysis("a-new") }),
    state,
  );
  assert.equal(reduce(state, { type: "select", id: "missing" }), state);
});

test("tab labels distinguish held PT tokens from lending markets and respect custom names", () => {
  assert.equal(investmentLabel(analysis("a")), "Market a");
  assert.equal(investmentLabel(analysis("a", "token")), "PT a");
  const custom = analysis("a");
  custom.request.label = "Treasury position";
  assert.equal(investmentLabel(custom), "Treasury position");
});
