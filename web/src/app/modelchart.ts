import { alpha, fontOf, mix, token, SEQUENTIAL } from "./tokens";
// @ts-ignore
import { price, duration, integer } from "../lib/format.js";
import type { Size } from "./echart";

/* ECharts options for the Models chart: any two axes in 2D, any three in 3D, one engine for both. */

export type Axis = { title: string; log: boolean; unit?: string; percent?: boolean; higherBetter: boolean };
export type Row = {
  key: string; label: string; short: string; colour: string; group: string; hover: string;
  v: number[]; best: boolean; mine: boolean; pinned: boolean; dim: boolean; named: boolean;
};

const fmtFor = (a: Axis) => (v: number) => {
  if (!Number.isFinite(v)) return "";
  if (a.unit === "currency") return price(v);
  if (a.unit === "duration") return duration(v);
  if (a.unit === "integer") return integer(v);
  if (a.unit === "speed") return `${Math.round(v)}/s`;
  if (a.percent) return `${Math.round(v * 100)}%`;
  return Math.abs(v) >= 100 ? String(Math.round(v)) : String(+v.toFixed(1));
};

function palette() {
  return {
    ink: token("--foreground"), muted: token("--muted-foreground"), grid: token("--border"), card: token("--card"),
    pop: token("--popover"), surface: token("--background"), sans: fontOf("--font-sans"), data: fontOf("--font-data"),
    ramp: SEQUENTIAL.map(token),
  };
}

function tooltip(p: ReturnType<typeof palette>) {
  return {
    trigger: "item", confine: true, transitionDuration: 0, borderWidth: 1, padding: [8, 10],
    backgroundColor: p.pop, borderColor: alpha(p.ink, 0.16), textStyle: { color: p.ink, fontFamily: p.sans, fontSize: 12 },
    extraCssText: "border-radius:10px;box-shadow:0 18px 40px -12px rgba(0,0,0,.55);",
    formatter: (params: any) => params.data?.hover ?? "",
  };
}

// ---------------------------------------------------------------- 2D

