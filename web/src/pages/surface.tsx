import { createEffect, createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Hero, Inset, Section, Legend, FamilyChips, Skeleton } from "../app/bits";
import { Plot, gl3d, theme } from "../app/plot";
import { frontierScatter, hoverHTML, type ScatterPoint } from "../app/charts";
import { loadFrontier, loadReports, personal } from "../app/data";
import { alpha, token, mix, SEQUENTIAL } from "../app/tokens";
import { Segmented, Swatch } from "../conduit/ui/controls";
import { DashRow, DashEmpty } from "../conduit/ui/dashboard";
import { Select } from "../conduit/menus";
// @ts-ignore
import { price, duration, integer, score as fmtScore } from "../lib/format.js";
// @ts-ignore
import { points as metricPoints, surfaceMetrics, pareto3d, attainmentGrid, bestWithin, familyColor, familyGroup, tasksPerEntitlement } from "../lib/frontier.js";

type View = "iso" | "cost" | "time" | "top";
const CAMERAS: Record<View, any> = {
  iso: { eye: { x: -1.55, y: -1.7, z: 0.95 }, up: { x: 0, y: 0, z: 1 }, center: { x: 0, y: 0, z: -0.1 } },
  cost: { eye: { x: 0, y: -2.5, z: 0.02 }, up: { x: 0, y: 0, z: 1 }, center: { x: 0, y: 0, z: 0 } },
  time: { eye: { x: 2.5, y: 0, z: 0.02 }, up: { x: 0, y: 0, z: 1 }, center: { x: 0, y: 0, z: 0 } },
  top: { eye: { x: 0, y: -0.001, z: 2.6 }, up: { x: 0, y: 1, z: 0 }, center: { x: 0, y: 0, z: 0 } },
};
const params = new URLSearchParams(location.search);

// A slider position (0..1000) on a log scale between the data's extremes, and back.
const toLog = (pos: number, [lo, hi]: [number, number]) => 10 ** (Math.log10(lo) + (Math.log10(hi) - Math.log10(lo)) * pos / 1000);

