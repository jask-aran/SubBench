import { createEffect, createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Inset, Legend, FamilyChips, Skeleton, Section } from "../app/bits";
import { hoverHTML } from "../app/charts";
import { EChart } from "../app/echart";
import { option2d, option3d, type Row } from "../app/modelchart";
import { RESOURCE_FORMAT } from "../app/points";
import { loadFrontier, loadReports, personal } from "../app/data";
import { Segmented, Input, Swatch, Slider } from "../conduit/ui/controls";
import { DashRow, DashEmpty } from "../conduit/ui/dashboard";
import { Select } from "../conduit/menus";
// @ts-ignore
import { price, duration, integer, score as fmtScore } from "../lib/format.js";
// @ts-ignore
import { points as metricPoints, availableResources, familyColor, familyGroup, RESOURCES, tasksPerEntitlement, baseKey, AXIS_RESOURCES, axisKind, higherIsBetter, axisIsLog, axisValue, scoreLookup, paretoN, attainmentGridBy, bestWithinBy } from "../lib/frontier.js";

/*
 * Models: every public benchmark in one chart. Pick a benchmark, then any two axes (its score, its cost, time or
 * tokens, or another benchmark's score); a third axis turns the chart 3D. The best trade-offs are the models nothing
 * beats on every chosen axis at once. With two "less is better" axes and a score, the 3D chart also carries the
 * attainment surface: the best score within both ceilings.
 */

type Mode = "model" | "agent";
const params = new URLSearchParams(location.search);
const toLog = (pos: number, [lo, hi]: [number, number]) => 10 ** (Math.log10(lo) + (Math.log10(hi) - Math.log10(lo)) * pos / 1000);
const tall = () => Math.max(480, innerHeight - 220);
const NONE = "";

