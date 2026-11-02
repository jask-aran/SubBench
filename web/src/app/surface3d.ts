import { theme } from "./plot";
import { alpha, mix, token, SEQUENTIAL } from "./tokens";
import { hoverHTML } from "./charts";
// @ts-ignore
import { price, duration, score as fmtScore } from "../lib/format.js";
// @ts-ignore
import { familyColor } from "../lib/frontier.js";

/* The 3D view: the attainment surface, the points, the 3D Pareto set, the person's models, a selected point's drop
   line and the budget probe. Built as plain Plotly traces so the explorer can switch to it like any other view. */

export const CAMERAS: Record<string, any> = {
  iso: { eye: { x: -1.5, y: -1.65, z: 0.9 }, up: { x: 0, y: 0, z: 1 }, center: { x: 0, y: 0, z: -0.12 } },
  cost: { eye: { x: 0, y: -2.4, z: 0.02 }, up: { x: 0, y: 0, z: 1 }, center: { x: 0, y: 0, z: 0 } },
  time: { eye: { x: 2.4, y: 0, z: 0.02 }, up: { x: 0, y: 0, z: 1 }, center: { x: 0, y: 0, z: 0 } },
  top: { eye: { x: 0, y: -0.001, z: 2.5 }, up: { x: 0, y: 1, z: 0 }, center: { x: 0, y: 0, z: 0 } },
};

export type SurfaceInput = {
  visible: any[]; front: any[]; frontKeys: Set<string>; grid: any; ranges: { cost: [number, number]; time: [number, number] };
  caps: { cost: number; time: number }; probe: any; selected: string | null; showSurface: boolean; labels: boolean;
  frontierOnly: boolean; mine: Set<string>; meta: any; hover: (p: any) => string; key: (p: any) => string; label: (p: any) => string;
};

