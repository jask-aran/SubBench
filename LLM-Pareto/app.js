const DATA_URL = "./frontier-data.json";
const SVG_NS = "http://www.w3.org/2000/svg";

const RESOURCE_META = {
  rank: { label: "Capability only", short: "score", format: "number", log: false },
  blend: { label: "Blended cost + time", short: "blend", format: "blend", log: false },
  cost: { label: "Cost / task", short: "cost", format: "currency", log: true },
  time: { label: "Time / task", short: "time", format: "duration", log: true },
  weighted_cost: { label: "Weighted eval cost", short: "weighted cost", format: "currency", log: true },
  output_tokens: { label: "Output tokens", short: "output", format: "integer", log: true },
  input_tokens: { label: "Input tokens", short: "input", format: "integer", log: true },
  total_tokens: { label: "Total tokens", short: "tokens", format: "integer", log: true },
  reasoning_tokens: { label: "Reasoning tokens", short: "reasoning", format: "integer", log: true },
  steps: { label: "Agent steps", short: "steps", format: "integer", log: false },
  ttft: { label: "Time to first token", short: "TTFT", format: "duration", log: true },
  first_answer: { label: "Time to first answer token", short: "first answer", format: "duration", log: true },
  output_speed: { label: "Output speed", short: "speed", format: "speed", log: true },
};

const FAMILY_HUES = new Map([
  ["Anthropic", 27], ["Claude Code", 27],
  ["OpenAI", 145], ["Codex", 145],
  ["Google", 214], ["Gemini CLI", 214],
  ["xAI", 2], ["Grok Build", 2],
  ["Moonshot AI", 326], ["Kimi", 326], ["Kimi Code CLI", 326],
  ["Zhipu AI", 274], ["Z AI", 274],
  ["Alibaba", 43], ["Opencode", 43],
  ["Meta", 186], ["Mistral", 167],
  ["Amazon", 307], ["NVIDIA", 104],
  ["DeepSeek", 203], ["Cohere", 235],
  ["Cursor CLI", 286],
]);

const state = {
  mode: "model",
  metric: "aa-intelligence",
  resource: "blend",
  size: "none",
  blendWeight: .5,
  frontier: true,
  log: false,
  target: .9,
  solverMin: "x",
  search: "",
  hiddenFamilies: { model: new Set(), agent: new Set() },
  hiddenEntities: { model: new Set(), agent: new Set() },
  pinned: { model: new Set(), agent: new Set() },
};

let bundle;
let metrics = new Map();
let entities = { model: new Map(), agent: new Map() };
let observations = new Map();
let renderContext = null;

const $ = selector => document.querySelector(selector);
const refs = {
  freshness: $("#freshness"),
  statline: $("#statline"),
  metric: $("#metric-select"),
  resource: $("#resource-select"),
  size: $("#size-select"),
  blendControl: $("#blend-control"),
  blendWeight: $("#blend-weight"),
  blendValue: $("#blend-value"),
  frontierToggle: $("#frontier-toggle"),
  logToggle: $("#log-toggle"),
  search: $("#search-input"),
  familyList: $("#family-list"),
  visibleCount: $("#visible-count"),
  chart: $("#chart"),
  ranking: $("#ranking"),
  chartStage: $("#chart-stage"),
  tooltip: $("#tooltip"),
  empty: $("#empty-state"),
  sourcePill: $("#source-pill"),
  title: $("#chart-title"),
  description: $("#chart-description"),
  summary: $("#chart-summary"),
  scopeNote: $("#scope-note"),
  targetRange: $("#target-range"),
  targetLabel: $("#target-label"),
  solverOptions: $("#solver-options"),
  solverResult: $("#solver-result"),
  frontierList: $("#frontier-list"),
  frontierCount: $("#frontier-count"),
  methodNote: $("#method-note"),
  compareTray: $("#compare-tray"),
  compareGrid: $("#compare-grid"),
  collectionId: $("#collection-id"),
};

function svgEl(tag, attrs = {}, text = null) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (text !== null) node.textContent = text;
  return node;
}

function hash(text) {
  let value = 2166136261;
  for (let i = 0; i < text.length; i++) {
    value ^= text.charCodeAt(i);
    value = Math.imul(value, 16777619);
  }
  return value >>> 0;
}