function Models() {
  const [index] = createResource(loadFrontier);
  const [reports] = createResource(loadReports);
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const weekly = () => me()?.products[0]?.headline.value ?? null;

  const [mode, setMode] = createSignal<Mode>(params.get("mode") === "agent" ? "agent" : "model");
  const [lens, setLens] = createSignal(params.get("lens") || "aa-intelligence");
  const [xAxis, setX] = createSignal(params.get("x") || "res:cost");
  const [yAxis, setY] = createSignal(params.get("y") || "score");
  const [zAxis, setZ] = createSignal(params.get("z") || NONE);
  const [log, setLog] = createSignal(true);
  const [tiers, setTiers] = createSignal(true);
  const [labels, setLabels] = createSignal(true);
  const [bestOnly, setBestOnly] = createSignal(false);
  // Three axes draw flat by default: x and y as chosen, the third as colour. Orbit is the true 3D view, opt-in.
  const [orbit, setOrbit] = createSignal(params.get("orbit") !== "0");
  const [surface, setSurface] = createSignal(false);
  const [rail, setRail] = createSignal(true);
  const [camera, setCamera] = createSignal("iso");
  const [search, setSearch] = createSignal("");
  const [hidden, setHidden] = createSignal<Set<string>>(new Set());
  const [pinned, setPinned] = createSignal<Set<string>>(new Set());
  const [selected, setSelected] = createSignal<string | null>(null);
  const [target, setTarget] = createSignal("90");
  const [posA, setPosA] = createSignal(620);
  const [posB, setPosB] = createSignal(620);

  const metrics = createMemo(() => (index() ? [...index()!.metrics.values()].filter((m: any) => m.entity_type === mode()) : []));
  const meta = (id = lens()) => index()?.metrics.get(id) as any;
  createEffect(() => {
    if (index() && !metrics().some((m: any) => m.id === lens())) setLens(mode() === "model" ? "aa-intelligence" : "agent-index");
  });

  // ---- axes
  const base = createMemo(() => (index() ? metricPoints(index()!, lens()) : []));
  const axisOptions = createMemo(() => {
    const own = availableResources(base()) as string[];
    return [
      { value: "score", label: `${meta()?.label ?? "Score"} score` },
      ...AXIS_RESOURCES.filter((k: string) => own.includes(k)).map((k: string) => ({ value: `res:${k}`, label: RESOURCES[k].label })),
      ...metrics().filter((m: any) => m.id !== lens()).map((m: any) => ({ value: `lens:${m.id}`, label: m.label })),
    ];
  });
  // A lens without the chosen resource falls back to sensible axes rather than an empty chart.
  createEffect(() => {
    const valid = new Set(axisOptions().map((o) => o.value));
    if (!index() || !valid.size) return;
    const firstOther = axisOptions().find((o) => o.value !== "score")?.value ?? "score";
    if (!valid.has(xAxis())) setX(valid.has("res:cost") ? "res:cost" : firstOther);
    if (!valid.has(yAxis())) setY("score");
    if (zAxis() && !valid.has(zAxis())) setZ(NONE);
  });
  createEffect(() => {
    const q = new URLSearchParams({ ...(mode() === "agent" ? { mode: "agent" } : {}), lens: lens(), x: xAxis(), y: yAxis(), ...(zAxis() ? { z: zAxis() } : {}), ...(zAxis() && !orbit() ? { orbit: "0" } : {}) });
    history.replaceState(null, "", `${location.pathname}?${q}`);
  });

  const axes = () => (zAxis() ? [xAxis(), yAxis(), zAxis()] : [xAxis(), yAxis()]);
  const is3d = () => axes().length === 3;
  const lookups = createMemo(() => {
    const out = new Map();
    for (const a of axes()) if (axisKind(a) === "lens" && index()) out.set(a.slice(5), scoreLookup(index()!, a.slice(5)));
    return out;
  });
  const title = (a: string) => a === "score" ? meta()?.label ?? "Score" : axisKind(a) === "resource" ? RESOURCES[a.slice(4)]?.label ?? a : meta(a.slice(5))?.label ?? a;
  const unit = (a: string) => axisKind(a) === "resource" ? RESOURCES[a.slice(4)]?.unit : undefined;
  const isPercent = (a: string) => (a === "score" ? meta() : axisKind(a) === "lens" ? meta(a.slice(5)) : null)?.format === "percent";
  const fmtAxis = (a: string, v: number) => axisKind(a) === "resource" ? (RESOURCE_FORMAT[unit(a)] ?? String)(v)
    : fmtScore(v, (a === "score" ? meta() : meta(a.slice(5)))?.format);
  const logAxis = (a: string) => log() && axisIsLog(a);

  // ---- the points, with a value on every chosen axis
  const key = (p: any) => p.entity.id + "|" + (p.observation.variant || "");
  const label = (p: any) => p.entity.label + (p.observation.variant && !p.entity.label.includes(p.observation.variant) ? ` (${p.observation.variant})` : "");
  const needle = () => search().trim().toLowerCase();
  const items = createMemo(() => base()
    .filter((p: any) => !hidden().has(familyGroup(p.entity.family))
      && (!needle() || `${p.entity.label} ${p.entity.id} ${p.entity.family} ${p.entity.agent ?? ""}`.toLowerCase().includes(needle())))
    .map((p: any) => ({ p, v: axes().map((a) => axisValue(a, p, lookups())) as number[] }))
    .filter((row: any) => row.v.every((value: number | null, k: number) => value !== null && (!axisIsLog(axes()[k]) || value > 0))));
  const best = createMemo(() => new Set(paretoN(items(), axes().map((_, k) => (row: any) => row.v[k]), axes().map(higherIsBetter)).map((row: any) => key(row.p))));
  const hover = (row: any) => {
    const o = row.p.observation;
    const rows: Array<[string, string]> = axes().map((a, k) => [title(a), fmtAxis(a, row.v[k])]);
    if (!axes().includes("res:cost") && Number.isFinite(o.resources?.cost)) rows.push(["Cost / task", price(o.resources.cost)]);
    const per = tasksPerEntitlement(weekly(), o.resources?.cost);
    if (per) rows.push(["Tasks / week", integer(per)]);
    return hoverHTML(label(row.p), rows, [row.p.entity.family, best().has(key(row.p)) ? "best trade-off" : null, me()?.mine.has(row.p.entity.id) ? "yours" : null].filter(Boolean).join(" · "));
  };

  // ---- surface: two "less is better" resources and one score
  const surfaceAxes = createMemo(() => {
    if (!is3d()) return null;
    const lower = axes().map((a, k) => ({ a, k })).filter(({ a }) => !higherIsBetter(a) && axisIsLog(a));
    const upper = axes().map((a, k) => ({ a, k })).filter(({ a }) => higherIsBetter(a));
    return lower.length === 2 && upper.length === 1 ? { a: lower[0], b: lower[1], s: upper[0] } : null;
  });
  const span = (k: number): [number, number] => {
    const vs = items().map((r: any) => r.v[k]);
    return vs.length ? [Math.min(...vs), Math.max(...vs)] : [1, 10];
  };
  const caps = createMemo(() => surfaceAxes() ? { a: toLog(posA(), span(surfaceAxes()!.a.k)), b: toLog(posB(), span(surfaceAxes()!.b.k)) } : null);
  const probe = createMemo(() => {
    const s = surfaceAxes(), c = caps();
    if (!s || !c) return null;
    return bestWithinBy(items(), (r: any) => r.v[s.a.k], (r: any) => r.v[s.b.k], (r: any) => r.v[s.s.k], c.a, c.b);
  });

  // The surface grid depends on the points and axes only, so moving a budget slider never recomputes it.
  const grid = createMemo(() => {
    const s = surfaceAxes();
    if (!s || !surface() || !orbit()) return null;
    const g = attainmentGridBy(items(), (r: any) => r.v[s.a.k], (r: any) => r.v[s.b.k], (r: any) => r.v[s.s.k], { steps: 28 });
    return g && {
      a: Array.from({ length: g.steps + 1 }, (_, i) => g.aAt(i)), b: Array.from({ length: g.steps + 1 }, (_, j) => g.bAt(j)),
      z: Array.from({ length: g.steps + 1 }, (_, j) => Array.from({ length: g.steps + 1 }, (_, i) => g.cells[i][j]?.v[s.s.k] ?? null)),
    };
  });

  const scoreK = () => Math.max(0, axes().findIndex((a) => higherIsBetter(a)));
  // ---- the one chart: one engine for 2D and 3D, so changing an axis or the view redraws in place.
  const flat = () => !is3d() || !orbit();
  const axisMeta = (a: string) => ({ title: title(a), log: logAxis(a), unit: unit(a), percent: isPercent(a), higherBetter: higherIsBetter(a) });
  const rows = createMemo<Row[]>(() => {
    const sk = scoreK();
    const named = new Set([...items()].filter((r: any) => best().has(key(r.p))).sort((a: any, b: any) => b.v[sk] - a.v[sk]).slice(0, 14).map((r: any) => key(r.p)));
    return items().map((row: any) => ({
      key: key(row.p), label: label(row.p), short: row.p.entity.label, colour: familyColor(row.p.entity.family),
      group: baseKey(row.p.entity, row.p.observation), hover: hover(row), v: row.v, best: best().has(key(row.p)),
      mine: !!me()?.mine.has(row.p.entity.id), pinned: pinned().has(row.p.entity.id), dim: bestOnly() && !best().has(key(row.p)),
      named: named.has(key(row.p)) || !!me()?.mine.has(row.p.entity.id),
    }));
  });
  const chartOption = (size: { width: number; height: number }) => {
    const r = rows();
    if (flat()) {
      const [x, y, z] = axes();
      return option2d({
        rows: r, axes: [axisMeta(x), axisMeta(y)], colourAxis: z ? axisMeta(z) : null, size, labels: labels(),
        // The staircase reads "nothing above and to the left" only for a cost-like x and a score y, in 2D.
        step: !z && !higherIsBetter(x) && higherIsBetter(y), tiers: !z && tiers() && mode() === "model",
      });
    }
    // 3D: with a surface, the score goes up and the two ceilings lie on the floor.
    const s = surfaceAxes();
    const order = s ? [s.a.k, s.b.k, s.s.k] : [0, 1, 2];
    const c = caps(), pr = probe();
    return option3d({
      rows: r.map((row) => ({ ...row, v: order.map((k) => row.v[k]) })), axes: order.map((k) => axisMeta(axes()[k])) as any,
      labels: labels(), selected: selected(), view: camera(), surface: grid(),
      probe: s && c ? { a: c.a, b: c.b, z: pr ? pr.v[s.s.k] : null, a0: span(s.a.k)[0], b0: span(s.b.k)[0] } : null,
    });
  };

  // ---- side panel
  const bestList = createMemo(() => items().filter((r: any) => best().has(key(r.p))).sort((a: any, b: any) => b.v[scoreK()] - a.v[scoreK()]));
  // Cheapest at target: one score axis and one "less is better" axis, in 2D.
  const solver = createMemo(() => {
    if (is3d()) return null;
    const s = axes().findIndex((a) => higherIsBetter(a)), r = axes().findIndex((a) => !higherIsBetter(a));
    if (s < 0 || r < 0) return null;
    const top = Math.max(...items().map((row: any) => row.v[s]));
    const pick = items().filter((row: any) => row.v[s] >= top * Number(target()) / 100).sort((a: any, b: any) => a.v[r] - b.v[r])[0];
    return pick ? { row: pick, s, r } : null;
  });

  const families = createMemo(() => {
    const counts = new Map<string, number>();
    for (const p of base()) counts.set(familyGroup(p.entity.family), (counts.get(familyGroup(p.entity.family)) || 0) + 1);
    return [...counts.entries()].sort((a, b) => Number(a[0] === "Other") - Number(b[0] === "Other") || b[1] - a[1])
      .map(([name, count]) => ({ name, count, colour: name === "Other" ? "var(--series-other)" : familyColor(name) }));
  });
  const toggle = (name: string) => { const next = new Set<string>(hidden()); next.has(name) ? next.delete(name) : next.add(name); setHidden(next); };
  const solo = (name: string) => setHidden(new Set<string>(families().map((f) => f.name).filter((n) => n !== name)));
  const pin = (id: string) => { const next = new Set<string>(pinned()); next.has(id) ? next.delete(id) : next.add(id); setPinned(next); };
  const onClick = (event: any) => {
    const id = event?.data?.id ?? event?.data?.name ?? event?.name;
    if (typeof id !== "string") return;
    if (is3d()) setSelected(id === selected() ? null : id);
    pin(id.split("|")[0]);
  };
  const compare = createMemo(() => items().filter((r: any) => pinned().has(r.p.entity.id)));
  const axisSelect = (labelText: string, value: () => string, set: (v: string) => void, optional = false) =>
    <Select aria-label={labelText} search="Find an axis" bare={false} value={value()} onChange={(e) => { set(e.currentTarget.value); setSelected(null); }}>
      <Show when={optional}><option value={NONE}>{labelText}: none</option></Show>
      <For each={axisOptions()}>{(o) => <option value={o.value}>{labelText}: {o.label}</option>}</For>
    </Select>;

  return <Site page="models" title="Models" wide>
    <div class="sb-viewbar">
      <Select aria-label="Benchmark" search="Find a benchmark" bare={false} value={lens()} onChange={(e) => { setLens(e.currentTarget.value); setSelected(null); }}>
        <For each={metrics()}>{(m: any) => <option value={m.id}>{m.label} · {m.count}</option>}</For>
      </Select>
      {axisSelect("X", xAxis, setX)}
      {axisSelect("Y", yAxis, setY)}
      {axisSelect("Z", zAxis, setZ, true)}
      <Segmented label="Entities" size="sm" value={mode()} onChange={(v) => { setMode(v); setHidden(new Set<string>()); setPinned(new Set<string>()); }}
        options={[{ value: "model", label: "Models" }, { value: "agent", label: "Agents" }]} />
      <span class="grow" />
      <button type="button" class="sb-chip" aria-pressed={log()} onClick={() => setLog(!log())}>Log</button>
      <Show when={is3d()}>
        <Segmented label="3D" size="sm" value={orbit() ? "orbit" : "flat"} onChange={(v) => setOrbit(v === "orbit")}
          options={[{ value: "orbit", label: "3D" }, { value: "flat", label: "Colour" }]} />
      </Show>
      <Show when={!is3d() && mode() === "model"}><button type="button" class="sb-chip" aria-pressed={tiers()} onClick={() => setTiers(!tiers())}>Tiers</button></Show>
      <Show when={surfaceAxes() && orbit()}><button type="button" class="sb-chip" aria-pressed={surface()} onClick={() => setSurface(!surface())}>Surface</button></Show>
      <button type="button" class="sb-chip" aria-pressed={labels()} onClick={() => setLabels(!labels())}>Labels</button>
      <button type="button" class="sb-chip" aria-pressed={bestOnly()} onClick={() => setBestOnly(!bestOnly())}>Best only</button>
      <button type="button" class="sb-chip" aria-pressed={rail()} onClick={() => setRail(!rail())}>Panel</button>
      <div style={{ width: "11rem" }}><Input type="search" placeholder="Find" aria-label="Find a model" value={search()} onInput={(e) => setSearch(e.currentTarget.value)} /></div>
    </div>

    <div class="sb-canvas" data-rail={rail() ? "on" : "off"}>
      <section class="sb-canvas-main" aria-label="Chart">
        <div class="sb-canvas-head">
          <h2>{`${title(yAxis())} vs ${title(xAxis())}`}{is3d() ? (orbit() ? ` vs ${title(zAxis())}` : ` · colour: ${title(zAxis())}`) : ""}</h2>
          <span>{items().length} shown · {is3d() && orbit() ? "drag to orbit, scroll to zoom" : "scroll to zoom, drag to pan"}</span>
        </div>
        <div class="sb-canvas-chart">
          <Show when={index()} fallback={<Skeleton />}>
            <Show when={items().length >= 2} fallback={<div class="sb-chart-empty" style={{ height: "100%" }}>No models have all of these axes</div>}>
              <EChart option={chartOption} onClick={onClick} label="Models chart" />
            </Show>
          </Show>
        </div>
        <div class="sb-canvas-foot">
          <FamilyChips families={families()} hidden={hidden()} onToggle={toggle} onSolo={solo} />
          <Legend items={[{ label: "Best trade-off", colour: "var(--foreground)" }, { label: "Yours", colour: "var(--foreground)", kind: "ring" }]} />
        </div>
      </section>

      <Show when={rail()}>
        <aside class="sb-rail">
          <Show when={is3d() && orbit()}>
            <Inset title="Camera" tight>
              <Segmented label="Camera" size="sm" value={camera()} onChange={setCamera}
                options={[{ value: "iso", label: "3D" }, { value: "front", label: "Front" }, { value: "side", label: "Side" }, { value: "top", label: "Top" }]} />
            </Inset>
          </Show>
          <Show when={surfaceAxes()}>{(s) =>
            <Inset title="Budget" tight>
              <label class="sb-range"><span class="sb-range-head">{title(s().a.a)} ≤ <b>{fmtAxis(s().a.a, caps()!.a)}</b></span>
                <input type="range" min="0" max="1000" value={posA()} onInput={(e) => setPosA(Number(e.currentTarget.value))} /></label>
              <label class="sb-range"><span class="sb-range-head">{title(s().b.a)} ≤ <b>{fmtAxis(s().b.a, caps()!.b)}</b></span>
                <input type="range" min="0" max="1000" value={posB()} onInput={(e) => setPosB(Number(e.currentTarget.value))} /></label>
              <div style={{ "margin-top": "var(--space-md)" }}>
                <Show when={probe()} fallback={<DashEmpty>None</DashEmpty>}>{(row) =>
                  <div class="sb-callout"><b>{label(row().p)}</b>
                    <dl class="sb-kv" style={{ "margin-top": "var(--space-sm)" }}>
                      <For each={axes()}>{(a, k) => <><dt>{title(a)}</dt><dd>{fmtAxis(a, row().v[k()])}</dd></>}</For>
                    </dl></div>}</Show>
              </div>
            </Inset>}</Show>
          <Show when={solver() !== null || (!is3d() && axes().some((a) => !higherIsBetter(a)))}>
            <Inset title="Cheapest at target" tight>
              <Slider label="Target" value={target()} onChange={setTarget}
                options={[50, 60, 70, 75, 80, 85, 90, 95, 100].map((v) => ({ value: String(v), label: `${v}%` }))} />
              <div style={{ "margin-top": "var(--space-md)" }}>
                <Show when={solver()} fallback={<DashEmpty>None</DashEmpty>}>{(s) => {
                  const per = tasksPerEntitlement(weekly(), s().row.p.observation.resources?.cost);
                  return <div class="sb-callout"><b>{label(s().row.p)}</b>
                    <dl class="sb-kv" style={{ "margin-top": "var(--space-sm)" }}>
                      <For each={axes()}>{(a, k) => <><dt>{title(a)}</dt><dd>{fmtAxis(a, s().row.v[k()])}</dd></>}</For>
                      <Show when={per}><dt>Tasks / week</dt><dd>{integer(per)}</dd></Show>
                    </dl></div>;
                }}</Show>
              </div>
            </Inset>
          </Show>
          <Inset title="Best trade-offs" tight aside={<span class="sb-tag" data-tone="quiet">{bestList().length}</span>}>
            <div class="sb-rows">
              <For each={bestList()} fallback={<DashEmpty>None</DashEmpty>}>{(row: any, i) =>
                <DashRow element="button" current={pinned().has(row.p.entity.id) || selected() === key(row.p)}
                  onClick={() => { if (is3d()) setSelected(selected() === key(row.p) ? null : key(row.p)); pin(row.p.entity.id); }}
                  lead={<Swatch colour={familyColor(row.p.entity.family)} />}
                  primary={<><span class="sb-rank">{String(i() + 1).padStart(2, "0")}</span>{label(row.p)}</>}
                  trailing={axes().map((a, k) => axisKind(a) === "resource" ? fmtAxis(a, row.v[k]) : fmtScore(row.v[k], (a === "score" ? meta() : meta(a.slice(5)))?.format, true)).join(" · ")} />}</For>
            </div>
          </Inset>
        </aside>
      </Show>
    </div>

    <Show when={compare().length}>
      <Section title="Pinned" tools={<button type="button" class="sb-chip" onClick={() => setPinned(new Set<string>())}>Clear</button>}>
        <Inset flush>
          <div class="sb-table-wrap"><table class="sb-table">
            <thead><tr><th>Model</th><th>Family</th><For each={axes()}>{(a) => <th class="num">{title(a)}</th>}</For><th class="num">Cost / task</th><th class="num">Tasks / week</th></tr></thead>
            <tbody><For each={compare()}>{(row: any) => {
              const per = tasksPerEntitlement(weekly(), row.p.observation.resources?.cost);
              return <tr>
                <td><span style={{ display: "inline-flex", gap: "var(--space-sm)", "align-items": "center" }}><Swatch colour={familyColor(row.p.entity.family)} />{label(row.p)}</span></td>
                <td class="data">{row.p.entity.family}</td>
                <For each={axes()}>{(a, k) => <td class="num">{fmtAxis(a, row.v[k()])}</td>}</For>
                <td class="num">{price(row.p.observation.resources?.cost)}</td>
                <td class="num">{per ? integer(per) : "—"}</td>
              </tr>;
            }}</For></tbody>
          </table></div>
        </Inset>
      </Section>
    </Show>
  </Site>;
}

mount(() => <Models />);
