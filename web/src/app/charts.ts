import { alpha, token } from "./tokens";
import { theme } from "./plot";
// @ts-ignore
import { price, duration, integer, score as fmtScore } from "../lib/format.js";

/* Trace builders shared by the pages, so a frontier or a value line reads the same wherever it appears. */

export const UNIT_FORMAT: Record<string, (v: number) => string> = {
  currency: price, duration, integer, speed: (v) => `${Math.round(v)} tok/s`, blend: (v) => v.toFixed(3),
};

export type ScatterPoint = {
  id: string; x: number; y: number; colour: string; label: string; short?: string; hover: string;
  frontier?: boolean; mine?: boolean; pinned?: boolean; dim?: boolean; group?: string; size?: number;
};

/** A frontier scatter: dim points, the step line of the Pareto set, the set's points outlined, the person's own models
 *  ringed, and direct labels only where they earn it. */
export function frontierScatter(points: ScatterPoint[], opts: { xLog: boolean; xTitle: string; yTitle: string; yFormat: string; xFormat?: string; groups?: boolean; labels?: boolean; height?: number; step?: boolean; yLog?: boolean; yTickFormat?: string; gutter?: boolean; fill?: boolean }) {
  const t = theme();
  const traces: any[] = [];
  const surface = token("--background");

  if (opts.groups) {
    const groups = new Map<string, ScatterPoint[]>();
    for (const p of points) if (p.group && !p.dim) { if (!groups.has(p.group)) groups.set(p.group, []); groups.get(p.group)!.push(p); }
    for (const group of groups.values()) {
      if (group.length < 2) continue;
      group.sort((a, b) => a.x - b.x);
      traces.push({ type: "scatter", mode: "lines", x: group.map((p) => p.x), y: group.map((p) => p.y), hoverinfo: "skip",
        line: { color: alpha(token(group[0].colour), 0.35), width: 1.2 } });
    }
  }

  const frontier = points.filter((p) => p.frontier && !p.dim).sort((a, b) => a.x - b.x);
  if (frontier.length > 1 && opts.step !== false) {
    const xs = frontier.map((p) => p.x), ys = frontier.map((p) => p.y);
    const maxX = Math.max(...points.map((p) => p.x));
    traces.push({ type: "scatter", mode: "lines", x: [...xs, maxX], y: [...ys, ys.at(-1)], hoverinfo: "skip",
      line: { color: alpha(t.ink, 0.85), width: 1.8, shape: "hv" } });
  }

  const ordered = [...points].sort((a, b) => Number(!!a.frontier) - Number(!!b.frontier) || Number(!a.dim) - Number(!b.dim));
  traces.push({
    type: "scatter", mode: "markers", x: ordered.map((p) => p.x), y: ordered.map((p) => p.y),
    customdata: ordered.map((p) => p.id), hovertext: ordered.map((p) => p.hover), hovertemplate: "%{hovertext}<extra></extra>",
    marker: {
      color: ordered.map((p) => (p.dim ? alpha(token(p.colour), 0.18) : token(p.colour))),
      size: ordered.map((p) => p.size ?? (p.frontier ? 10 : 8)),
      line: { color: ordered.map((p) => (p.frontier && !p.dim ? t.ink : surface)), width: ordered.map((p) => (p.frontier && !p.dim ? 1.6 : 1.2)) },
    },
  });

  const ringed = points.filter((p) => (p.mine || p.pinned) && !p.dim);
  if (ringed.length) {
    traces.push({ type: "scatter", mode: "markers", x: ringed.map((p) => p.x), y: ringed.map((p) => p.y), hoverinfo: "skip",
      marker: { symbol: "circle-open", size: 20, color: ringed.map((p) => (p.mine ? t.ink : alpha(t.ink, 0.6))), line: { width: 1.6 } } });
  }

  // Explicit ranges, so a gutter label's leader line can start exactly at its point.
  const xs = points.map((p) => (opts.xLog ? Math.log10(p.x) : p.x)), ys = points.map((p) => p.y);
  const [x0, x1] = pad(Math.min(...xs), Math.max(...xs), 0.04), [y0, y1] = pad(Math.min(...ys), Math.max(...ys), 0.07);
  const gutter = opts.gutter && opts.labels !== false ? gutterLabels(points, { xLog: opts.xLog, x0, x1, y0, y1, height: opts.height ?? 480, t }) : null;
  const annotations = gutter ? gutter.annotations : opts.labels === false ? [] : labelAnnotations(points.filter((p) => !p.dim && (p.frontier || p.mine || p.pinned)), t, opts.xLog);
  const layout = {
    ...t.layout,
    // fill: the chart takes its container's size (a canvas sized to the window); height then only spaces labels.
    height: opts.fill ? undefined : opts.height, autosize: true,
    margin: { l: 60, r: gutter ? 190 : 18, t: 10, b: 50 },
    // A small panel labels decades only; 1-2-5 subdivisions crowd its axis.
    xaxis: { ...t.axis, ...(opts.xLog && (opts.height ?? 500) <= 320 ? { dtick: 1 } : {}), type: opts.xLog ? "log" : "linear", range: [x0, x1],
      title: { ...t.axis.title, text: opts.xTitle + (opts.xLog ? " · log scale" : "") }, tickformat: opts.xFormat },
    yaxis: { ...t.axis, ...(opts.yLog ? { dtick: 1 } : {}), type: opts.yLog ? "log" : "linear", ...(opts.yLog ? {} : { range: [y0, y1] }),
      title: { ...t.axis.title, text: opts.yTitle }, tickformat: opts.yFormat },
    annotations,
    shapes: gutter?.shapes ?? [],
  };
  return { traces, layout };
}

