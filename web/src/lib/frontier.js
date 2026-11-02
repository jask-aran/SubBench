// Benchmark-frontier logic over the LLM-Pareto bundle (data/frontier.json). Pure: no DOM,
// so it is tested directly under Node.

export const RESOURCES = {
  cost: { label: "Cost per task", short: "cost", unit: "currency", log: true },
  time: { label: "Time per task", short: "time", unit: "duration", log: true },
  blend: { label: "Blended cost + time", short: "blend", unit: "blend", log: false },
  output_tokens: { label: "Output tokens", short: "output tokens", unit: "integer", log: true },
  input_tokens: { label: "Input tokens", short: "input tokens", unit: "integer", log: true },
  total_tokens: { label: "Total tokens", short: "tokens", unit: "integer", log: true },
  reasoning_tokens: { label: "Reasoning tokens", short: "reasoning", unit: "integer", log: true },
  steps: { label: "Agent steps", short: "steps", unit: "integer", log: false },
  weighted_cost: { label: "Weighted eval cost", short: "weighted cost", unit: "currency", log: true },
  ttft: { label: "Time to first token", short: "TTFT", unit: "duration", log: true },
  output_speed: { label: "Output speed", short: "speed", unit: "speed", log: true, higherIsBetter: true },
};

export const RESOURCE_ORDER = [
  "cost", "time", "blend", "output_tokens", "total_tokens", "input_tokens",
  "reasoning_tokens", "steps", "weighted_cost", "ttft",
];

// Fixed slot per family. Slots follow the validated categorical order in site.css; the
// assignment is by entity, never by rank, so a filter never repaints the survivors. Past
// eight families everything folds into "other" rather than inventing a ninth hue.
const FAMILY_SLOTS = new Map([
  ["Anthropic", 2], ["Claude Code", 2],
  ["OpenAI", 3], ["Codex", 3],
  ["Google", 1], ["Gemini CLI", 1],
  ["Alibaba", 4], ["Opencode", 4],
  ["Moonshot AI", 5], ["Kimi", 5], ["Kimi Code CLI", 5],
  ["Mistral", 6], ["Z AI", 6], ["Zhipu AI", 6],
  ["DeepSeek", 7], ["Cursor CLI", 7],
  ["xAI", 8], ["SpaceXAI", 8], ["Grok Build", 8],
]);

export function familySlot(family) {
  return FAMILY_SLOTS.get(family) ?? 0;
}

export function familyColor(family) {
  const slot = familySlot(family);
  return slot ? `var(--series-${slot})` : "var(--series-other)";
}

// Families that share a slot are shown under one legend entry.
export function familyGroup(family) {
  const slot = familySlot(family);
  if (!slot) return "Other";
  return family;
}

export function indexBundle(bundle) {
  const metrics = new Map(bundle.metrics.map((metric) => [metric.id, metric]));
  const entities = {
    model: new Map(bundle.entities.model.map((entity) => [entity.id, entity])),
    agent: new Map(bundle.entities.agent.map((entity) => [entity.id, entity])),
  };
  const byMetric = new Map();
  for (const item of bundle.observations) {
    if (!byMetric.has(item.metric_id)) byMetric.set(item.metric_id, []);
    byMetric.get(item.metric_id).push(item);
  }
  return { bundle, metrics, entities, byMetric };
}

// Every observation of one metric, joined with its entity.
export function points(index, metricId) {
  const metric = index.metrics.get(metricId);
  if (!metric) return [];
  const table = index.entities[metric.entity_type];
  return (index.byMetric.get(metricId) || [])
    .map((observation) => ({ observation, entity: table.get(observation.entity_id) }))
    .filter((point) => point.entity && Number.isFinite(point.observation.score));
}

export function resourceCounts(list) {
  const counts = {};
  for (const { observation } of list) {
    for (const [key, value] of Object.entries(observation.resources || {})) {
      if (Number.isFinite(value)) counts[key] = (counts[key] || 0) + 1;
    }
  }
  if ((counts.cost || 0) >= 2 && (counts.time || 0) >= 2) counts.blend = Math.min(counts.cost, counts.time);
  return counts;
}