export function option2d(o: {
  rows: Row[]; axes: [Axis, Axis]; colourAxis?: Axis | null; step: boolean; tiers: boolean; labels: boolean; size: Size;
}) {
  const p = palette();
  const [ax, ay] = o.axes;
  // Names sit beside their points inside the plot; overlaps are nudged apart, and what still collides is hidden.
  const gutter = 24;
  const axis = (a: Axis, name: string) => ({
    type: a.log ? "log" : "value", name, nameLocation: "middle", nameGap: name === "x" ? 32 : 46, scale: !a.log,
    nameTextStyle: { color: p.muted, fontFamily: p.sans, fontSize: 12 },
    axisLabel: { color: p.muted, fontFamily: p.data, fontSize: 11, formatter: fmtFor(a), hideOverlap: true },
    axisLine: { show: false }, axisTick: { show: false },
    splitLine: { lineStyle: { color: p.grid } },
  });
  const xAxis = { ...axis(ax, "x"), name: ax.title + (ax.log ? " · log" : "") };
  const yAxis = { ...axis(ay, "y"), name: ay.title + (ay.log ? " · log" : "") };
  const series: any[] = [];

  // Tier lines join one model's reasoning tiers, so its trade-off reads as one curve.
  if (o.tiers) {
    const groups = new Map<string, Row[]>();
    for (const r of o.rows) if (!r.dim) (groups.get(r.group) ?? groups.set(r.group, []).get(r.group)!).push(r);
    for (const g of groups.values()) {
      if (g.length < 2) continue;
      g.sort((a, b) => a.v[0] - b.v[0]);
      series.push({ type: "line", silent: true, symbol: "none", z: 1, data: g.map((r) => [r.v[0], r.v[1]]),
        lineStyle: { color: alpha(token(g[0].colour), 0.32), width: 1.2 }, animation: false });
    }
  }
  // The staircase of best trade-offs: below and right of it is paying for nothing.
  const best = o.rows.filter((r) => r.best && !r.dim).sort((a, b) => a.v[0] - b.v[0]);
  if (o.step && best.length > 1) {
    series.push({ type: "line", step: "end", silent: true, symbol: "none", z: 2, animation: false,
      data: [...best.map((r) => [r.v[0], r.v[1]]), [Math.max(...o.rows.map((r) => r.v[0])), best.at(-1)!.v[1]]],
      lineStyle: { color: alpha(p.ink, 0.85), width: 1.8 } });
  }
  const side = false;
  const point = (r: Row) => {
    const named = o.labels && r.named && !r.dim;
    return {
      name: r.key, value: o.colourAxis ? [r.v[0], r.v[1], r.v[2]] : [r.v[0], r.v[1]], hover: r.hover,
      symbolSize: r.best ? 11 : 8,
      itemStyle: {
        ...(o.colourAxis ? {} : { color: token(r.colour) }), opacity: r.dim ? 0.15 : 1,
        borderColor: r.best && !r.dim ? p.ink : p.card, borderWidth: r.best && !r.dim ? 1.6 : 1,
      },
      // Named points carry their label in a gutter at the right, joined by a faint leader line.
      label: { show: named, formatter: tidy(r.short), color: o.colourAxis ? p.ink : token(r.colour) },
      labelLine: { show: named && side },
    };
  };
  const labelled = {
    label: { position: "right", fontFamily: p.sans, fontSize: 12, distance: 8 },
    labelLine: { lineStyle: { color: alpha(p.muted, 0.35), width: 1 }, length2: 4 },
    labelLayout: { moveOverlap: "shiftY", hideOverlap: true },
  };
  series.push({ type: "scatter", z: 3, data: o.rows.filter((r) => !r.best).map(point), emphasis: { scale: 1.5 }, ...labelled });
  series.push({ type: "scatter", z: 4, data: o.rows.filter((r) => r.best).map(point), emphasis: { scale: 1.5 }, ...labelled });
  // Rings: the person's own models, and pinned ones.
  const ringed = o.rows.filter((r) => (r.mine || r.pinned) && !r.dim);
  if (ringed.length) series.push({ type: "scatter", z: 5, silent: true, symbol: "circle", symbolSize: 21,
    data: ringed.map((r) => [r.v[0], r.v[1]]), itemStyle: { color: "rgba(0,0,0,0)", borderColor: p.ink, borderWidth: 1.6, opacity: 0.95 } });

  return {
    backgroundColor: "transparent", animationDuration: 250, animationDurationUpdate: 250,
    textStyle: { fontFamily: p.sans },
    grid: { left: 64, right: gutter, top: o.colourAxis ? 52 : 16, bottom: 52 },
    xAxis, yAxis, tooltip: tooltip(p), series,
    // Scroll to zoom, drag to pan; double-click resets.
    dataZoom: [{ type: "inside", xAxisIndex: 0, filterMode: "none" }, { type: "inside", yAxisIndex: 0, filterMode: "none" }],
    ...(o.colourAxis ? { visualMap: {
      type: "continuous", dimension: 2, seriesIndex: series.map((s, i) => (s.type === "scatter" && !s.silent ? i : -1)).filter((i) => i >= 0),
      min: Math.min(...o.rows.map((r) => r.v[2])), max: Math.max(...o.rows.map((r) => r.v[2])),
      inRange: { color: o.colourAxis.higherBetter ? p.ramp : [...p.ramp].reverse() }, calculable: true,
      orient: "horizontal", left: 72, top: 0, itemHeight: 200, itemWidth: 10,
      text: ["", o.colourAxis.title], textGap: 10, textStyle: { color: p.muted, fontFamily: p.sans, fontSize: 11 },
      formatter: fmtFor(o.colourAxis),
    } } : {}),
  };
}

// ---------------------------------------------------------------- 3D

export const VIEWS3D: Record<string, { alpha: number; beta: number }> = {
  iso: { alpha: 22, beta: 38 }, front: { alpha: 0, beta: 0 }, side: { alpha: 0, beta: 90 }, top: { alpha: 89, beta: 0 },
};

