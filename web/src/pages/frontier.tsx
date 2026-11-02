import { createEffect, createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Inset, Legend, Bars, FamilyChips, Skeleton, Section } from "../app/bits";
import { Plot, gl3d } from "../app/plot";
import { frontierScatter, hoverHTML, type ScatterPoint } from "../app/charts";
import { joinedScatter, RESOURCE_FORMAT } from "../app/points";
import { surfaceTraces, surfaceLayout, CAMERAS } from "../app/surface3d";
import { loadFrontier, loadReports, personal } from "../app/data";
import { Segmented, Input, Swatch, Slider } from "../conduit/ui/controls";
import { DashRow, DashEmpty } from "../conduit/ui/dashboard";
import { Select } from "../conduit/menus";
// @ts-ignore
import { price, duration, integer, score as fmtScore } from "../lib/format.js";
// @ts-ignore
import { points as metricPoints, availableResources, familyColor, familyGroup, solve, resourceValue, RESOURCES, tasksPerEntitlement, pareto3d, attainmentGrid, bestWithin } from "../lib/frontier.js";

/*
 * The explorer: one chart the size of the window, and tabs that change what it shows. Score against cost, time or
 * tokens (the 2D Pareto frontier), cost against time, or all three at once (the 3D attainment surface). The panel
 * beside it follows the view: the solver and 2D set, or the budget and 3D set.
 */

type Mode = "model" | "agent";
type View = "cost" | "time" | "tokens" | "plane" | "3d";
const VIEWS: Array<{ value: View; label: string }> = [
  { value: "cost", label: "Cost" }, { value: "time", label: "Time" }, { value: "tokens", label: "Tokens" },
  { value: "plane", label: "Cost × time" }, { value: "3d", label: "3D" },
];
const params = new URLSearchParams(location.search);
const toLog = (pos: number, [lo, hi]: [number, number]) => 10 ** (Math.log10(lo) + (Math.log10(hi) - Math.log10(lo)) * pos / 1000);
const tall = () => Math.max(480, innerHeight - 220);

