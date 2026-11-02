// The benchmark-frontier arithmetic the site draws: the 2D and 3D Pareto sets, the attainment surface, the budget
// probe, the solver, and the join from usage-log model names to benchmark entities.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { pareto2d, pareto3d, attainmentGrid, bestWithin, solve, normaliseModelName, matchModel, tasksPerEntitlement,
  indexBundle, points, availableResources, surfaceMetrics, familyColor, familyGroup } from "../web/src/lib/frontier.js";

const pt = (id, score, cost, time) => ({ entity: { id, label: id, family: "OpenAI" }, observation: { score, resources: { cost, time } } });

test("the 2D frontier keeps only points no cheaper point beats", () => {
  const list = [pt("a", 10, 1, 1), pt("b", 30, 2, 1), pt("c", 20, 3, 1), pt("d", 40, 4, 1)];
  assert.deepEqual(pareto2d(list, (p) => p.observation.resources.cost).map((p) => p.entity.id), ["a", "b", "d"]);
});

test("a tie on cost keeps the better score only", () => {
  const list = [pt("a", 10, 1, 1), pt("b", 12, 1, 1)];
  assert.deepEqual(pareto2d(list, (p) => p.observation.resources.cost).map((p) => p.entity.id), ["b"]);
});

test("the 3D set keeps a point that wins on time although it looks dominated on cost", () => {
  const list = [pt("cheap-slow", 50, 1, 100), pt("dear-fast", 50, 5, 10), pt("dominated", 40, 6, 120), pt("best", 80, 9, 90)];
  assert.deepEqual(pareto3d(list).map((p) => p.entity.id).sort(), ["best", "cheap-slow", "dear-fast"]);
});

test("identical points do not dominate each other out of the set", () => {
  assert.equal(pareto3d([pt("x", 1, 1, 1), pt("y", 1, 1, 1)]).length, 2);
});

test("the attainment surface is the best score within both budgets, and never falls as either grows", () => {
  const list = [pt("a", 10, 0.01, 10), pt("b", 50, 1, 100), pt("c", 70, 10, 1000), pt("d", 30, 0.1, 1000)];
  const grid = attainmentGrid(list, { steps: 8 });
  assert.equal(grid.cells[0][0].entity.id, "a");
  assert.equal(grid.cells[8][8].entity.id, "c");
  for (let i = 0; i <= 8; i++) for (let j = 0; j <= 8; j++) {
    const here = grid.cells[i][j]?.observation.score ?? -1;
    if (i < 8) assert.ok((grid.cells[i + 1][j]?.observation.score ?? -1) >= here);
    if (j < 8) assert.ok((grid.cells[i][j + 1]?.observation.score ?? -1) >= here);
  }
});

test("the budget probe returns nothing when nothing fits", () => {
  assert.equal(bestWithin([pt("a", 10, 5, 5)], 1, 1), null);
  assert.equal(bestWithin([pt("a", 10, 5, 5), pt("b", 20, 1, 1)], 6, 6).entity.id, "b");
});

test("the solver picks the cheapest point that clears the target", () => {
  const list = [pt("a", 100, 10, 1), pt("b", 92, 2, 1), pt("c", 80, 1, 1)];
  assert.equal(solve(list, { target: 0.9, getValue: (p) => p.observation.resources.cost }).entity.id, "b");
  assert.equal(solve([], { target: 0.9, getValue: () => 1 }), null);
});

test("usage-log model names normalise to benchmark slugs", () => {
  assert.equal(normaliseModelName("gpt-5.6-sol"), "gpt-5-6-sol");
  assert.equal(normaliseModelName("claude-opus-5-20260724"), "claude-opus-5");
  assert.equal(normaliseModelName("anthropic/Claude_Sonnet 5"), "claude-sonnet-5");
});

test("a usage model matches its benchmark entity, and a newer model matches nothing", () => {
  const models = new Map([
    ["gpt-5-6-sol", { id: "gpt-5-6-sol" }], ["gpt-5.6-sol", { id: "gpt-5.6-sol" }],
    ["claude-opus-5", { id: "claude-opus-5" }], ["claude-fable-5-high", { id: "claude-fable-5-high" }],
  ]);
  assert.equal(matchModel("gpt-5.6-sol", models).id, "gpt-5-6-sol");
  assert.equal(matchModel("claude-opus-5-20260724", models).id, "claude-opus-5");
  assert.equal(matchModel("claude-fable-5", models).id, "claude-fable-5-high");
  // Opus 5.5 is not Opus 5: a model newer than the snapshot must stay unmatched rather than borrow a neighbour.
  assert.equal(matchModel("claude-opus-5-5", models), null);
});