// "Claude Opus 5.5 (max with fallback)" -> "Claude Opus 5.5 (max)": a 3D label has little room.
const tidy = (name: string) => name.replace(/\(([a-z]+)[^)]*\)/i, "($1)");

// echarts-gl's axes are linear; a log axis is drawn on log10 values with labels in the data's units.
const lg = (a: Axis) => (v: number) => (a.log ? Math.log10(v) : v);

export function option3d(o: {
  size?: Size;
  rows: Row[]; axes: [Axis, Axis, Axis]; labels: boolean; selected: string | null; view: string;
  surface?: { a: number[]; b: number[]; z: Array<Array<number | null>> } | null;
  probe?: { a: number; b: number; z: number | null; a0: number; b0: number } | null;
  lines?: Array<{ colour: string; dash: boolean; points: number[][] }>;
}) {
  const p = palette();
  const [ax, ay, az] = o.axes;
  const [fx, fy, fz] = o.axes.map(lg);
  const zFloor = Math.min(...o.rows.map((r) => fz(r.v[2])));
  const axis = (a: Axis) => ({
    type: "value", name: a.title, scale: true,
    nameTextStyle: { color: p.muted, fontFamily: p.sans, fontSize: 12 },
    axisLabel: { textStyle: { color: p.muted, fontFamily: p.data, fontSize: 11 }, formatter: (v: number) => fmtFor(a)(a.log ? 10 ** v : v) },
    axisLine: { lineStyle: { color: alpha(p.ink, 0.25) } }, axisTick: { show: false },
    splitLine: { lineStyle: { color: alpha(p.ink, 0.08) } }, splitArea: { show: false },
    axisPointer: { lineStyle: { color: alpha(p.ink, 0.6) }, label: { formatter: (v: number) => fmtFor(a)(a.log ? 10 ** Number(v) : Number(v)),
      textStyle: { color: p.ink, backgroundColor: p.pop, fontFamily: p.data } } },
  });
  const item = (r: Row) => ({
    id: r.key, name: r.named ? r.short : "", value: [fx(r.v[0]), fy(r.v[1]), fz(r.v[2])], hover: r.hover,
    itemStyle: { color: token(r.colour), opacity: r.dim ? 0.12 : r.best ? 1 : 0.75, borderColor: r.best ? p.ink : "transparent", borderWidth: r.best ? 1 : 0 },
  });
  const series: any[] = [];
  if (o.surface) {
    const data: any[] = [];
    o.surface.b.forEach((b, j) => o.surface!.a.forEach((a, i) => data.push([fx(a), fy(b), o.surface!.z[j][i] === null ? "-" : fz(o.surface!.z[j][i] as number)])));
    series.push({ type: "surface", silent: true, data, shading: "color", wireframe: { show: true, lineStyle: { color: alpha(p.ink, 0.12), width: 1 } },
      itemStyle: { opacity: 0.45 }, dataShape: [o.surface.b.length, o.surface.a.length] });
  }
  for (const l of o.lines ?? []) series.push({ type: "line3D", silent: true, data: l.points.map((q) => [fx(q[0]), fy(q[1]), fz(q[2])]),
    lineStyle: { color: token(l.colour), width: 3, opacity: l.dash ? 0.7 : 1 } });
  series.push({ type: "scatter3D", data: o.rows.filter((r) => !r.best).map(item), symbolSize: 7,
    emphasis: { itemStyle: { color: p.ink }, label: { show: false } } });
  const ringed = o.rows.filter((r) => (r.mine || r.pinned) && !r.dim);
  if (ringed.length) series.push({ type: "scatter3D", silent: true, symbol: "circle", symbolSize: 20,
    data: ringed.map((r) => [fx(r.v[0]), fy(r.v[1]), fz(r.v[2])]), itemStyle: { color: "rgba(0,0,0,0)", borderColor: p.ink, borderWidth: 2 } });
  // Stems: each best trade-off dropped to the floor, which is what places it in depth.
  for (const r of o.rows.filter((row) => row.best && !row.dim)) {
    series.push({ type: "line3D", silent: true, data: [[fx(r.v[0]), fy(r.v[1]), fz(r.v[2])], [fx(r.v[0]), fy(r.v[1]), zFloor]],
      lineStyle: { color: alpha(token(r.colour), r.key === o.selected ? 1 : 0.45), width: r.key === o.selected ? 3 : 1.5 } });
  }
  if (o.probe) {
    const q = o.probe;
    series.push({ type: "line3D", silent: true, lineStyle: { color: alpha(p.ink, 0.75), width: 2 },
      data: [[fx(q.a0), fy(q.b0), zFloor], [fx(q.a), fy(q.b0), zFloor], [fx(q.a), fy(q.b), zFloor], [fx(q.a0), fy(q.b), zFloor], [fx(q.a0), fy(q.b0), zFloor]] });
    if (q.z !== null) series.push({ type: "line3D", silent: true, lineStyle: { color: p.ink, width: 4 },
      data: [[fx(q.a), fy(q.b), zFloor], [fx(q.a), fy(q.b), fz(q.z)]] });
  }
  series.push({ type: "scatter3D", data: o.rows.filter((r) => r.best).map((r) => ({ ...item(r), name: "" })), symbolSize: 12,
    emphasis: { itemStyle: { color: p.ink } } });
  // Names last, one series each: echarts-gl packs a series' labels into one 256px texture and, when they overflow it,
  // draws none at all. A series per named point gives each label its own texture.
  // At most eight names, best first, skipping any that would sit on top of one already named.
  const span = [0, 1, 2].map((k) => {
    const vs = o.rows.map((r) => [fx, fy, fz][k](r.v[k]));
    return Math.max(1e-9, Math.max(...vs) - Math.min(...vs));
  });
  const pos = (r: Row) => [fx(r.v[0]) / span[0], fy(r.v[1]) / span[1], fz(r.v[2]) / span[2]];
  const placed: number[][] = [];
  const toName = o.rows.filter((r) => r.named && !r.dim).sort((a, b) => Number(b.mine) - Number(a.mine) || b.v[2] - a.v[2]).filter((r) => {
    if (placed.length >= 8) return false;
    const q = pos(r);
    if (placed.some((c) => Math.hypot(c[0] - q[0], c[1] - q[1], (c[2] - q[2]) * 2.5) < 0.18)) return false;
    placed.push(q);
    return true;
  });
  if (o.labels) for (const r of toName) {
    series.push({ type: "scatter3D", data: [{ ...item(r), name: tidy(r.short) }], symbolSize: r.best ? 12 : 7,
      label: { show: true, formatter: "{b}", color: p.ink, fontSize: 13, distance: 3 },
      emphasis: { itemStyle: { color: p.ink } } });
  }
  const view = VIEWS3D[o.view] ?? VIEWS3D.iso;
  // Pull the camera back in a short or narrow chart, so the whole box stays in frame at any window size.
  const fit = o.size ? Math.max(1, 640 / Math.min(o.size.height, o.size.width * 0.62)) : 1;
  return {
    backgroundColor: "transparent", textStyle: { fontFamily: p.sans }, tooltip: tooltip(p),
    xAxis3D: axis(ax), yAxis3D: axis(ay), zAxis3D: axis(az),
    grid3D: {
      boxWidth: 125, boxDepth: 125, boxHeight: 85, top: -20,
      environment: "none", light: { main: { intensity: 1.1 }, ambient: { intensity: 0.4 } },
      axisPointer: { show: true, lineStyle: { color: alpha(p.ink, 0.5) } },
      viewControl: { projection: "perspective", alpha: view.alpha, beta: view.beta, distance: Math.round(205 * fit), minDistance: 110, maxDistance: 520,
        damping: 0.85, rotateSensitivity: 1.2, zoomSensitivity: 1.2, panSensitivity: 1, animationDurationUpdate: 500 },
      axisLine: { lineStyle: { color: alpha(p.ink, 0.25) } },
    },
    ...(o.surface ? { visualMap: { show: false, seriesIndex: 0, dimension: 2, min: Math.min(...o.surface.z.flat().filter((v): v is number => v !== null).map(fz)),
      max: Math.max(...o.surface.z.flat().filter((v): v is number => v !== null).map(fz)), inRange: { color: p.ramp } } } : {}),
    series,
  };
}