const pad = (lo: number, hi: number, f: number): [number, number] => { const d = (hi - lo) * f || Math.abs(hi || 1) * 0.1; return [lo - d, hi + d]; };

/*
 * Names in a gutter at the chart's right, each joined to its model by a faint leader line (the reference design's
 * reading aid): the plot itself stays free of text. One name per model -- its best tier -- for the models that reach
 * the frontier or that the person uses, spread apart so no two collide.
 */
function gutterLabels(points: ScatterPoint[], o: { xLog: boolean; x0: number; x1: number; y0: number; y1: number; height: number; t: ReturnType<typeof theme> }) {
  const best = new Map<string, ScatterPoint>();
  const keep = new Set<string>();
  for (const p of points) {
    if (p.dim) continue;
    const g = p.group || p.id;
    if (p.frontier || p.mine || p.pinned) keep.add(g);
    if (!best.has(g) || p.y > best.get(g)!.y) best.set(g, p);
  }
  const chosen = [...keep].map((g) => best.get(g)!).filter(Boolean).sort((a, b) => b.y - a.y).slice(0, Math.max(8, Math.floor((o.height - 60) / 26)));
  const plotH = Math.max(120, o.height - 60);
  const gap = ((o.y1 - o.y0) * 17) / plotH;
  const placed: number[] = [];
  for (const p of chosen) placed.push(Math.min(p.y, placed.length ? placed.at(-1)! - gap : p.y, o.y1));
  // Pushed below the floor: shift the run back up from the bottom.
  for (let i = placed.length - 1; i >= 0; i--) {
    const floor = o.y0 + gap * (placed.length - 1 - i);
    if (placed[i] < floor) placed[i] = floor;
    if (i < placed.length - 1 && placed[i] < placed[i + 1] + gap) placed[i] = placed[i + 1] + gap;
  }
  const px = (p: ScatterPoint) => ((o.xLog ? Math.log10(p.x) : p.x) - o.x0) / (o.x1 - o.x0);
  const annotations = chosen.map((p, i) => ({
    xref: "paper", yref: "y", x: 1.015, y: placed[i], xanchor: "left", yanchor: "middle", showarrow: false,
    text: (p.short || p.label).replace(/\s*\((max|xhigh|high|medium|low|with fallback)\)$/i, "") + (p.mine ? "  ·  yours" : ""),
    font: { family: o.t.sans, size: 13, color: token(p.colour) },
  }));
  const shapes = chosen.map((p, i) => ({
    type: "path", xref: "paper", yref: "y", layer: "below",
    path: `M ${px(p)},${p.y} L 1.005,${placed[i]}`,
    line: { color: alpha(o.t.muted, 0.28), width: 1 },
  }));
  return { annotations, shapes };
}