function Surface() {
  const [index] = createResource(loadFrontier);
  const [reports] = createResource(loadReports);
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const weekly = () => me()?.products[0]?.headline.value ?? null;

  const [metric, setMetric] = createSignal(params.get("lens") || "aa-coding");
  const [view, setView] = createSignal<View>("iso");
  const [showSurface, setShowSurface] = createSignal(true);
  const [labels, setLabels] = createSignal(true);
  const [frontierOnly, setFrontierOnly] = createSignal(false);
  const [hidden, setHidden] = createSignal<Set<string>>(new Set());
  const [selected, setSelected] = createSignal<string | null>(null);
  const [costPos, setCostPos] = createSignal(620);
  const [timePos, setTimePos] = createSignal(620);
  let api: { element: HTMLDivElement; plotly: any } | undefined;

  const options = createMemo(() => (index() ? surfaceMetrics(index()!) : []));
  createEffect(() => { if (options().length && !options().some((m: any) => m.id === metric())) setMetric(options()[0].id); });
  createEffect(() => history.replaceState(null, "", `${location.pathname}?lens=${metric()}`));
  const meta = () => index()?.metrics.get(metric()) as any;

  const base = createMemo(() => index() ? metricPoints(index()!, metric()).filter(({ observation: o }: any) => o.resources?.cost > 0 && o.resources?.time > 0) : []);
  const visible = createMemo(() => base().filter((p: any) => !hidden().has(familyGroup(p.entity.family))));
  const front = createMemo(() => pareto3d(visible()));
  const frontKeys = createMemo(() => new Set(front().map((p: any) => p.entity.id + "|" + (p.observation.variant || ""))));
  const ranges = createMemo(() => {
    const cs = base().map((p: any) => p.observation.resources.cost), ts = base().map((p: any) => p.observation.resources.time);
    return { cost: [Math.min(...cs), Math.max(...cs)] as [number, number], time: [Math.min(...ts), Math.max(...ts)] as [number, number] };
  });
  const grid = createMemo(() => visible().length ? attainmentGrid(visible(), { steps: 44, costRange: ranges().cost, timeRange: ranges().time }) : null);
  const caps = createMemo(() => ({ cost: toLog(costPos(), ranges().cost), time: toLog(timePos(), ranges().time) }));
  const probe = createMemo(() => bestWithin(visible(), caps().cost, caps().time));
  const key = (p: any) => p.entity.id + "|" + (p.observation.variant || "");
  const label = (p: any) => p.entity.label + (p.observation.variant && !p.entity.label.includes(p.observation.variant) ? ` (${p.observation.variant})` : "");

  const hover = (p: any) => {
    const o = p.observation;
    const rows: Array<[string, string]> = [[meta()?.label ?? "Score", fmtScore(o.score, meta()?.format)], ["Cost / task", price(o.resources.cost)], ["Time / task", duration(o.resources.time)]];
    const per = tasksPerEntitlement(weekly(), o.resources.cost);
    if (per) rows.push(["Tasks per weekly limit", integer(per)]);
    const mine = me()?.mine.get(p.entity.id);
    return hoverHTML(label(p), rows, [p.entity.family, frontKeys().has(key(p)) ? "on the 3D Pareto set" : null, mine ? "yours" : null].filter(Boolean).join(" · "));
  };

  const data = createMemo(() => {
    const t = theme();
    const traces: any[] = [];
    const g = grid();
    const zFloor = Math.min(...visible().map((p: any) => p.observation.score));
    if (g && showSurface()) {
      // Plotly's surface takes z[row = time][column = cost].
      const z = Array.from({ length: g.steps + 1 }, (_, j) => Array.from({ length: g.steps + 1 }, (_, i) => g.cells[i][j]?.observation.score ?? null));
      const text = Array.from({ length: g.steps + 1 }, (_, j) => Array.from({ length: g.steps + 1 }, (_, i) => {
        const best = g.cells[i][j];
        return best ? hoverHTML(`Budget ${price(g.cAt(i))} · ${duration(g.tAt(j))}`, [["Best within", label(best)], [meta()?.label ?? "Score", fmtScore(best.observation.score, meta()?.format)]]) : "Nothing reaches this budget";
      }));
      const ramp = SEQUENTIAL.map((name, k) => [k / (SEQUENTIAL.length - 1), token(name)]);
      traces.push({
        type: "surface", x: Array.from({ length: g.steps + 1 }, (_, i) => g.cAt(i)), y: Array.from({ length: g.steps + 1 }, (_, j) => g.tAt(j)), z,
        colorscale: ramp, opacity: 0.62, showscale: true, hovertext: text, hovertemplate: "%{hovertext}<extra></extra>",
        contours: { z: { show: true, usecolormap: true, width: 1, project: { z: false } } },
        lighting: { ambient: 0.75, diffuse: 0.5, specular: 0.08, roughness: 0.9 }, connectgaps: false,
        colorbar: { thickness: 10, len: 0.5, x: 1.0, y: 0.5, outlinewidth: 0, tickfont: { family: t.data, size: 10, color: t.muted },
          title: { text: "Best score<br>in budget", font: { family: t.sans, size: 11, color: t.muted } }, tickformat: meta()?.format === "percent" ? ".0%" : "" },
      });
    }
    // Direct labels on the best eight of the 3D set only; every other point keeps its hover and its row in the list.
    const top = new Set([...front()].sort((a: any, b: any) => b.observation.score - a.observation.score).slice(0, 8).map(key));
    const groups = [
      { pts: visible().filter((p: any) => !frontKeys().has(key(p))), size: 4.5, opacity: frontierOnly() ? 0.12 : 0.7, text: false },
      { pts: visible().filter((p: any) => frontKeys().has(key(p)) && !top.has(key(p))), size: 7.5, opacity: 1, text: false },
      { pts: visible().filter((p: any) => top.has(key(p))), size: 7.5, opacity: 1, text: labels() },
    ];
    for (const group of groups) {
      if (!group.pts.length) continue;
      traces.push({
        type: "scatter3d", mode: group.text ? "markers+text" : "markers",
        x: group.pts.map((p: any) => p.observation.resources.cost), y: group.pts.map((p: any) => p.observation.resources.time), z: group.pts.map((p: any) => p.observation.score),
        customdata: group.pts.map(key), hovertext: group.pts.map(hover), hovertemplate: "%{hovertext}<extra></extra>",
        text: group.pts.map((p: any) => p.entity.label), textposition: "top center", textfont: { family: t.sans, size: 10, color: t.muted },
        marker: { size: group.size, opacity: group.opacity, color: group.pts.map((p: any) => token(familyColor(p.entity.family))), line: { color: group.size > 5 ? t.ink : token("--background"), width: group.size > 5 ? 1.5 : 0.5 } },
      });
    }
    const mine = visible().filter((p: any) => me()?.mine.has(p.entity.id));
    if (mine.length) traces.push({ type: "scatter3d", mode: "markers", hoverinfo: "skip",
      x: mine.map((p: any) => p.observation.resources.cost), y: mine.map((p: any) => p.observation.resources.time), z: mine.map((p: any) => p.observation.score),
      marker: { symbol: "circle-open", size: 13, color: t.ink, line: { width: 2 } } });

    // The selected point drops a line to the floor, so its cost and time can be read.
    const chosen = visible().find((p: any) => key(p) === selected());
    if (chosen) {
      const o = chosen.observation;
      traces.push({ type: "scatter3d", mode: "lines", hoverinfo: "skip", x: [o.resources.cost, o.resources.cost], y: [o.resources.time, o.resources.time], z: [o.score, zFloor],
        line: { color: t.ink, width: 3, dash: "dash" } });
    }
    // The budget probe: the affordable rectangle on the floor and the best score inside it.
    const c = caps(), best = probe();
    const [c0] = ranges().cost, [t0] = ranges().time;
    const accent = token("--foreground");
    traces.push({ type: "scatter3d", mode: "lines", hoverinfo: "skip", x: [c0, c.cost, c.cost, c0, c0], y: [t0, t0, c.time, c.time, t0], z: Array(5).fill(zFloor),
      line: { color: alpha(accent, 0.7), width: 4 } });
    if (best) traces.push({ type: "scatter3d", mode: "lines+markers", hoverinfo: "skip", x: [c.cost, c.cost], y: [c.time, c.time], z: [zFloor, best.observation.score],
      line: { color: accent, width: 5 }, marker: { size: [3, 6], color: accent } });
    return traces;
  });

  const layout = createMemo(() => {
    const t = theme();
    const card = token("--card");
    // Log axes get their ticks named in the data's own units ($0.05, 2m), at round values inside the range.
    const logTicks = (lo: number, hi: number, candidates: number[], fmt: (v: number) => string) => {
      const vals = candidates.filter((v) => v >= lo * 0.9 && v <= hi * 1.1);
      return { tickvals: vals, ticktext: vals.map(fmt) };
    };
    const axis = (title: string, type: string, extra: Record<string, unknown> = {}) => ({
      title: { text: title, font: { family: t.sans, size: 12, color: t.muted } }, type,
      tickfont: { family: t.data, size: 10, color: t.muted }, gridcolor: mix(card, t.ink, 0.12), zerolinecolor: mix(card, t.ink, 0.12),
      linecolor: mix(card, t.ink, 0.25), showbackground: true, backgroundcolor: mix(card, t.ink, 0.03), showspikes: false, ...extra,
    });
    const r = ranges();
    return {
      ...t.layout, margin: { l: 0, r: 0, t: 0, b: 0 }, uirevision: "keep", hovermode: "closest",
      scene: {
        uirevision: "keep", camera: CAMERAS.iso, aspectmode: "manual", aspectratio: { x: 1.15, y: 1.15, z: 0.8 }, dragmode: "orbit",
        xaxis: axis("Cost per task", "log", logTicks(r.cost[0], r.cost[1], [0.001, 0.003, 0.01, 0.03, 0.1, 0.3, 1, 3, 10, 30], price)),
        yaxis: axis("Time per task", "log", logTicks(r.time[0], r.time[1], [1, 3, 10, 30, 60, 180, 600, 1800, 3600, 10800], duration)),
        zaxis: axis(meta()?.label ?? "Score", "linear", { tickformat: meta()?.format === "percent" ? ".0%" : "" }),
      },
    };
  });

  createEffect(() => { const v = view(); api?.plotly.relayout(api.element, { "scene.camera": CAMERAS[v] }); });

  // The three projections, the 3D set outlined in each.
  const projection = (x: "cost" | "time", y: "score" | "time") => createMemo(() => {
    const pts: ScatterPoint[] = visible().map((p: any) => ({
      id: key(p), x: p.observation.resources[x], y: y === "score" ? p.observation.score : p.observation.resources.time,
      colour: familyColor(p.entity.family), label: label(p), short: p.entity.label, hover: hover(p), frontier: frontKeys().has(key(p)),
      mine: me()?.mine.has(p.entity.id), dim: frontierOnly() && !frontKeys().has(key(p)),
    }));
    return frontierScatter(pts, { xLog: true, xTitle: x === "cost" ? "Cost per task" : "Time per task (s)", yTitle: y === "score" ? meta()?.label ?? "Score" : "Time per task (s)",
      yFormat: y === "score" && meta()?.format === "percent" ? ".0%" : "", xFormat: x === "cost" ? "$.2~g" : undefined, height: 300, labels: false, step: false, yLog: y === "time" });
  });
  const projCost = projection("cost", "score");
  const projTime = projection("time", "score");
  const projPlane = projection("cost", "time");

  const families = createMemo(() => {
    const counts = new Map<string, number>();
    for (const p of base()) counts.set(familyGroup(p.entity.family), (counts.get(familyGroup(p.entity.family)) || 0) + 1);
    return [...counts.entries()].sort((a, b) => Number(a[0] === "Other") - Number(b[0] === "Other") || b[1] - a[1])
      .map(([name, count]) => ({ name, count, colour: name === "Other" ? "var(--series-other)" : familyColor(name) }));
  });
  const toggle = (name: string) => { const next = new Set<string>(hidden()); next.has(name) ? next.delete(name) : next.add(name); setHidden(next); };
  const solo = (name: string) => setHidden(new Set<string>(families().map((f) => f.name).filter((n) => n !== name)));

  return <Site page="surface" title="3D Pareto surface">
    <Hero small kicker="3D Pareto surface" title={<>Cost, time and score, <em>at once.</em></>}
      lede="A two-axis chart hides a trade-off: a model can be cheap and slow, or fast and expensive. The surface is the best score reachable within a cost and a time budget; the outlined points are the 3D Pareto set, which nothing beats on all three at once. Drag to orbit, scroll to zoom, click a point to drop it to the floor." />

    <div class="sb-section-tools" style={{ "margin-bottom": "var(--space-lg)" }}>
      <Select aria-label="Capability lens" bare={false} value={metric()} onChange={(e) => { setMetric(e.currentTarget.value); setSelected(null); }}>
        <For each={options()}>{(m: any) => <option value={m.id}>{m.label}</option>}</For>
      </Select>
      <Segmented label="Camera" size="sm" value={view()} onChange={setView}
        options={[{ value: "iso", label: "Perspective" }, { value: "cost", label: "Cost × score" }, { value: "time", label: "Time × score" }, { value: "top", label: "Cost × time" }]} />
      <button type="button" class="sb-chip" aria-pressed={showSurface()} onClick={() => setShowSurface(!showSurface())}>Surface</button>
      <button type="button" class="sb-chip" aria-pressed={labels()} onClick={() => setLabels(!labels())}>Labels</button>
      <button type="button" class="sb-chip" aria-pressed={frontierOnly()} onClick={() => setFrontierOnly(!frontierOnly())}>Frontier only</button>
    </div>

    <div class="sb-grid main-aside">
      <div>
        <div class="sb-stage">
          <Show when={index()} fallback={<Skeleton height="100%" />}>
            <Plot plotly={gl3d} data={data} layout={layout} config={{ scrollZoom: true }} label="Three-dimensional chart of cost, time and score"
              ref={(handle) => { api = handle; }}
              onClick={(event) => { const id = event?.points?.find((p: any) => typeof p.customdata === "string")?.customdata; setSelected(id && id !== selected() ? id : null); }} />
          </Show>
          <div class="sb-stage-hint">Drag to orbit · scroll to zoom · double-click to reset</div>
        </div>
        <div style={{ "margin-top": "var(--space-md)" }}><FamilyChips families={families()} hidden={hidden()} onToggle={toggle} onSolo={solo} /></div>
        <Legend items={[{ label: "3D Pareto set (outlined, labelled)", colour: "var(--foreground)", kind: "dot" }, { label: "Your models", colour: "var(--foreground)", kind: "ring" },
          { label: "Budget probe", colour: "var(--foreground)" }]} />
      </div>

      <div class="sb-grid">
        <Inset title="Budget probe" note="Set a ceiling on cost and time; the surface gives the best score inside it.">
          <label class="sb-range"><span class="sb-range-head">Cost per task ≤ <b>{price(caps().cost)}</b></span>
            <input type="range" min="0" max="1000" value={costPos()} onInput={(e) => setCostPos(Number(e.currentTarget.value))} /></label>
          <label class="sb-range"><span class="sb-range-head">Time per task ≤ <b>{duration(caps().time)}</b></span>
            <input type="range" min="0" max="1000" value={timePos()} onInput={(e) => setTimePos(Number(e.currentTarget.value))} /></label>
          <div style={{ "margin-top": "var(--space-lg)" }}>
            <Show when={probe()} fallback={<DashEmpty>Nothing visible fits this budget.</DashEmpty>}>{(best) =>
              <div class="sb-callout"><b>{label(best())}</b> is the best within budget: {fmtScore(best().observation.score, meta()?.format)} for {price(best().observation.resources.cost)} and {duration(best().observation.resources.time)} per task.
                <Show when={tasksPerEntitlement(weekly(), best().observation.resources.cost)}>{(per) => <> A weekly limit buys about <b>{integer(per())}</b> of them.</>}</Show></div>}
            </Show>
          </div>
        </Inset>
        <Inset title="3D Pareto set" note="Best score first" aside={<span class="sb-tag" data-tone="quiet">{front().length}</span>}>
          <div class="sb-rows">
            <For each={[...front()].sort((a: any, b: any) => b.observation.score - a.observation.score)} fallback={<DashEmpty>Nothing visible.</DashEmpty>}>{(p: any) =>
              <DashRow element="button" current={selected() === key(p)} onClick={() => setSelected(selected() === key(p) ? null : key(p))}
                lead={<Swatch colour={familyColor(p.entity.family)} />} primary={label(p)}
                trailing={`${fmtScore(p.observation.score, meta()?.format, true)} · ${price(p.observation.resources.cost)} · ${duration(p.observation.resources.time)}`} />}</For>
          </div>
        </Inset>
      </div>
    </div>

    <Section title="The three projections" note="The same points flattened onto each pair of axes, with the 3D Pareto set outlined. A point can be on the 3D set and look dominated in one projection, because it wins on the axis that projection hides.">
      <div class="sb-grid three">
        <For each={[{ title: "Cost × score", chart: projCost }, { title: "Time × score", chart: projTime }, { title: "Cost × time", chart: projPlane }]}>{(panel) =>
          <Inset title={panel.title}>
            <Show when={index()} fallback={<Skeleton height="300px" />}>
              <Plot plotly={gl3d} data={() => panel.chart().traces} layout={() => panel.chart().layout} label={panel.title} />
            </Show>
          </Inset>}</For>
      </div>
    </Section>
  </Site>;
}

mount(() => <Surface />);