export function surfaceTraces(s: SurfaceInput) {
  const t = theme();
  const traces: any[] = [];
  const g = s.grid;
  const zFloor = Math.min(...s.visible.map((p) => p.observation.score));
  const fmt = s.meta?.format;
  if (g && s.showSurface) {
    // Plotly's surface takes z[row = time][column = cost].
    const z = Array.from({ length: g.steps + 1 }, (_, j) => Array.from({ length: g.steps + 1 }, (_, i) => g.cells[i][j]?.observation.score ?? null));
    const text = Array.from({ length: g.steps + 1 }, (_, j) => Array.from({ length: g.steps + 1 }, (_, i) => {
      const best = g.cells[i][j];
      return best ? hoverHTML(`≤ ${price(g.cAt(i))} · ≤ ${duration(g.tAt(j))}`, [["Best", s.label(best)], ["Score", fmtScore(best.observation.score, fmt)]]) : "None";
    }));
    traces.push({
      type: "surface", x: Array.from({ length: g.steps + 1 }, (_, i) => g.cAt(i)), y: Array.from({ length: g.steps + 1 }, (_, j) => g.tAt(j)), z,
      colorscale: SEQUENTIAL.map((name, k) => [k / (SEQUENTIAL.length - 1), token(name)]), opacity: 0.6, showscale: true,
      hovertext: text, hovertemplate: "%{hovertext}<extra></extra>",
      contours: { z: { show: true, usecolormap: true, width: 1 } },
      lighting: { ambient: 0.75, diffuse: 0.5, specular: 0.08, roughness: 0.9 },
      colorbar: { thickness: 8, len: 0.45, x: 0.98, y: 0.55, outlinewidth: 0, tickfont: { family: t.data, size: 10, color: t.muted },
        title: { text: "Best score", font: { family: t.sans, size: 11, color: t.muted } }, tickformat: fmt === "percent" ? ".0%" : "" },
    });
  }
  const top = new Set([...s.front].sort((a, b) => b.observation.score - a.observation.score).slice(0, 10).map(s.key));
  const groups = [
    { pts: s.visible.filter((p) => !s.frontKeys.has(s.key(p))), size: 4.5, opacity: s.frontierOnly ? 0.12 : 0.7, text: false },
    { pts: s.visible.filter((p) => s.frontKeys.has(s.key(p)) && !top.has(s.key(p))), size: 7.5, opacity: 1, text: false },
    { pts: s.visible.filter((p) => top.has(s.key(p))), size: 8, opacity: 1, text: s.labels },
  ];
  for (const group of groups) {
    if (!group.pts.length) continue;
    traces.push({
      type: "scatter3d", mode: group.text ? "markers+text" : "markers",
      x: group.pts.map((p) => p.observation.resources.cost), y: group.pts.map((p) => p.observation.resources.time), z: group.pts.map((p) => p.observation.score),
      customdata: group.pts.map(s.key), hovertext: group.pts.map(s.hover), hovertemplate: "%{hovertext}<extra></extra>",
      text: group.pts.map((p) => p.entity.label), textposition: "top center", textfont: { family: t.sans, size: 12, color: t.ink },
      marker: { size: group.size, opacity: group.opacity, color: group.pts.map((p) => token(familyColor(p.entity.family))),
        line: { color: group.size > 5 ? t.ink : token("--background"), width: group.size > 5 ? 1.5 : 0.5 } },
    });
  }
  const mine = s.visible.filter((p) => s.mine.has(p.entity.id));
  if (mine.length) traces.push({ type: "scatter3d", mode: "markers", hoverinfo: "skip",
    x: mine.map((p) => p.observation.resources.cost), y: mine.map((p) => p.observation.resources.time), z: mine.map((p) => p.observation.score),
    marker: { symbol: "circle-open", size: 14, color: t.ink, line: { width: 2 } } });
  const chosen = s.visible.find((p) => s.key(p) === s.selected);
  if (chosen) {
    const o = chosen.observation;
    traces.push({ type: "scatter3d", mode: "lines", hoverinfo: "skip", x: [o.resources.cost, o.resources.cost], y: [o.resources.time, o.resources.time],
      z: [o.score, zFloor], line: { color: t.ink, width: 3, dash: "dash" } });
  }
  const c = s.caps, [c0] = s.ranges.cost, [t0] = s.ranges.time;
  traces.push({ type: "scatter3d", mode: "lines", hoverinfo: "skip", x: [c0, c.cost, c.cost, c0, c0], y: [t0, t0, c.time, c.time, t0],
    z: Array(5).fill(zFloor), line: { color: alpha(t.ink, 0.7), width: 4 } });
  if (s.probe) traces.push({ type: "scatter3d", mode: "lines+markers", hoverinfo: "skip", x: [c.cost, c.cost], y: [c.time, c.time],
    z: [zFloor, s.probe.observation.score], line: { color: t.ink, width: 5 }, marker: { size: [3, 6], color: t.ink } });
  return traces;
}

export function surfaceLayout(s: Pick<SurfaceInput, "ranges" | "meta">) {
  const t = theme();
  const card = token("--card");
  const ticks = (lo: number, hi: number, candidates: number[], fmt: (v: number) => string) => {
    const vals = candidates.filter((v) => v >= lo * 0.9 && v <= hi * 1.1);
    return { tickvals: vals, ticktext: vals.map(fmt) };
  };
  const axis = (title: string, type: string, extra: Record<string, unknown> = {}) => ({
    title: { text: title, font: { family: t.sans, size: 13, color: t.muted } }, type,
    tickfont: { family: t.data, size: 11, color: t.muted }, gridcolor: mix(card, t.ink, 0.12), zerolinecolor: mix(card, t.ink, 0.12),
    linecolor: mix(card, t.ink, 0.25), showbackground: true, backgroundcolor: mix(card, t.ink, 0.03), showspikes: false, ...extra,
  });
  const r = s.ranges;
  return {
    ...t.layout, autosize: true, margin: { l: 0, r: 0, t: 0, b: 0 }, uirevision: "keep", hovermode: "closest",
    scene: {
      uirevision: "keep", camera: CAMERAS.iso, aspectmode: "manual", aspectratio: { x: 1.25, y: 1.25, z: 0.85 }, dragmode: "orbit",
      xaxis: axis("Cost / task", "log", ticks(r.cost[0], r.cost[1], [0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30], price)),
      yaxis: axis("Time / task", "log", ticks(r.time[0], r.time[1], [1, 3, 10, 30, 60, 180, 600, 1800, 3600, 10800], duration)),
      zaxis: axis(s.meta?.label ?? "Score", "linear", { tickformat: s.meta?.format === "percent" ? ".0%" : "" }),
    },
  };
}