function Explorer() {
  const [index] = createResource(loadFrontier);
  const [reports] = createResource(loadReports);
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const weekly = () => me()?.products[0]?.headline.value ?? null;

  const [mode, setMode] = createSignal<Mode>(params.get("mode") === "agent" ? "agent" : "model");
  const [metric, setMetric] = createSignal(params.get("lens") || "aa-intelligence");
  const [view, setView] = createSignal<View>((VIEWS.find((v) => v.value === params.get("view"))?.value) ?? "cost");
  const [log, setLog] = createSignal(true);
  const [tiers, setTiers] = createSignal(true);
  const [frontierOnly, setFrontierOnly] = createSignal(false);
  const [labels, setLabels] = createSignal(true);
  const [surface, setSurface] = createSignal(true);
  const [rail, setRail] = createSignal(true);
  const [camera, setCamera] = createSignal("iso");
  const [search, setSearch] = createSignal("");
  const [hidden, setHidden] = createSignal<Set<string>>(new Set());
  const [pinned, setPinned] = createSignal<Set<string>>(new Set());
  const [selected, setSelected] = createSignal<string | null>(null);
  const [target, setTarget] = createSignal("90");
  const [solveBy, setSolveBy] = createSignal<"x" | "cost" | "time">("x");
  const [costPos, setCostPos] = createSignal(620);
  const [timePos, setTimePos] = createSignal(620);
  let api: { element: HTMLDivElement; plotly: any } | undefined;

  const metrics = createMemo(() => (index() ? [...index()!.metrics.values()].filter((m: any) => m.entity_type === mode()) : []));
  createEffect(() => {
    if (index() && !metrics().some((m: any) => m.id === metric())) setMetric(mode() === "model" ? "aa-intelligence" : "agent-index");
  });
  const meta = () => index()?.metrics.get(metric()) as any;
  const resources = createMemo(() => (index() ? availableResources(metricPoints(index()!, metric())) as string[] : []));
  const tokenKey = () => ["output_tokens", "total_tokens", "input_tokens"].find((k) => resources().includes(k)) ?? null;
  const has = (v: View) => v === "cost" ? resources().includes("cost") : v === "time" ? resources().includes("time")
    : v === "tokens" ? !!tokenKey() : resources().includes("cost") && resources().includes("time");
  const is3d = () => view() === "3d" || view() === "plane";
  createEffect(() => {
    const next = new URLSearchParams({ ...(mode() === "agent" ? { mode: "agent" } : {}), lens: metric(), view: view() });
    history.replaceState(null, "", `${location.pathname}?${next}`);
  });
  const ranking = () => index() && !resources().length;

  // ---- 2D views
  const resource = () => view() === "time" ? "time" : view() === "tokens" ? tokenKey() ?? "cost" : "cost";
  const joined = createMemo(() => index() && !ranking() && !is3d() && has(view())
    ? joinedScatter(index()!, metric(), resource(), { mine: me()?.mine, hidden: hidden(), search: search(), pinned: pinned(), weeklyValue: weekly(), frontierOnly: frontierOnly() })
    : null);
  const solver = createMemo(() => {
    const j = joined();
    if (!j) return null;
    const by = solveBy() === "x" ? resource() : solveBy();
    const getValue = (p: any) => by === resource() ? j.getX(p) : resourceValue(p, by, {});
    const winner = solve(j.comparable, { target: Number(target()) / 100, getValue });
    return winner ? { winner, by, value: getValue(winner) } : null;
  });

  // ---- 3D views
  const key = (p: any) => p.entity.id + "|" + (p.observation.variant || "");
  const label = (p: any) => p.entity.label + (p.observation.variant && !p.entity.label.includes(p.observation.variant) ? ` (${p.observation.variant})` : "");
  const base3 = createMemo(() => index() ? metricPoints(index()!, metric()).filter(({ observation: o }: any) => o.resources?.cost > 0 && o.resources?.time > 0) : []);
  const needle = () => search().trim().toLowerCase();
  const visible3 = createMemo(() => base3().filter((p: any) => !hidden().has(familyGroup(p.entity.family))
    && (!needle() || `${p.entity.label} ${p.entity.id} ${p.entity.family}`.toLowerCase().includes(needle()))));
  const front3 = createMemo(() => pareto3d(visible3()));
  const frontKeys = createMemo(() => new Set<string>(front3().map(key)));
  const ranges = createMemo(() => {
    const cs = base3().map((p: any) => p.observation.resources.cost), ts = base3().map((p: any) => p.observation.resources.time);
    return { cost: [Math.min(...cs), Math.max(...cs)] as [number, number], time: [Math.min(...ts), Math.max(...ts)] as [number, number] };
  });
  const caps = createMemo(() => ({ cost: toLog(costPos(), ranges().cost), time: toLog(timePos(), ranges().time) }));
  const probe = createMemo(() => bestWithin(visible3(), caps().cost, caps().time));
  const hover3 = (p: any) => {
    const o = p.observation;
    const rows: Array<[string, string]> = [["Score", fmtScore(o.score, meta()?.format)], ["Cost / task", price(o.resources.cost)], ["Time / task", duration(o.resources.time)]];
    const per = tasksPerEntitlement(weekly(), o.resources.cost);
    if (per) rows.push(["Tasks / week", integer(per)]);
    return hoverHTML(label(p), rows, [p.entity.family, frontKeys().has(key(p)) ? "Pareto" : null, me()?.mine.has(p.entity.id) ? "yours" : null].filter(Boolean).join(" · "));
  };

  // ---- the one chart
  const figure = createMemo(() => {
    if (!index() || ranking() || !has(view())) return null;
    if (view() === "3d") {
      const input = {
        visible: visible3(), front: front3(), frontKeys: frontKeys(), ranges: ranges(), caps: caps(), probe: probe(), selected: selected(),
        grid: visible3().length ? attainmentGrid(visible3(), { steps: 48, costRange: ranges().cost, timeRange: ranges().time }) : null,
        showSurface: surface(), labels: labels(), frontierOnly: frontierOnly(), mine: new Set<string>(me()?.mine.keys() ?? []),
        meta: meta(), hover: hover3, key, label,
      };
      return { traces: surfaceTraces(input), layout: surfaceLayout(input) };
    }
    if (view() === "plane") {
      const pts: ScatterPoint[] = visible3().map((p: any) => ({
        id: key(p), x: p.observation.resources.cost, y: p.observation.resources.time, colour: familyColor(p.entity.family), label: label(p),
        short: p.entity.label, hover: hover3(p), frontier: frontKeys().has(key(p)), mine: me()?.mine.has(p.entity.id), pinned: pinned().has(p.entity.id),
        dim: frontierOnly() && !frontKeys().has(key(p)),
      }));
      return frontierScatter(pts, { xLog: true, yLog: true, xTitle: "Cost / task", yTitle: "Time / task (s)", yFormat: "", xFormat: "$.2~g",
        step: false, labels: false, fill: true, height: tall() });
    }
    const j = joined();
    if (!j) return null;
    const win = solver()?.winner;
    const pts = j.scatter.map((p) => (win && p.id === key(win) ? { ...p, pinned: true } : p));
    const unit = RESOURCES[resource()]?.unit;
    return frontierScatter(pts, {
      xLog: log() && !!RESOURCES[resource()]?.log, xTitle: RESOURCES[resource()]?.label ?? resource(), yTitle: j.metric.label,
      yFormat: j.metric.format === "percent" ? ".0%" : "", xFormat: unit === "currency" ? "$.3~g" : undefined,
      groups: tiers() && mode() === "model", gutter: labels() && innerWidth > 700, fill: true, height: tall(),
    });
  });
  createEffect(() => { const c = camera(); if (view() === "3d") api?.plotly.relayout(api.element, { "scene.camera": CAMERAS[c] }); });

  const families = createMemo(() => {
    if (!index()) return [];
    const counts = new Map<string, number>();
    for (const p of metricPoints(index()!, metric())) counts.set(familyGroup(p.entity.family), (counts.get(familyGroup(p.entity.family)) || 0) + 1);
    return [...counts.entries()].sort((a, b) => Number(a[0] === "Other") - Number(b[0] === "Other") || b[1] - a[1])
      .map(([name, count]) => ({ name, count, colour: name === "Other" ? "var(--series-other)" : familyColor(name) }));
  });
  const toggle = (name: string) => { const next = new Set<string>(hidden()); next.has(name) ? next.delete(name) : next.add(name); setHidden(next); };
  const solo = (name: string) => setHidden(new Set<string>(families().map((f) => f.name).filter((n) => n !== name)));
  const pin = (id: string) => { const next = new Set<string>(pinned()); next.has(id) ? next.delete(id) : next.add(id); setPinned(next); };
  const onClick = (event: any) => {
    const id = event?.points?.find((p: any) => typeof p.customdata === "string")?.customdata;
    if (typeof id !== "string") return;
    if (view() === "3d") setSelected(id === selected() ? null : id);
    pin(id.split("|")[0]);
  };

  const fmtX = (v: number) => (RESOURCE_FORMAT[RESOURCES[resource()]?.unit] ?? String)(v);
  const count = () => is3d() ? visible3().length : joined()?.comparable.length ?? 0;
  const set = () => is3d() ? [...front3()].sort((a: any, b: any) => b.observation.score - a.observation.score) : joined()?.frontier ?? [];
  const rankRows = createMemo(() => {
    if (!index() || !ranking()) return [];
    return metricPoints(index()!, metric()).filter((p: any) => !hidden().has(familyGroup(p.entity.family)))
      .sort((a: any, b: any) => b.observation.score - a.observation.score).slice(0, 40)
      .map((p: any) => ({ key: key(p), label: label(p), colour: familyColor(p.entity.family), value: p.observation.score,
        display: fmtScore(p.observation.score, meta()?.format), tag: me()?.mine.has(p.entity.id) ? "yours" : undefined }));
  });
  const compare = createMemo(() => pinned().size && index()
    ? metricPoints(index()!, metric()).filter((p: any) => pinned().has(p.entity.id)) : []);

  return <Site page="frontier" title="Frontier" wide>
    <div class="sb-viewbar">
      <Segmented label="View" value={view()} onChange={setView}
        options={VIEWS.map((v) => ({ ...v, title: has(v.value) ? undefined : "No data for this lens" }))} />
      <Select aria-label="Lens" search="Find a lens" bare={false} value={metric()} onChange={(e) => { setMetric(e.currentTarget.value); setSelected(null); }}>
        <For each={metrics()}>{(m: any) => <option value={m.id}>{m.label} · {m.count}</option>}</For>
      </Select>
      <Segmented label="Entities" size="sm" value={mode()} onChange={(v) => { setMode(v); setHidden(new Set<string>()); setPinned(new Set<string>()); }}
        options={[{ value: "model", label: "Models" }, { value: "agent", label: "Agents" }]} />
      <span class="grow" />
      <Show when={!is3d()}>
        <button type="button" class="sb-chip" aria-pressed={log()} onClick={() => setLog(!log())}>Log</button>
        <Show when={mode() === "model"}><button type="button" class="sb-chip" aria-pressed={tiers()} onClick={() => setTiers(!tiers())}>Tiers</button></Show>
      </Show>
      <Show when={view() === "3d"}><button type="button" class="sb-chip" aria-pressed={surface()} onClick={() => setSurface(!surface())}>Surface</button></Show>
      <button type="button" class="sb-chip" aria-pressed={labels()} onClick={() => setLabels(!labels())}>Labels</button>
      <button type="button" class="sb-chip" aria-pressed={frontierOnly()} onClick={() => setFrontierOnly(!frontierOnly())}>Frontier only</button>
      <button type="button" class="sb-chip" aria-pressed={rail()} onClick={() => setRail(!rail())}>Panel</button>
      <div style={{ width: "12rem" }}><Input type="search" placeholder="Find" aria-label="Find a model" value={search()} onInput={(e) => setSearch(e.currentTarget.value)} /></div>
    </div>

    <div class="sb-canvas" data-rail={rail() ? "on" : "off"}>
      <section class="sb-canvas-main" aria-label="Chart">
        <div class="sb-canvas-head">
          <h2>{meta()?.label ?? "Loading"}</h2>
          <span>{ranking() ? "ranking" : `${count()} shown`}{view() === "3d" ? " · drag to orbit" : ""}</span>
        </div>
        <div class="sb-canvas-chart">
          <Show when={index()} fallback={<Skeleton />}>
            <Show when={!ranking()} fallback={<div style={{ "overflow-y": "auto", height: "100%" }}><Bars rows={rankRows()} /></div>}>
              <Show when={figure()} fallback={<div class="sb-chart-empty" style={{ height: "100%" }}>No {VIEWS.find((v) => v.value === view())?.label.toLowerCase()} data for this lens</div>}>
                <Plot plotly={gl3d} data={() => figure()!.traces} layout={() => figure()!.layout} config={{ scrollZoom: view() === "3d" }}
                  label={`${meta()?.label} chart`} onClick={onClick} ref={(handle) => { api = handle; }} />
              </Show>
            </Show>
          </Show>
        </div>
        <div class="sb-canvas-foot">
          <FamilyChips families={families()} hidden={hidden()} onToggle={toggle} onSolo={solo} />
          <Legend items={[{ label: "Pareto", colour: "var(--foreground)" }, { label: "Yours", colour: "var(--foreground)", kind: "ring" }]} />
        </div>
      </section>

      <Show when={rail()}>
        <aside class="sb-rail">
          <Show when={view() === "3d"}>
            <Inset title="Camera" tight>
              <Segmented label="Camera" size="sm" value={camera()} onChange={setCamera}
                options={[{ value: "iso", label: "3D" }, { value: "cost", label: "Cost" }, { value: "time", label: "Time" }, { value: "top", label: "Top" }]} />
            </Inset>
          </Show>
          <Show when={is3d()} fallback={
            <Inset title="Cheapest at target" tight>
              <Slider label="Target" value={target()} onChange={setTarget}
                options={[50, 60, 70, 75, 80, 85, 90, 95, 100].map((v) => ({ value: String(v), label: `${v}%` }))} />
              <div style={{ margin: "var(--space-md) 0" }}>
                <Segmented label="Minimise" size="sm" value={solveBy()} onChange={setSolveBy}
                  options={[{ value: "x", label: "Axis" }, { value: "cost", label: "Cost" }, { value: "time", label: "Time" }]} />
              </div>
              <Show when={solver()} fallback={<DashEmpty>None</DashEmpty>}>{(s) => {
                const per = tasksPerEntitlement(weekly(), s().winner.observation.resources?.cost);
                return <div class="sb-callout"><b>{label(s().winner)}</b>
                  <dl class="sb-kv" style={{ "margin-top": "var(--space-sm)" }}>
                    <dt>Score</dt><dd>{fmtScore(s().winner.observation.score, meta()?.format)}</dd>
                    <dt>{RESOURCES[s().by]?.label}</dt><dd>{(RESOURCE_FORMAT[RESOURCES[s().by]?.unit] ?? String)(s().value)}</dd>
                    <Show when={per}><dt>Tasks / week</dt><dd>{integer(per)}</dd></Show>
                  </dl></div>;
              }}</Show>
            </Inset>}>
            <Inset title="Budget" tight>
              <label class="sb-range"><span class="sb-range-head">Cost ≤ <b>{price(caps().cost)}</b></span>
                <input type="range" min="0" max="1000" value={costPos()} onInput={(e) => setCostPos(Number(e.currentTarget.value))} /></label>
              <label class="sb-range"><span class="sb-range-head">Time ≤ <b>{duration(caps().time)}</b></span>
                <input type="range" min="0" max="1000" value={timePos()} onInput={(e) => setTimePos(Number(e.currentTarget.value))} /></label>
              <div style={{ "margin-top": "var(--space-md)" }}>
                <Show when={probe()} fallback={<DashEmpty>None</DashEmpty>}>{(best) => {
                  const per = tasksPerEntitlement(weekly(), best().observation.resources.cost);
                  return <div class="sb-callout"><b>{label(best())}</b>
                    <dl class="sb-kv" style={{ "margin-top": "var(--space-sm)" }}>
                      <dt>Score</dt><dd>{fmtScore(best().observation.score, meta()?.format)}</dd>
                      <dt>Cost / task</dt><dd>{price(best().observation.resources.cost)}</dd>
                      <dt>Time / task</dt><dd>{duration(best().observation.resources.time)}</dd>
                      <Show when={per}><dt>Tasks / week</dt><dd>{integer(per)}</dd></Show>
                    </dl></div>;
                }}</Show>
              </div>
            </Inset>
          </Show>
          <Inset title="Pareto set" tight aside={<span class="sb-tag" data-tone="quiet">{set().length}</span>}>
            <div class="sb-rows">
              <For each={set()} fallback={<DashEmpty>None</DashEmpty>}>{(p: any, i) =>
                <DashRow element="button" current={pinned().has(p.entity.id) || selected() === key(p)}
                  onClick={() => { if (view() === "3d") setSelected(selected() === key(p) ? null : key(p)); pin(p.entity.id); }}
                  lead={<Swatch colour={familyColor(p.entity.family)} />}
                  primary={<><span class="sb-rank">{String(i() + 1).padStart(2, "0")}</span>{label(p)}</>}
                  trailing={is3d() ? `${fmtScore(p.observation.score, meta()?.format, true)} · ${price(p.observation.resources.cost)}`
                    : `${fmtScore(p.observation.score, meta()?.format, true)} · ${fmtX(joined()!.getX(p))}`} />}</For>
            </div>
          </Inset>
        </aside>
      </Show>
    </div>

    <Show when={compare().length}>
      <Section title="Pinned" tools={<button type="button" class="sb-chip" onClick={() => setPinned(new Set<string>())}>Clear</button>}>
        <Inset flush>
          <div class="sb-table-wrap"><table class="sb-table">
            <thead><tr><th>Model</th><th>Family</th><th class="num">Score</th><th class="num">Cost / task</th><th class="num">Time / task</th><th class="num">Output tokens</th><th class="num">Tasks / week</th></tr></thead>
            <tbody><For each={compare()}>{(p: any) => {
              const per = tasksPerEntitlement(weekly(), p.observation.resources?.cost);
              return <tr>
                <td><span style={{ display: "inline-flex", gap: "var(--space-sm)", "align-items": "center" }}><Swatch colour={familyColor(p.entity.family)} />{label(p)}</span></td>
                <td class="data">{p.entity.family}</td>
                <td class="num">{fmtScore(p.observation.score, meta()?.format)}</td>
                <td class="num">{price(p.observation.resources?.cost)}</td>
                <td class="num">{duration(p.observation.resources?.time)}</td>
                <td class="num">{integer(p.observation.resources?.output_tokens)}</td>
                <td class="num">{per ? integer(per) : "—"}</td>
              </tr>;
            }}</For></tbody>
          </table></div>
        </Inset>
      </Section>
    </Show>
  </Site>;
}

mount(() => <Explorer />);