test("tasks per entitlement needs a positive value and a positive price", () => {
  assert.equal(tasksPerEntitlement(100, 2), 50);
  assert.equal(tasksPerEntitlement(null, 2), null);
  assert.equal(tasksPerEntitlement(100, 0), null);
});

test("a family keeps its colour slot; an unknown family folds into Other", () => {
  assert.equal(familyColor("Anthropic"), "var(--series-2)");
  assert.equal(familyColor("Claude Code"), "var(--series-2)");
  assert.equal(familyGroup("Some Lab"), "Other");
  assert.equal(familyColor("Some Lab"), "var(--series-other)");
});

test("the shipped bundle loads, and its headline lenses can be charted and drawn as a surface", () => {
  const bundle = JSON.parse(readFileSync(new URL("../web/public/data/frontier.json", import.meta.url), "utf8"));
  const index = indexBundle(bundle);
  assert.ok(points(index, "aa-intelligence").length > 20);
  assert.ok(availableResources(points(index, "aa-intelligence")).includes("cost"));
  // Every Intelligence Index evaluation carries its own cost and time, so each one can be a surface.
  const surfaces = surfaceMetrics(index).map((m) => m.id);
  assert.ok(surfaces.includes("aa-intelligence"));
  assert.ok(surfaces.filter((id) => id.startsWith("aa-eval-")).length >= 5);
});

import { paretoN, axisValue, higherIsBetter, axisIsLog, attainmentGridBy, scoreLookup } from "../web/src/lib/frontier.js";

test("the best trade-offs follow each axis's direction, in any number of axes", () => {
  const list = [pt("a", 10, 1, 9), pt("b", 30, 2, 1), pt("c", 20, 3, 5), pt("d", 40, 4, 8)];
  const get = (axis) => (p) => axisValue(axis, p);
  // score up, cost down: a, b, d
  assert.deepEqual(paretoN(list, [get("res:cost"), get("score")], [false, true]).map((p) => p.entity.id).sort(), ["a", "b", "d"]);
  // cost down, time down: a and b (c and d are beaten by b on both)
  assert.deepEqual(paretoN(list, [get("res:cost"), get("res:time")], [false, false]).map((p) => p.entity.id).sort(), ["a", "b"]);
  // three axes agree with pareto3d
  assert.deepEqual(paretoN(list, [get("res:cost"), get("res:time"), get("score")], [false, false, true]).map((p) => p.entity.id).sort(),
    pareto3d(list).map((p) => p.entity.id).sort());
});

test("axis directions and scales", () => {
  assert.equal(higherIsBetter("score"), true);
  assert.equal(higherIsBetter("lens:deepswe-pass1"), true);
  assert.equal(higherIsBetter("res:cost"), false);
  assert.equal(higherIsBetter("res:output_speed"), true);
  assert.equal(axisIsLog("res:cost"), true);
  assert.equal(axisIsLog("score"), false);
});

test("another benchmark's score joins by model and tier", () => {
  const index = indexBundle({
    metrics: [{ id: "a", entity_type: "model" }, { id: "b", entity_type: "model" }],
    entities: { model: [{ id: "m" }], agent: [] },
    observations: [
      { entity_type: "model", entity_id: "m", metric_id: "a", score: 1, variant: "high" },
      { entity_type: "model", entity_id: "m", metric_id: "b", score: 7, variant: "high" },
      { entity_type: "model", entity_id: "m", metric_id: "b", score: 5 },
    ],
  });
  const lookups = new Map([["b", scoreLookup(index, "b")]]);
  const point = { entity: { id: "m" }, observation: { score: 1, variant: "high" } };
  assert.equal(axisValue("lens:b", point, lookups), 7);
  assert.equal(axisValue("lens:b", { entity: { id: "m" }, observation: { score: 1, variant: "low" } }, lookups), 5);
});

test("the general attainment grid matches the cost-time one", () => {
  const list = [pt("a", 10, 0.01, 10), pt("b", 50, 1, 100), pt("c", 70, 10, 1000)];
  const g = attainmentGridBy(list, (p) => p.observation.resources.cost, (p) => p.observation.resources.time, (p) => p.observation.score, { steps: 4 });
  assert.equal(g.cells[4][4].entity.id, "c");
  assert.equal(g.cells[0][0].entity.id, "a");
});