export function availableResources(list) {
  const counts = resourceCounts(list);
  return RESOURCE_ORDER.filter((key) => (counts[key] || 0) >= 2);
}

// Metrics that carry both cost and time on enough points to draw a surface.
export function surfaceMetrics(index, min = 6) {
  return [...index.metrics.values()].filter((metric) => {
    const list = points(index, metric.id);
    const both = list.filter(({ observation: o }) =>
      Number.isFinite(o.resources?.cost) && Number.isFinite(o.resources?.time)
      && o.resources.cost > 0 && o.resources.time > 0);
    return both.length >= min;
  });
}

export function blendNormalization(list) {
  const span = (key) => {
    const values = list.map(({ observation }) => observation.resources?.[key]).filter(Number.isFinite);
    return values.length ? [Math.min(...values), Math.max(...values)] : [0, 1];
  };
  return { cost: span("cost"), time: span("time") };
}

export function resourceValue(point, resource, { normalization, weight = 0.5 } = {}) {
  if (resource !== "blend") {
    const value = point.observation.resources?.[resource];
    return Number.isFinite(value) ? value : null;
  }
  const cost = point.observation.resources?.cost;
  const time = point.observation.resources?.time;
  if (!Number.isFinite(cost) || !Number.isFinite(time) || !normalization) return null;
  const [c0, c1] = normalization.cost;
  const [t0, t1] = normalization.time;
  const c = c1 === c0 ? 0.5 : (cost - c0) / (c1 - c0);
  const t = t1 === t0 ? 0.5 : (time - t0) / (t1 - t0);
  return weight * c + (1 - weight) * t;
}

// Non-dominated set for "less x, more score". Sorted by x ascending.
export function pareto2d(list, getX) {
  const sorted = list
    .map((point) => ({ point, x: getX(point) }))
    .filter((item) => Number.isFinite(item.x))
    .sort((a, b) => a.x - b.x || b.point.observation.score - a.point.observation.score);
  const frontier = [];
  let best = -Infinity;
  for (const { point } of sorted) {
    if (point.observation.score > best + 1e-12) {
      frontier.push(point);
      best = point.observation.score;
    }
  }
  return frontier;
}

// Non-dominated set for "less cost, less time, more score". A point is dominated when
// another is no worse on all three axes and strictly better on at least one.
export function pareto3d(list) {
  const items = list.filter(({ observation: o }) =>
    Number.isFinite(o.resources?.cost) && Number.isFinite(o.resources?.time));
  return items.filter((p) => {
    const a = p.observation;
    return !items.some((q) => {
      if (q === p) return false;
      const b = q.observation;
      const noWorse = b.resources.cost <= a.resources.cost
        && b.resources.time <= a.resources.time
        && b.score >= a.score;
      const better = b.resources.cost < a.resources.cost
        || b.resources.time < a.resources.time
        || b.score > a.score;
      return noWorse && better;
    });
  });
}

// The attainment surface: for a budget of cost c and time t, the best score any point
// reaches without exceeding either. It is a staircase by construction, and it is exactly
// what "the best model within budget" means. Returned on a log-spaced grid.
export function attainmentGrid(list, { steps = 36, costRange, timeRange } = {}) {
  const items = list.filter(({ observation: o }) => o.resources?.cost > 0 && o.resources?.time > 0);
  if (!items.length) return null;
  const costs = items.map(({ observation: o }) => o.resources.cost);
  const times = items.map(({ observation: o }) => o.resources.time);
  const [c0, c1] = costRange || [Math.min(...costs), Math.max(...costs)];
  const [t0, t1] = timeRange || [Math.min(...times), Math.max(...times)];
  const lc0 = Math.log10(c0), lc1 = Math.log10(c1);
  const lt0 = Math.log10(t0), lt1 = Math.log10(t1);
  const cAt = (i) => 10 ** (lc0 + (lc1 - lc0) * (i / steps));
  const tAt = (j) => 10 ** (lt0 + (lt1 - lt0) * (j / steps));
  const cells = [];
  for (let i = 0; i <= steps; i++) {
    const row = [];
    for (let j = 0; j <= steps; j++) {
      row.push(bestWithin(items, cAt(i), tAt(j)));
    }
    cells.push(row);
  }
  return { steps, cAt, tAt, cells, costRange: [c0, c1], timeRange: [t0, t1] };
}