function labelAnnotations(points: ScatterPoint[], t: ReturnType<typeof theme>, xLog: boolean) {
  // Direct labels are selective: the person's own models first, then the frontier from the top, and a label that
  // would land on one already placed is dropped (its point keeps its hover and its row in the side list). Overlap is
  // judged in axis-normalised units, since pixel positions are Plotly's to decide.
  const xv = (p: ScatterPoint) => (xLog ? Math.log10(p.x) : p.x);
  const xs = points.map(xv), ys = points.map((p) => p.y);
  const xSpan = Math.max(1e-9, Math.max(...xs) - Math.min(...xs)), ySpan = Math.max(1e-9, Math.max(...ys) - Math.min(...ys));
  const placed: Array<[number, number, number]> = [];
  const chosen: ScatterPoint[] = [];
  const order = [...points].sort((a, b) => Number(!!b.mine) - Number(!!a.mine) || Number(!!b.pinned) - Number(!!a.pinned) || b.y - a.y);
  for (const p of order) {
    if (chosen.length >= 10) break;
    const nx = (xv(p) - Math.min(...xs)) / xSpan, ny = (p.y - Math.min(...ys)) / ySpan;
    const width = ((p.short || p.label).length * 0.0085);
    if (placed.some(([x, y, w]) => Math.abs(ny - y) < 0.055 && nx < x + w + 0.02 && nx + width + 0.02 > x)) continue;
    placed.push([nx, ny, width]);
    chosen.push(p);
  }
  return chosen.map((p) => ({
    x: xLog ? Math.log10(p.x) : p.x, y: p.y, xref: "x", yref: "y", text: p.short || p.label, showarrow: false,
    xanchor: "left", xshift: 9, yshift: 0, font: { family: t.sans, size: 11, color: p.mine ? t.ink : t.muted },
    bgcolor: "rgba(0,0,0,0)",
  }));
}

/** Value over time: one line per product, the in-progress figure as a hollow point at its end. */
export function valueLines(series: Array<{ name: string; colour: string; dash?: string; points: Array<{ x: number; y: number; tip: string }>; open?: { x: number; y: number; tip: string } | null }>, opts: { height?: number } = {}) {
  const t = theme();
  const surface = token("--background");
  const traces: any[] = [];
  for (const s of series) {
    const colour = token(s.colour);
    traces.push({ type: "scatter", mode: "lines+markers", name: s.name, x: s.points.map((p) => new Date(p.x)), y: s.points.map((p) => p.y),
      hovertext: s.points.map((p) => p.tip), hovertemplate: "%{hovertext}<extra></extra>",
      line: { color: colour, width: 2, dash: s.dash ?? "solid" }, marker: { color: colour, size: 7, line: { color: surface, width: 1.5 } } });
    if (s.open) {
      const last = s.points.at(-1);
      traces.push({ type: "scatter", mode: "lines+markers", x: [last ? new Date(last.x) : new Date(s.open.x), new Date(s.open.x)], y: [last?.y ?? s.open.y, s.open.y],
        hovertext: ["", s.open.tip], hovertemplate: "%{hovertext}<extra></extra>",
        line: { color: alpha(colour, 0.7), width: 2, dash: "dot" }, marker: { color: [colour, surface], size: [0, 9], line: { color: colour, width: 2 } } });
    }
  }
  const layout = {
    ...t.layout, height: opts.height ?? 300, hovermode: "closest",
    xaxis: { ...t.axis, type: "date", tickformat: "%b %-d" },
    yaxis: { ...t.axis, tickprefix: "$", rangemode: "tozero", separatethousands: true },
  };
  return { traces, layout };
}

export function hoverHTML(title: string, rows: Array<[string, string]>, note?: string) {
  const esc = (s: string) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]!));
  return `<b>${esc(title)}</b><br>` + rows.map(([k, v]) => `${esc(k)}  <b>${esc(v)}</b>`).join("<br>") + (note ? `<br><span style="font-size:11px">${esc(note)}</span>` : "");
}

export { fmtScore, integer };