function colorFor(entity, observation = null) {
  const family = entity.family || entity.provider || "Other";
  const base = FAMILY_HUES.get(family) ?? hash(family) % 360;
  const id = entity.id.toLowerCase();
  let light = 52;
  if (/(?:-max|\(max\))/.test(id) || observation?.variant === "max") light = 40;
  else if (/(?:-xhigh|\(xhigh\))/.test(id) || observation?.variant === "xhigh") light = 45;
  else if (/(?:-high|\(high\))/.test(id) || observation?.variant === "high") light = 51;
  else if (/(?:-medium|\(medium\))/.test(id) || observation?.variant === "medium") light = 58;
  else if (/(?:-low|\(low\))/.test(id) || observation?.variant === "low") light = 65;
  else if (/non-reasoning/.test(id)) light = 72;
  const hueShift = (hash(entity.id) % 9) - 4;
  return `hsl(${(base + hueShift + 360) % 360} 78% ${light}%)`;
}

function formatNumber(value, format, compact = false) {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  if (format === "percent") return `${(value * 100).toFixed(compact ? 0 : 1)}%`;
  if (format === "score100") return value.toFixed(compact ? 0 : 1);
  if (format === "currency") {
    if (value < .01) return `$${value.toFixed(4)}`;
    if (value < 1) return `$${value.toFixed(3)}`;
    return `$${value.toFixed(value < 100 ? 2 : 0)}`;
  }
  if (format === "duration") {
    if (value < 1) return `${Math.round(value * 1000)}ms`;
    if (value >= 3600) return `${(value / 3600).toFixed(1)}h`;
    if (value >= 120) return `${(value / 60).toFixed(1)}m`;
    return `${value.toFixed(value < 10 ? 1 : 0)}s`;
  }
  if (format === "integer") {
    if (Math.abs(value) >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}m`;
    if (Math.abs(value) >= 10_000) return `${(value / 1000).toFixed(0)}k`;
    return Math.round(value).toLocaleString();
  }
  if (format === "speed") return `${Math.round(value).toLocaleString()} tok/s`;
  if (format === "blend") return value.toFixed(3);
  return Math.abs(value) < 1 ? value.toFixed(3) : value.toFixed(1);
}

function metricFormat() {
  return metrics.get(state.metric)?.format || "number";
}

function entityLabel(entity) {
  return entity.label || entity.id;
}

function baseModelKey(entity, observation) {
  if (state.mode === "agent") return entity.agent || entity.family || entity.id;
  const variant = observation?.variant;
  if (variant && !["default", "max"].includes(variant)) {
    return entity.id.replace(new RegExp(`-${variant}$`), "");
  }
  return entity.id.replace(/-(?:non-reasoning|xhigh|high|medium|low)$/, "");
}

function allMetricObservations() {
  return observations.get(state.metric) || [];
}

function entityPassesSearch(entity) {
  if (!state.search) return true;
  const haystack = [
    entity.label, entity.id, entity.provider, entity.family,
    entity.agent, entity.model, entity.host_model_slug,
  ].filter(Boolean).join(" ").toLowerCase();
  return haystack.includes(state.search);
}

function filteredPoints({ includeHidden = false } = {}) {
  return allMetricObservations().filter(observation => {
    const entity = entities[state.mode].get(observation.entity_id);
    if (!entity || !entityPassesSearch(entity)) return false;
    if (includeHidden) return true;
    return !state.hiddenFamilies[state.mode].has(entity.family)
      && !state.hiddenEntities[state.mode].has(entity.id);
  }).map(observation => ({ observation, entity: entities[state.mode].get(observation.entity_id) }));
}

function resourceCounts(points = allMetricObservations()) {
  const counts = {};
  for (const observation of points) {
    for (const [key, value] of Object.entries(observation.resources || {})) {
      if (Number.isFinite(value)) counts[key] = (counts[key] || 0) + 1;
    }
  }
  if ((counts.cost || 0) >= 2 && (counts.time || 0) >= 2) counts.blend = Math.min(counts.cost, counts.time);
  return counts;
}

function availableResources() {
  const counts = resourceCounts();
  const preferred = [
    "blend", "cost", "time", "weighted_cost", "output_tokens", "input_tokens",
    "total_tokens", "reasoning_tokens", "steps", "ttft", "first_answer", "output_speed",
  ];
  const available = preferred.filter(key => (counts[key] || 0) >= 2);
  return available.length ? available : ["rank"];
}

function buildNormalization(points) {
  const values = key => points
    .map(({ observation }) => observation.resources?.[key])
    .filter(Number.isFinite);
  const range = key => {
    const items = values(key);
    return items.length ? [Math.min(...items), Math.max(...items)] : [0, 1];
  };
  return { cost: range("cost"), time: range("time") };
}

function resourceValue(point, resource = state.resource, normalization = null) {
  if (resource === "rank") return null;
  if (resource !== "blend") return point.observation.resources?.[resource] ?? null;
  const cost = point.observation.resources?.cost;
  const time = point.observation.resources?.time;
  if (!Number.isFinite(cost) || !Number.isFinite(time)) return null;
  const ranges = normalization || buildNormalization(filteredPoints());
  const [cMin, cMax] = ranges.cost;
  const [tMin, tMax] = ranges.time;
  const c = cMax === cMin ? .5 : (cost - cMin) / (cMax - cMin);
  const t = tMax === tMin ? .5 : (time - tMin) / (tMax - tMin);
  return state.blendWeight * c + (1 - state.blendWeight) * t;
}

function populateMetricSelect() {
  refs.metric.innerHTML = "";
  const grouped = new Map();
  for (const metric of bundle.metrics.filter(item => item.entity_type === state.mode)) {
    if (!grouped.has(metric.group)) grouped.set(metric.group, []);
    grouped.get(metric.group).push(metric);
  }
  for (const [group, items] of grouped) {
    const optgroup = document.createElement("optgroup");
    optgroup.label = group;
    for (const metric of items) {
      const option = document.createElement("option");
      option.value = metric.id;
      option.textContent = `${metric.label} · ${metric.count}`;
      optgroup.append(option);
    }
    refs.metric.append(optgroup);
  }
  if (![...metrics.values()].some(metric => metric.id === state.metric && metric.entity_type === state.mode)) {
    state.metric = state.mode === "model" ? "aa-intelligence" : "agent-index";
  }
  refs.metric.value = state.metric;
}

function populateResourceControls({ reset = false } = {}) {
  const available = availableResources();
  if (reset || !available.includes(state.resource)) {
    state.resource = available.includes("blend")
      ? "blend"
      : available.includes("weighted_cost")
        ? "weighted_cost"
        : available[0];
    state.log = false;
  }
  refs.resource.innerHTML = "";
  for (const key of available) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = RESOURCE_META[key].label;
    refs.resource.append(option);
  }
  refs.resource.value = state.resource;

  const counts = resourceCounts();
  const sizeOptions = ["none", ...Object.keys(RESOURCE_META).filter(key =>
    !["rank", "blend"].includes(key) && (counts[key] || 0) >= 2
  )];
  if (!sizeOptions.includes(state.size)) state.size = "none";
  refs.size.innerHTML = "";
  for (const key of sizeOptions) {
    const option = document.createElement("option");
    option.value = key;
    option.textContent = key === "none" ? "Uniform" : RESOURCE_META[key].label;
    refs.size.append(option);
  }
  refs.size.value = state.size;
  refs.blendControl.hidden = state.resource !== "blend";
  refs.logToggle.disabled = !RESOURCE_META[state.resource].log;
  if (refs.logToggle.disabled) state.log = false;
  syncToggle(refs.logToggle, state.log);
  buildSolverOptions();
}

function buildSolverOptions() {
  const available = availableResources();
  const options = [];
  if (state.resource !== "rank") options.push(["x", RESOURCE_META[state.resource].short]);
  for (const key of ["cost", "time"]) {
    if (available.includes(key) && key !== state.resource) options.push([key, RESOURCE_META[key].short]);
  }
  refs.solverOptions.innerHTML = "";
  if (!options.length) {
    state.solverMin = null;
    return;
  }
  if (!options.some(([key]) => key === state.solverMin)) state.solverMin = options[0][0];
  for (const [key, label] of options) {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.resource = key;
    button.classList.toggle("active", key === state.solverMin);
    button.textContent = label;
    button.addEventListener("click", () => {
      state.solverMin = key;
      buildSolverOptions();
      render();
    });
    refs.solverOptions.append(button);
  }
}

function syncToggle(button, active) {
  button.classList.toggle("active", active);
  button.setAttribute("aria-pressed", String(active));
}

function familyRows() {
  const map = new Map();
  for (const point of filteredPoints({ includeHidden: true })) {
    const family = point.entity.family || "Other";
    if (!map.has(family)) map.set(family, []);
    map.get(family).push(point);
  }
  return [...map.entries()].sort((a, b) => {
    const aBest = Math.max(...a[1].map(point => point.observation.score));
    const bBest = Math.max(...b[1].map(point => point.observation.score));
    return bBest - aBest;
  });
}

function renderFamilies() {
  refs.familyList.innerHTML = "";
  for (const [family, points] of familyRows()) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "family-row";
    const off = state.hiddenFamilies[state.mode].has(family);
    button.classList.toggle("off", off);
    const sample = points[0];
    const dot = document.createElement("span");
    dot.className = "family-dot";
    dot.style.background = colorFor({ ...sample.entity, family });
    const name = document.createElement("span");
    name.className = "family-name";
    name.textContent = family;
    const count = document.createElement("span");
    count.className = "family-count";
    const visible = points.filter(point => !state.hiddenEntities[state.mode].has(point.entity.id)).length;
    count.textContent = `${off ? 0 : visible}/${points.length}`;
    button.append(dot, name, count);
    button.addEventListener("click", () => {
      if (off) state.hiddenFamilies[state.mode].delete(family);
      else state.hiddenFamilies[state.mode].add(family);
      render();
    });
    button.addEventListener("contextmenu", event => {
      event.preventDefault();
      const allFamilies = familyRows().map(([name]) => name);
      state.hiddenFamilies[state.mode] = new Set(allFamilies.filter(name => name !== family));
      state.hiddenEntities[state.mode].clear();
      render();
    });
    refs.familyList.append(button);
  }
}

function renderHeader(points) {
  const metric = metrics.get(state.metric);
  refs.title.textContent = metric.label;
  refs.description.textContent = metric.description;
  refs.sourcePill.textContent = metric.group.startsWith("DeepSWE") ? "DeepSWE"
    : metric.entity_type === "agent" || metric.group.startsWith("AA") ? "AA" : metric.group;
  const sorted = [...points].sort((a, b) => b.observation.score - a.observation.score);
  const best = sorted[0];
  refs.summary.innerHTML = best
    ? `<b>${points.length}</b> visible<br>best · <b>${formatNumber(best.observation.score, metric.format)}</b>`
    : "No visible records";

  const scopes = new Set(points.map(point => point.observation.resource_scope));
  let note = "";
  if (state.resource === "rank" && metric.group === "AA benchmarks") {
    note = "Artificial Analysis does not publish benchmark-specific time or full task cost here. This lens therefore uses a capability ranking instead of borrowing the composite index’s resources.";
  } else if (state.resource === "weighted_cost") {
    note = "Weighted eval cost is this benchmark’s contribution to the Intelligence Index cost. It is useful for relative efficiency, but it is not an unweighted per-task price.";
  } else if (scopes.has("configuration-wide") && !metric.composite) {
    note = "Cost, time, and token use apply to the complete harness–model configuration across the agent evaluation suite, not this component benchmark alone.";
  }
  refs.scopeNote.hidden = !note;
  refs.scopeNote.textContent = note;
  refs.methodNote.textContent = state.resource === "rank"
    ? "No resource metric is published for this lens, so records are ordered by capability."
    : state.resource === "blend"
      ? "Blend min–max normalizes cost and time across the visible set. Changing filters changes that relative index."
      : `The x-axis uses ${RESOURCE_META[state.resource].label.toLowerCase()} from the same observation scope shown above.`;
}

function computePareto(points, getX) {
  const sorted = [...points].sort((a, b) => getX(a) - getX(b) || b.observation.score - a.observation.score);
  const frontier = [];
  let best = -Infinity;
  for (const point of sorted) {
    if (point.observation.score > best + 1e-12) {
      frontier.push(point);
      best = point.observation.score;
    }
  }
  return frontier;
}

function renderScatter(points) {
  refs.ranking.hidden = true;
  refs.chart.style.display = "";
  refs.chart.innerHTML = "";
  const width = Math.max(refs.chartStage.clientWidth, 520);
  const height = Math.max(refs.chartStage.clientHeight, 390);
  refs.chart.setAttribute("viewBox", `0 0 ${width} ${height}`);
  const margin = { top: 24, right: 22, bottom: 48, left: 62 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const normalization = buildNormalization(points);
  const comparable = points.filter(point => Number.isFinite(resourceValue(point, state.resource, normalization)));
  if (!comparable.length) return { comparable, frontier: [], winner: null };

  const rawX = comparable.map(point => resourceValue(point, state.resource, normalization));
  const rawY = comparable.map(point => point.observation.score);
  const useLog = state.log && RESOURCE_META[state.resource].log && rawX.every(value => value > 0);
  const tx = value => useLog ? Math.log10(value) : value;
  const xValues = rawX.map(tx);
  let xMin = Math.min(...xValues), xMax = Math.max(...xValues);
  let yMin = Math.min(...rawY), yMax = Math.max(...rawY);
  const xPad = (xMax - xMin) * .065 || Math.abs(xMax || 1) * .08;
  const yPad = (yMax - yMin) * .08 || Math.abs(yMax || 1) * .08;
  xMin -= xPad; xMax += xPad;
  if (state.resource === "blend") xMin = Math.max(0, xMin);
  yMin -= yPad; yMax += yPad;
  const sx = value => margin.left + (tx(value) - xMin) / (xMax - xMin) * plotWidth;
  const sy = value => margin.top + plotHeight - (value - yMin) / (yMax - yMin) * plotHeight;

  const grid = svgEl("g");
  const metric = metrics.get(state.metric);
  for (let index = 0; index <= 5; index++) {
    const fraction = index / 5;
    const x = margin.left + fraction * plotWidth;
    const y = margin.top + plotHeight - fraction * plotHeight;
    const transformedX = xMin + fraction * (xMax - xMin);
    const yValue = yMin + fraction * (yMax - yMin);
    grid.append(
      svgEl("line", { class: "grid-line", x1: x, x2: x, y1: margin.top, y2: margin.top + plotHeight }),
      svgEl("line", { class: "grid-line", x1: margin.left, x2: margin.left + plotWidth, y1: y, y2: y }),
      svgEl("text", { class: "axis-text", x, y: height - 17, "text-anchor": "middle" },
        formatNumber(useLog ? 10 ** transformedX : transformedX, RESOURCE_META[state.resource].format, true)),
      svgEl("text", { class: "axis-text", x: margin.left - 10, y: y + 3, "text-anchor": "end" },
        formatNumber(yValue, metric.format, true)),
    );
  }
  grid.append(
    svgEl("text", { class: "axis-title", x: margin.left + plotWidth / 2, y: height - 3, "text-anchor": "middle" },
      `${RESOURCE_META[state.resource].label}${useLog ? " · log scale" : ""}`),
    svgEl("text", {
      class: "axis-title", x: -(margin.top + plotHeight / 2), y: 15,
      "text-anchor": "middle", transform: "rotate(-90)",
    }, metric.label),
  );
  refs.chart.append(grid);

  const lines = svgEl("g");
  if (state.mode === "model") {
    const grouped = new Map();
    for (const point of comparable) {
      const key = baseModelKey(point.entity, point.observation);
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(point);
    }
    for (const group of grouped.values()) {
      if (group.length < 2) continue;
      group.sort((a, b) => resourceValue(a, state.resource, normalization) - resourceValue(b, state.resource, normalization));
      const path = group.map((point, index) =>
        `${index ? "L" : "M"}${sx(resourceValue(point, state.resource, normalization))},${sy(point.observation.score)}`
      ).join(" ");
      lines.append(svgEl("path", {
        class: "variant-line", d: path, stroke: colorFor(group[0].entity, group[0].observation),
      }));
    }
  }
  refs.chart.append(lines);

  const getX = point => resourceValue(point, state.resource, normalization);
  const frontier = computePareto(comparable, getX);
  if (state.frontier && frontier.length > 1) {
    const path = frontier.map((point, index) =>
      `${index ? "L" : "M"}${sx(getX(point))},${sy(point.observation.score)}`
    ).join(" ");
    refs.chart.append(svgEl("path", { class: "frontier-line", d: path }));
  }

  const sizeValues = state.size === "none" ? [] : comparable
    .map(point => point.observation.resources?.[state.size])
    .filter(Number.isFinite);
  const sizeMin = sizeValues.length ? Math.min(...sizeValues) : 0;
  const sizeMax = sizeValues.length ? Math.max(...sizeValues) : 1;
  const radius = point => {
    if (state.size === "none") return 5.5;
    const value = point.observation.resources?.[state.size];
    if (!Number.isFinite(value)) return 3.5;
    const ratio = sizeMax === sizeMin ? .5 : (value - sizeMin) / (sizeMax - sizeMin);
    return 4 + Math.sqrt(Math.max(0, ratio)) * 10;
  };
  const pointLayer = svgEl("g");
  for (const point of comparable) {
    const circle = svgEl("circle", {
      class: `point${state.pinned[state.mode].has(point.entity.id) ? " pinned" : ""}`,
      cx: sx(getX(point)),
      cy: sy(point.observation.score),
      r: radius(point),
      fill: colorFor(point.entity, point.observation),
      tabindex: 0,
      "aria-label": `${entityLabel(point.entity)}, ${metric.label} ${formatNumber(point.observation.score, metric.format)}`,
    });
    circle.addEventListener("pointerenter", event => showTooltip(event, point, getX(point)));
    circle.addEventListener("pointermove", event => moveTooltip(event));
    circle.addEventListener("pointerleave", hideTooltip);
    circle.addEventListener("click", () => togglePin(point.entity.id));
    circle.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        togglePin(point.entity.id);
      }
    });
    pointLayer.append(circle);
  }
  refs.chart.append(pointLayer);

  const winner = solve(comparable, normalization);
  if (winner) {
    refs.chart.append(svgEl("circle", {
      class: "solver-ring",
      cx: sx(getX(winner)),
      cy: sy(winner.observation.score),
      r: radius(winner) + 5,
    }));
  }
  return { comparable, frontier, winner, normalization };
}

function renderRanking(points) {
  refs.chart.style.display = "none";
  refs.ranking.hidden = false;
  refs.ranking.innerHTML = "";
  const sorted = [...points].sort((a, b) => b.observation.score - a.observation.score);
  if (!sorted.length) return { comparable: [], frontier: [], winner: null };
  const scores = sorted.map(point => point.observation.score);
  const min = Math.min(...scores);
  const max = Math.max(...scores);
  for (const [index, point] of sorted.entries()) {
    const row = document.createElement("div");
    row.className = "rank-row";
    row.tabIndex = 0;
    const color = colorFor(point.entity, point.observation);
    const ratio = max === min ? 1 : (point.observation.score - min) / (max - min);
    const number = document.createElement("span");
    number.className = "rank-number";
    number.textContent = String(index + 1);
    const name = document.createElement("span");
    name.className = "rank-name";
    name.textContent = entityLabel(point.entity);
    name.style.color = state.pinned[state.mode].has(point.entity.id) ? color : "";
    const track = document.createElement("span");
    track.className = "rank-track";
    const bar = document.createElement("span");
    bar.className = "rank-bar";
    bar.style.width = `${8 + ratio * 92}%`;
    bar.style.background = color;
    track.append(bar);
    const score = document.createElement("span");
    score.className = "rank-score";
    score.textContent = formatNumber(point.observation.score, metricFormat());
    row.append(number, name, track, score);
    row.addEventListener("click", () => togglePin(point.entity.id));
    row.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") togglePin(point.entity.id);
    });
    refs.ranking.append(row);
  }
  return { comparable: sorted, frontier: [], winner: null };
}

function showTooltip(event, point, xValue) {
  const metric = metrics.get(state.metric);
  const resources = point.observation.resources || {};
  const lines = [
    `<strong>${escapeHTML(entityLabel(point.entity))}</strong>`,
    `${escapeHTML(metric.label)}: <b>${formatNumber(point.observation.score, metric.format)}</b>`,
  ];
  if (state.resource !== "rank") {
    lines.push(`${escapeHTML(RESOURCE_META[state.resource].label)}: <b>${formatNumber(xValue, RESOURCE_META[state.resource].format)}</b>`);
  }
  const details = [
    ["cost", "cost"], ["time", "time"], ["output_tokens", "output"],
  ].filter(([key]) => Number.isFinite(resources[key]) && key !== state.resource);
  if (details.length) {
    lines.push(details.map(([key, label]) =>
      `${label}: ${formatNumber(resources[key], RESOURCE_META[key].format)}`
    ).join(" · "));
  }
  lines.push(`<span class="tip-source">${escapeHTML(point.observation.source)} · click to compare</span>`);
  refs.tooltip.innerHTML = lines.join("<br>");
  refs.tooltip.hidden = false;
  moveTooltip(event);
}

function escapeHTML(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function moveTooltip(event) {
  const stage = refs.chartStage.getBoundingClientRect();
  const tip = refs.tooltip.getBoundingClientRect();
  let left = event.clientX - stage.left + 13;
  let top = event.clientY - stage.top + 13;
  if (left + tip.width > stage.width - 8) left = event.clientX - stage.left - tip.width - 13;
  if (top + tip.height > stage.height - 8) top = event.clientY - stage.top - tip.height - 13;
  refs.tooltip.style.left = `${Math.max(8, left)}px`;
  refs.tooltip.style.top = `${Math.max(8, top)}px`;
}

function hideTooltip() {
  refs.tooltip.hidden = true;
}

function solve(points, normalization) {
  if (state.resource === "rank" || !state.solverMin || !points.length) return null;
  const bestScore = Math.max(...points.map(point => point.observation.score));
  const threshold = bestScore * state.target;
  const candidates = points.filter(point => point.observation.score >= threshold);
  const minResource = state.solverMin === "x" ? state.resource : state.solverMin;
  const values = candidates.map(point => ({
    point,
    value: resourceValue(point, minResource, normalization),
  })).filter(item => Number.isFinite(item.value));
  values.sort((a, b) => a.value - b.value || b.point.observation.score - a.point.observation.score);
  return values[0]?.point || null;
}

function renderSolver(points, context) {
  refs.targetLabel.textContent = `${Math.round(state.target * 100)}%`;
  if (state.resource === "rank" || !points.length || !state.solverMin) {
    refs.solverResult.className = "solver-result disabled";
    refs.solverResult.textContent = "Choose a lens with comparable resource data to solve for efficiency.";
    return;
  }
  const winner = context.winner || solve(points, context.normalization);
  if (!winner) {
    refs.solverResult.className = "solver-result disabled";
    refs.solverResult.textContent = "No visible configuration clears this target with the selected resource.";
    return;
  }
  const best = Math.max(...points.map(point => point.observation.score));
  const resource = state.solverMin === "x" ? state.resource : state.solverMin;
  const value = resourceValue(winner, resource, context.normalization);
  refs.solverResult.className = "solver-result";
  refs.solverResult.innerHTML =
    `<strong style="color:${colorFor(winner.entity, winner.observation)}">${escapeHTML(entityLabel(winner.entity))}</strong>` +
    `<span class="result-metric">${formatNumber(winner.observation.score, metricFormat())}</span> capability · ` +
    `${formatNumber(value, RESOURCE_META[resource].format)} ${escapeHTML(RESOURCE_META[resource].short)}<br>` +
    `Clears ${formatNumber(best * state.target, metricFormat())}.`;
}

function renderFrontier(frontier) {
  refs.frontierCount.textContent = String(state.frontier && state.resource !== "rank" ? frontier.length : 0);
  refs.frontierList.innerHTML = "";
  if (!state.frontier || state.resource === "rank") {
    const item = document.createElement("li");
    item.className = "frontier-empty";
    item.textContent = state.resource === "rank"
      ? "A capability-only ranking has no resource frontier."
      : "Turn on the frontier overlay to inspect non-dominated configurations.";
    refs.frontierList.append(item);
    return;
  }
  for (const [index, point] of frontier.entries()) {
    const item = document.createElement("li");
    const number = document.createElement("span");
    number.className = "frontier-index";
    number.textContent = String(index + 1).padStart(2, "0");
    const content = document.createElement("span");
    content.className = "frontier-model";
    const name = document.createElement("b");
    name.textContent = entityLabel(point.entity);
    name.style.color = colorFor(point.entity, point.observation);
    const values = document.createElement("span");
    const x = resourceValue(point, state.resource, renderContext?.normalization);
    values.textContent = `${formatNumber(point.observation.score, metricFormat())} · ${formatNumber(x, RESOURCE_META[state.resource].format)}`;
    content.append(name, values);
    item.append(number, content);
    item.addEventListener("click", () => togglePin(point.entity.id));
    refs.frontierList.append(item);
  }
}

function togglePin(entityId) {
  const pinned = state.pinned[state.mode];
  if (pinned.has(entityId)) pinned.delete(entityId);
  else {
    if (pinned.size >= 5) pinned.delete(pinned.values().next().value);
    pinned.add(entityId);
  }
  render();
}

function renderCompare(points) {
  const pinned = state.pinned[state.mode];
  const byId = new Map(points.map(point => [point.entity.id, point]));
  const selected = [...pinned].map(id => byId.get(id)).filter(Boolean);
  refs.compareTray.hidden = !selected.length;
  refs.compareGrid.innerHTML = "";
  for (const point of selected) {
    const item = document.createElement("div");
    item.className = "compare-item";
    item.style.borderTopColor = colorFor(point.entity, point.observation);
    const name = document.createElement("strong");
    name.textContent = entityLabel(point.entity);
    const value = document.createElement("span");
    const x = resourceValue(point, state.resource, renderContext?.normalization);
    value.textContent = state.resource === "rank"
      ? formatNumber(point.observation.score, metricFormat())
      : `${formatNumber(point.observation.score, metricFormat())} · ${formatNumber(x, RESOURCE_META[state.resource].format)}`;
    item.append(name, value);
    item.addEventListener("click", () => togglePin(point.entity.id));
    refs.compareGrid.append(item);
  }
}

function syncURL() {
  const url = new URL(window.location.href);
  url.search = "";
  if (state.mode !== "model") url.searchParams.set("mode", state.mode);
  if (state.metric !== (state.mode === "model" ? "aa-intelligence" : "agent-index")) url.searchParams.set("lens", state.metric);
  if (state.resource !== "blend") url.searchParams.set("x", state.resource);
  window.history.replaceState(null, "", url);
}

function render() {
  const points = filteredPoints();
  refs.visibleCount.textContent = String(points.length);
  refs.empty.hidden = points.length > 0;
  renderFamilies();
  renderHeader(points);
  renderContext = state.resource === "rank" ? renderRanking(points) : renderScatter(points);
  renderSolver(renderContext.comparable, renderContext);
  renderFrontier(renderContext.frontier);
  renderCompare(points);
  syncToggle(refs.frontierToggle, state.frontier);
  syncToggle(refs.logToggle, state.log);
  syncURL();
}

function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  state.metric = mode === "model" ? "aa-intelligence" : "agent-index";
  state.search = "";
  refs.search.value = "";
  document.querySelectorAll(".mode").forEach(button => {
    const active = button.dataset.mode === mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $(".filters-panel h2").textContent = mode === "model" ? "Models" : "Agent families";
  populateMetricSelect();
  populateResourceControls({ reset: true });
  render();
}

function wireEvents() {
  document.querySelectorAll(".mode").forEach(button =>
    button.addEventListener("click", () => setMode(button.dataset.mode))
  );
  refs.metric.addEventListener("change", event => {
    state.metric = event.target.value;
    state.hiddenEntities[state.mode].clear();
    populateResourceControls({ reset: true });
    render();
  });
  refs.resource.addEventListener("change", event => {
    state.resource = event.target.value;
    state.log = false;
    populateResourceControls();
    render();
  });
  refs.size.addEventListener("change", event => {
    state.size = event.target.value;
    render();
  });
  refs.blendWeight.addEventListener("input", event => {
    state.blendWeight = Number(event.target.value) / 100;
    refs.blendValue.textContent = `${event.target.value} / ${100 - Number(event.target.value)}`;
    render();
  });
  refs.frontierToggle.addEventListener("click", () => {
    state.frontier = !state.frontier;
    render();
  });
  refs.logToggle.addEventListener("click", () => {
    if (refs.logToggle.disabled) return;
    state.log = !state.log;
    render();
  });
  refs.targetRange.addEventListener("input", event => {
    state.target = Number(event.target.value) / 100;
    render();
  });
  refs.search.addEventListener("input", event => {
    state.search = event.target.value.trim().toLowerCase();
    render();
  });
  $("#show-all").addEventListener("click", () => {
    state.hiddenFamilies[state.mode].clear();
    state.hiddenEntities[state.mode].clear();
    render();
  });
  $("#hide-all").addEventListener("click", () => {
    state.hiddenEntities[state.mode] = new Set(allMetricObservations().map(item => item.entity_id));
    render();
  });
  $("#top-only").addEventListener("click", () => {
    const ranked = [...allMetricObservations()].sort((a, b) => b.score - a.score);
    const keep = new Set(ranked.slice(0, 12).map(item => item.entity_id));
    state.hiddenFamilies[state.mode].clear();
    state.hiddenEntities[state.mode] = new Set(ranked.filter(item => !keep.has(item.entity_id)).map(item => item.entity_id));
    render();
  });
  $("#clear-compare").addEventListener("click", () => {
    state.pinned[state.mode].clear();
    render();
  });
  window.addEventListener("keydown", event => {
    if (event.key === "/" && document.activeElement !== refs.search) {
      event.preventDefault();
      refs.search.focus();
    } else if (event.key === "Escape" && document.activeElement === refs.search) {
      refs.search.value = "";
      state.search = "";
      refs.search.blur();
      render();
    }
  });
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(render, 100);
  });
}

function readURLState() {
  const params = new URLSearchParams(window.location.search);
  if (params.get("mode") === "agent") state.mode = "agent";
  const requestedMetric = params.get("lens");
  if (requestedMetric && metrics.get(requestedMetric)?.entity_type === state.mode) state.metric = requestedMetric;
  const requestedResource = params.get("x");
  if (requestedResource && RESOURCE_META[requestedResource]) state.resource = requestedResource;
}

async function init() {
  try {
    const response = await fetch(DATA_URL);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    bundle = await response.json();
  } catch (error) {
    refs.empty.hidden = false;
    refs.empty.querySelector("span").textContent = "Could not load frontier data";
    refs.empty.querySelector("p").textContent = error.message;
    refs.freshness.lastElementChild.textContent = "Data unavailable";
    return;
  }

  metrics = new Map(bundle.metrics.map(metric => [metric.id, metric]));
  entities = {
    model: new Map(bundle.entities.model.map(entity => [entity.id, entity])),
    agent: new Map(bundle.entities.agent.map(entity => [entity.id, entity])),
  };
  observations = new Map();
  for (const item of bundle.observations) {
    if (!observations.has(item.metric_id)) observations.set(item.metric_id, []);
    observations.get(item.metric_id).push(item);
  }
  readURLState();
  const collected = new Date(bundle.collection.finished_at);
  refs.freshness.lastElementChild.textContent = `AA snapshot · ${collected.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" })}`;
  refs.statline.children[0].querySelector("dt").textContent = bundle.counts.models;
  refs.statline.children[1].querySelector("dt").textContent = bundle.counts.agents;
  refs.statline.children[2].querySelector("dt").textContent = bundle.counts.metrics;
  refs.collectionId.textContent = `snapshot ${bundle.collection.id.slice(0, 10)}`;
  document.querySelectorAll(".mode").forEach(button => {
    const active = button.dataset.mode === state.mode;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $(".filters-panel h2").textContent = state.mode === "model" ? "Models" : "Agent families";
  populateMetricSelect();
  populateResourceControls();
  wireEvents();
  render();
}

init();