export function bestWithin(list, costCap, timeCap) {
  let best = null;
  for (const point of list) {
    const o = point.observation;
    if (!(o.resources?.cost <= costCap * (1 + 1e-9) && o.resources?.time <= timeCap * (1 + 1e-9))) continue;
    if (!best || o.score > best.observation.score
        || (o.score === best.observation.score && o.resources.cost < best.observation.resources.cost)) {
      best = point;
    }
  }
  return best;
}

// The efficiency solver: the cheapest (or fastest) point that clears a share of the best
// visible score.
export function solve(list, { target = 0.9, getValue }) {
  if (!list.length) return null;
  const best = Math.max(...list.map(({ observation }) => observation.score));
  const threshold = best * target;
  return list
    .filter(({ observation }) => observation.score >= threshold)
    .map((point) => ({ point, value: getValue(point) }))
    .filter((item) => Number.isFinite(item.value))
    .sort((a, b) => a.value - b.value || b.point.observation.score - a.point.observation.score)[0]?.point ?? null;
}

// Usage logs name models the way the provider's API does ("gpt-5.6-sol",
// "claude-opus-5-20260724"); the benchmark bundle names them by slug ("gpt-5-6-sol").
export function normaliseModelName(name) {
  return String(name || "")
    .toLowerCase()
    .trim()
    .replace(/^(openai|anthropic)[/:]/, "")
    .replace(/[._\s]+/g, "-")
    .replace(/-(20\d{6}|\d{4}-\d{2}-\d{2})$/, "")
    .replace(/-latest$/, "")
    .replace(/-+/g, "-");
}

// The benchmark entity a usage model most likely corresponds to. An exact slug wins;
// otherwise the base slug's default configuration, then its highest reasoning tier.
export function matchModel(name, models) {
  const slug = normaliseModelName(name);
  if (!slug) return null;
  const byId = models instanceof Map ? models : new Map(models.map((m) => [m.id, m]));
  const normalisedIds = new Map();
  // The bundle lists Artificial Analysis entities first; they carry the most lenses, so
  // when two ids normalise alike ("gpt-5-6-sol", "gpt-5.6-sol") the first one wins.
  for (const id of byId.keys()) {
    const key = normaliseModelName(id);
    if (!normalisedIds.has(key)) normalisedIds.set(key, id);
  }
  if (normalisedIds.has(slug)) return byId.get(normalisedIds.get(slug));
  for (const suffix of ["-xhigh", "-high", "-medium"]) {
    const id = normalisedIds.get(slug + suffix);
    if (id) return byId.get(id);
  }
  // A dated or point-release name that only shares the family prefix.
  const stem = slug.replace(/-(xhigh|high|medium|low|max|thinking|reasoning)$/, "");
  return stem !== slug ? matchModel(stem, byId) : null;
}

// How many benchmark tasks one entitlement buys at API price. The subscription value is
// what the usage would have cost at public API prices; the task cost is what the
// benchmark measured one task to cost at the same prices. Their ratio is a throughput, not
// a promise: a real workload's tokens per task differ from the benchmark's.
export function tasksPerEntitlement(valueUsd, costPerTask) {
  if (!(valueUsd > 0) || !(costPerTask > 0)) return null;
  return valueUsd / costPerTask;
}

export function variantOf(entity, observation) {
  if (observation?.variant) return observation.variant;
  const match = /-(non-reasoning|xhigh|high|medium|low|max)$/.exec(entity.id);
  return match ? match[1] : "default";
}

export function baseKey(entity, observation) {
  if (entity.agent) return entity.agent;
  const variant = observation?.variant;
  if (variant && !["default", "max"].includes(variant)) return entity.id.replace(new RegExp(`-${variant}$`), "");
  return entity.id.replace(/-(?:non-reasoning|xhigh|high|medium|low)$/, "");
}