export { mix };

// ---------------------------------------------------------------- plans

/* How much work each plan gets done on one benchmark: x is tasks per week (the plan's weekly API-equivalent value over
   a model's cost per task), y the model's score. Each plan's best trade-offs (more tasks and a higher score than any
   alternative on the same plan) are joined into that plan's curve, named at its end; everything else is a faint dot.
   A plan only covers its provider's models, so each curve holds only those. */
export type PlanSeries = { name: string; colour: string; dash: boolean; points: Array<{ key: string; label: string; short: string; tasks: number; score: number; hover: string; best: boolean }> };

export function optionPlans(o: { plans: PlanSeries[]; score: Axis; labels: boolean; size: Size; perDollar?: boolean }) {
  const p = palette();
  const series: any[] = [];
  for (const plan of o.plans) {
    const colour = token(plan.colour);
    const best = plan.points.filter((q) => q.best).sort((a, b) => a.tasks - b.tasks);
    series.push({
      type: "scatter", name: plan.name, z: 2, symbolSize: 7, silent: false,
      data: plan.points.filter((q) => !q.best).map((q) => ({ name: q.key, value: [q.tasks, q.score], hover: q.hover })),
      itemStyle: { color: alpha(colour, 0.35), borderWidth: 0 },
    });
    series.push({
      type: "line", name: plan.name, z: 3, symbol: "circle", symbolSize: 9, smooth: false,
      data: best.map((q) => ({ name: q.key, value: [q.tasks, q.score], hover: q.hover,
        label: { show: o.labels, formatter: tidy(q.short), position: "top", color: p.ink, fontFamily: p.sans, fontSize: 11, distance: 6 } })),
      lineStyle: { color: colour, width: 2.2, type: plan.dash ? "dashed" : "solid" },
      itemStyle: { color: colour, borderColor: p.card, borderWidth: 1.5 },
      labelLayout: { hideOverlap: true, moveOverlap: "shiftY" },
      endLabel: { show: true, formatter: plan.name, color: colour, fontFamily: p.sans, fontSize: 13, fontWeight: 600, distance: 10 },
      emphasis: { focus: "series" },
    });
  }
  const axis = (a: Axis, name: string, log: boolean) => ({
    type: log ? "log" : "value", name, nameLocation: "middle", nameGap: name.startsWith("Tasks") ? 32 : 46, scale: !log,
    nameTextStyle: { color: p.muted, fontFamily: p.sans, fontSize: 12 },
    axisLabel: { color: p.muted, fontFamily: p.data, fontSize: 11, formatter: fmtFor(a), hideOverlap: true },
    axisLine: { show: false }, axisTick: { show: false }, splitLine: { lineStyle: { color: p.grid } },
  });
  return {
    backgroundColor: "transparent", textStyle: { fontFamily: p.sans }, animationDuration: 250,
    grid: { left: 64, right: 130, top: 20, bottom: 52 },
    tooltip: tooltip(p),
    xAxis: axis({ title: o.perDollar ? "Tasks / $" : "Tasks / week", log: true, unit: o.perDollar ? undefined : "integer", higherBetter: true },
      o.perDollar ? "Tasks per dollar of subscription · log" : "Tasks per weekly limit · log", true),
    yAxis: axis(o.score, o.score.title, false),
    dataZoom: [{ type: "inside", xAxisIndex: 0, filterMode: "none" }, { type: "inside", yAxisIndex: 0, filterMode: "none" }],
    series,
  };
}
