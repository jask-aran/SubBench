import { createEffect, createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Inset, Skeleton } from "../app/bits";
import { Picker, ToggleMenu, type PickGroup } from "../app/picker";
import { ArrowLeftRightIcon, XIcon, SlidersHorizontalIcon } from "lucide-solid";
import { hoverHTML } from "../app/charts";
import { EChart } from "../app/echart";
import { option2d, option3d, optionPlans, type Row, type PlanSeries } from "../app/modelchart";
// @ts-ignore
import { planPrice, valueMultiple } from "../lib/plans.js";
import { RESOURCE_FORMAT } from "../app/points";
import { loadFrontier, loadReports, personal } from "../app/data";
import { Segmented, Input, Swatch, Slider } from "../conduit/ui/controls";
import { DashRow, DashEmpty } from "../conduit/ui/dashboard";
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
const NONE = "";

function Models() {
  const [index] = createResource(loadFrontier);
  const [reports] = createResource(loadReports);
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const weekly = () => me()?.products[0]?.headline.value ?? null;
  // Every plan SubBench has measured, pooled across the accounts that report it. A plan covers its provider's models:
  // Claude plans Anthropic's, ChatGPT plans OpenAI's.
  type Plan = { name: string; provider: string; value: number; kind: string; accounts: number; monthly: number | null; multiple: number | null };
  const plans = createMemo<Plan[]>(() => (me()?.products ?? [])
    .filter((item: any) => item.card.provider)
    .map((item: any) => ({ name: item.card.name, provider: item.card.provider, value: item.headline.value, kind: item.headline.kind,
      accounts: item.headline.row?.account_count ?? 1,
      monthly: planPrice(item.card.name)?.monthly ?? null, multiple: valueMultiple(item.card.name, item.headline.value) }))
    .sort((a: Plan, b: Plan) => a.provider.localeCompare(b.provider) || a.value - b.value));
  const providerOf = (entity: any) => {
    const text = `${entity.provider ?? ""} ${entity.family ?? ""} ${entity.agent ?? ""}`.toLowerCase();
    return text.includes("anthropic") || text.includes("claude") ? "claude" : text.includes("openai") || text.includes("codex") ? "codex" : null;
  };
  const plansFor = (entity: any) => plans().filter((plan) => plan.provider === providerOf(entity));
  const planNamed = (axis: string) => plans().find((plan) => `sub:${plan.name}` === axis) ?? null;
  const [onPlans, setOnPlans] = createSignal(false);
  // Chart: any axes. Plans: how much of the chosen benchmark's work each measured plan gets done per week.
  const [view, setView] = createSignal<"chart" | "plans">(params.get("view") === "plans" ? "plans" : "chart");
  // Plans per week (what a limit gets done) or per dollar (what the money gets done, comparable across plan sizes).
  const [perDollar, setPerDollar] = createSignal(params.get("basis") === "dollar");
  const provider = (key: string) => (key === "claude" ? "Anthropic" : key === "codex" ? "OpenAI" : key);

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
  const own = createMemo(() => availableResources(base()) as string[]);
  const axisGroups = createMemo<PickGroup[]>(() => {
    const others = new Map<string, any[]>();
    for (const m of metrics()) if (m.id !== lens()) (others.get(m.group) ?? others.set(m.group, []).get(m.group)!).push(m);
    return [
      { title: meta()?.label ?? "This benchmark", items: [
        { value: "score", label: "Score" },
        ...AXIS_RESOURCES.filter((k: string) => own().includes(k)).map((k: string) => ({ value: `res:${k}`, label: RESOURCES[k].label })),
      ] },
      ...(plans().length && own().includes("cost") ? [{ title: "Measured plans · tasks per weekly limit", items: plans().map((plan) => ({
        value: `sub:${plan.name}`, label: `Tasks / week · ${plan.name}`,
        detail: `${provider(plan.provider)} models · $${Math.round(plan.value).toLocaleString()} per week`,
      })) }] : []),
      ...[...others.entries()].map(([group, list]) => ({ title: `${group} score`, items: list.map((m: any) => ({ value: `lens:${m.id}`, label: m.label, meta: String(m.count) })) })),
    ];
  });
  const axisOptions = createMemo(() => axisGroups().flatMap((g) => g.items));
  const lensGroups = createMemo<PickGroup[]>(() => {
    const groups = new Map<string, any[]>();
    for (const m of metrics()) (groups.get(m.group) ?? groups.set(m.group, []).get(m.group)!).push(m);
    return [...groups.entries()].map(([title, list]) => ({ title, items: list.map((m: any) => ({ value: m.id, label: m.label, meta: String(m.count) })) }));
  });
  // A lens without the chosen resource falls back to sensible axes rather than an empty chart.
  createEffect(() => {
    const valid = new Set(axisOptions().map((o) => o.value));
    if (!index() || !valid.size) return;
    // The plan axis exists once the subscription reports arrive; a link that asks for it waits for them.
    if (reports.loading) for (const a of axes()) if (a.startsWith("sub:")) valid.add(a);
    const firstOther = axisOptions().find((o) => o.value !== "score")?.value ?? "score";
    if (!valid.has(xAxis())) setX(valid.has("res:cost") ? "res:cost" : firstOther);
    if (!valid.has(yAxis())) setY("score");
    if (zAxis() && !valid.has(zAxis())) setZ(NONE);
  });
  createEffect(() => {
    const q = new URLSearchParams({ ...(mode() === "agent" ? { mode: "agent" } : {}), ...(view() === "plans" ? { view: "plans", ...(perDollar() ? { basis: "dollar" } : {}) } : {}), lens: lens(), x: xAxis(), y: yAxis(), ...(zAxis() ? { z: zAxis() } : {}), ...(zAxis() && !orbit() ? { orbit: "0" } : {}) });
    history.replaceState(null, "", `${location.pathname}?${q}`);
  });

  const axes = () => (zAxis() ? [xAxis(), yAxis(), zAxis()] : [xAxis(), yAxis()]);
  const is3d = () => axes().length === 3;
  const lookups = createMemo(() => {
    const out = new Map();
    for (const a of axes()) if (axisKind(a) === "lens" && index()) out.set(a.slice(5), scoreLookup(index()!, a.slice(5)));
    return out;
  });
  const isPlanAxis = (a: string) => a.startsWith("sub:");
  const title = (a: string) => isPlanAxis(a) ? `Tasks / week · ${a.slice(4)}` : a === "score" ? meta()?.label ?? "Score" : axisKind(a) === "resource" ? RESOURCES[a.slice(4)]?.label ?? a : meta(a.slice(5))?.label ?? a;
  const unit = (a: string) => isPlanAxis(a) ? "integer" : axisKind(a) === "resource" ? RESOURCES[a.slice(4)]?.unit : undefined;
  const isPercent = (a: string) => (a === "score" ? meta() : axisKind(a) === "lens" ? meta(a.slice(5)) : null)?.format === "percent";
  const fmtAxis = (a: string, v: number) => isPlanAxis(a) ? integer(v) : axisKind(a) === "resource" ? (RESOURCE_FORMAT[unit(a)] ?? String)(v)
    : fmtScore(v, (a === "score" ? meta() : meta(a.slice(5)))?.format);
  const logAxis = (a: string) => log() && (axisIsLog(a) || isPlanAxis(a));
  // A plan axis: the weekly value of the plan covering the model's provider, over the benchmark's cost per task.
  const valueOn = (a: string, p: any) => {
    if (!isPlanAxis(a)) return axisValue(a, p, lookups());
    // A plan counts only the models it covers; a model from another provider has no value on its axis.
    const plan = planNamed(a), cost = p.observation.resources?.cost;
    return plan && plan.provider === providerOf(p.entity) && cost > 0 ? plan.value / cost : null;
  };

  // ---- the points, with a value on every chosen axis
  const key = (p: any) => p.entity.id + "|" + (p.observation.variant || "");
  const label = (p: any) => p.entity.label + (p.observation.variant && !p.entity.label.includes(p.observation.variant) ? ` (${p.observation.variant})` : "");
  const needle = () => search().trim().toLowerCase();
  const items = createMemo(() => base()
    .filter((p: any) => !hidden().has(familyGroup(p.entity.family)) && (!onPlans() || plansFor(p.entity).length > 0)
      && (!needle() || `${p.entity.label} ${p.entity.id} ${p.entity.family} ${p.entity.agent ?? ""}`.toLowerCase().includes(needle())))
    .map((p: any) => ({ p, v: axes().map((a) => valueOn(a, p)) as number[] }))
    .filter((row: any) => row.v.every((value: number | null, k: number) => value !== null && (!logAxis(axes()[k]) || value > 0))));
  const best = createMemo(() => new Set(paretoN(items(), axes().map((_, k) => (row: any) => row.v[k]), axes().map(higherIsBetter)).map((row: any) => key(row.p))));
  const hover = (row: any) => {
    const o = row.p.observation;
    const rows: Array<[string, string]> = axes().map((a, k) => [title(a), fmtAxis(a, row.v[k])]);
    if (!axes().includes("res:cost") && Number.isFinite(o.resources?.cost)) rows.push(["Cost / task", price(o.resources.cost)]);
    for (const plan of plansFor(row.p.entity)) {
      const per = tasksPerEntitlement(plan.value, o.resources?.cost);
      if (per && !axes().includes(`sub:${plan.name}`)) rows.push([`Tasks / week · ${plan.name}`, integer(per)]);
    }
    return hoverHTML(label(row.p), rows, [row.p.entity.family, best().has(key(row.p)) ? "best trade-off" : null,
      me()?.mine.has(row.p.entity.id) ? "in measured usage" : null,
      plans().length && !plansFor(row.p.entity).length ? "no measured plan" : null].filter(Boolean).join(" · "));
  };

  // ---- surface: two "less is better" resources and one score
  const surfaceAxes = createMemo(() => {
    if (!is3d()) return null;
    const lower = axes().map((a, k) => ({ a, k })).filter(({ a }) => !higherIsBetter(a) && axisIsLog(a) && !isPlanAxis(a));
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
  // Plans: every measured plan's tasks per week on the chosen benchmark, with each plan's best trade-offs as its curve.
  const planSeries = createMemo<PlanSeries[]>(() => {
    const seen = new Map<string, number>();
    return plans().map((plan) => {
      const n = seen.get(plan.provider) ?? 0;
      seen.set(plan.provider, n + 1);
      const points = base().filter((p: any) => providerOf(p.entity) === plan.provider && p.observation.resources?.cost > 0
          && !hidden().has(familyGroup(p.entity.family)) && (!needle() || `${p.entity.label} ${p.entity.id}`.toLowerCase().includes(needle())))
        .map((p: any) => {
          const week = plan.value / p.observation.resources.cost;
          const dollar = plan.monthly ? week / (plan.monthly * 12 / 52) : null;
          const tasks = perDollar() ? dollar ?? 0 : week;
          return { key: key(p), label: label(p), short: p.entity.label, tasks, score: p.observation.score,
            hover: hoverHTML(label(p), [["Plan", plan.name + (plan.monthly ? ` · $${plan.monthly}/mo` : "")], ["Tasks / week", integer(week)],
              ...(dollar ? [["Tasks / $", dollar < 10 ? dollar.toFixed(1) : integer(dollar)] as [string, string]] : []),
              [meta()?.label ?? "Score", fmtScore(p.observation.score, meta()?.format)], ["Cost / task", price(p.observation.resources.cost)]]), best: false };
        });
      const keep = new Set(paretoN(points, [(q: any) => q.tasks, (q: any) => q.score], [true, true]).map((q: any) => q.key));
      for (const q of points) q.best = keep.has(q.key);
      return { name: plan.name, colour: familyColor(provider(plan.provider)), dash: n > 0, points: points.filter((q: any) => q.tasks > 0) };
    }).filter((s) => s.points.length);
  });
  const chartOption = (size: { width: number; height: number }) => {
    if (view() === "plans") return optionPlans({ plans: planSeries(), score: axisMeta("score"), labels: labels(), size, perDollar: perDollar() });
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
      size,
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
  const pin = (id: string) => { const next = new Set<string>(pinned()); next.has(id) ? next.delete(id) : next.add(id); setPinned(next); };
  const onClick = (event: any) => {
    const id = event?.data?.id ?? event?.data?.name ?? event?.name;
    if (typeof id !== "string") return;
    if (is3d()) setSelected(id === selected() ? null : id);
    pin(id.split("|")[0]);
  };
  const compare = createMemo(() => items().filter((r: any) => pinned().has(r.p.entity.id)));
  const choose = (set: (v: string) => void) => (v: string) => { set(v); setSelected(null); };
  const swap = () => { const x = xAxis(); setX(yAxis()); setY(x); };
  const fmtList = (row: any) => axes().map((a, k) => axisKind(a) === "resource" || isPlanAxis(a) ? fmtAxis(a, row.v[k])
    : fmtScore(row.v[k], (a === "score" ? meta() : meta(a.slice(5)))?.format, true)).join(" · ");

  return <Site page="models" title="Models" wide fill>
    <div class="sb-toolbar">
      <Picker lead value={lens()} groups={lensGroups()} onChoose={choose(setLens)} placeholder="Find a benchmark or index" title="Benchmark" width="xl" />
      <Show when={plans().length}>
        <Segmented label="View" size="sm" value={view()} onChange={setView}
          options={[{ value: "chart", label: "Chart" }, { value: "plans", label: "Plans" }]} />
      </Show>
      <Show when={view() === "plans"}>
        <Segmented label="Basis" size="sm" value={perDollar() ? "dollar" : "week"} onChange={(v) => setPerDollar(v === "dollar")}
          options={[{ value: "week", label: "Per week" }, { value: "dollar", label: "Per $" }]} />
      </Show>
      <Show when={view() === "chart"}>
      <span class="sb-pick-group">
        <Picker label="X" value={xAxis()} groups={axisGroups()} onChoose={choose(setX)} placeholder="Find an axis" />
        <button type="button" class="sb-icon-button" aria-label="Swap X and Y" title="Swap X and Y" onClick={swap}><ArrowLeftRightIcon /></button>
        <Picker label="Y" value={yAxis()} groups={axisGroups()} onChoose={choose(setY)} placeholder="Find an axis" />
      </span>
      <span class="sb-pick-group">
        <Picker label="Z" value={zAxis()} groups={axisGroups()} onChoose={choose(setZ)} placeholder="Find an axis" empty="Add a third axis" />
        <Show when={zAxis()}><button type="button" class="sb-icon-button" aria-label="Remove Z" title="Remove Z" onClick={() => setZ(NONE)}><XIcon /></button></Show>
      </span>
      </Show>
      <Show when={is3d() && view() === "chart"}>
        <Segmented label="Third axis" size="sm" value={orbit() ? "orbit" : "flat"} onChange={(v) => setOrbit(v === "orbit")}
          options={[{ value: "orbit", label: "3D" }, { value: "flat", label: "Colour" }]} />
      </Show>
      <span class="grow" />
      <Segmented label="Entities" size="sm" value={mode()} onChange={(v) => { setMode(v); setHidden(new Set<string>()); setPinned(new Set<string>()); }}
        options={[{ value: "model", label: "Models" }, { value: "agent", label: "Agents" }]} />
      <Show when={plans().length}>
        <button type="button" class="sb-chip" aria-pressed={onPlans()} onClick={() => setOnPlans(!onPlans())} title="Only models a measured plan covers">Plan-covered</button>
      </Show>
      <div class="sb-find"><Input type="search" placeholder="Find" aria-label="Find a model" value={search()} onInput={(e) => setSearch(e.currentTarget.value)} /></div>
      <ToggleMenu label="Families" count={hidden().size ? `${families().length - hidden().size}/${families().length}` : undefined}
        rows={families().map((f) => ({ label: f.name, on: !hidden().has(f.name), toggle: () => toggle(f.name), meta: String(f.count), swatch: f.colour }))}
        actions={<div style={{ display: "flex", gap: "var(--space-xs)", padding: "var(--space-xs)" }}>
          <button type="button" class="sb-chip" onClick={() => setHidden(new Set<string>())}>All</button>
          <button type="button" class="sb-chip" onClick={() => setHidden(new Set<string>(families().map((f) => f.name)))}>None</button>
        </div>} />
      <ToggleMenu label="Display" icon={<SlidersHorizontalIcon />} rows={[
        { label: "Names", on: labels(), toggle: () => setLabels(!labels()) },
        { label: "Log scale", on: log(), toggle: () => setLog(!log()) },
        ...(mode() === "model" && !is3d() ? [{ label: "Reasoning tiers joined", on: tiers(), toggle: () => setTiers(!tiers()) }] : []),
        { label: "Best trade-offs only", on: bestOnly(), toggle: () => setBestOnly(!bestOnly()) },
        ...(surfaceAxes() && orbit() ? [{ label: "Best-within-budget surface", on: surface(), toggle: () => setSurface(!surface()) }] : []),
        { label: "Side panel", on: rail(), toggle: () => setRail(!rail()) },
      ]} />
    </div>

    <div class="sb-canvas" data-rail={rail() ? "on" : "off"}>
      <section class="sb-canvas-main" aria-label="Chart">
        <div class="sb-canvas-head">
          <h2>{view() === "plans" ? `${meta()?.label ?? ""} vs tasks per ${perDollar() ? "dollar" : "week"}, by plan`
            : `${title(yAxis())} vs ${title(xAxis())}${is3d() ? (orbit() ? ` vs ${title(zAxis())}` : ` · colour: ${title(zAxis())}`) : ""}`}</h2>
          <div class="sb-chart-legend">
            <Show when={view() === "plans"} fallback={<>
            <span><i data-best="" />best trade-off</span>
            <Show when={me()?.mine.size}><span><i data-ring="" />in measured usage</span></Show>
            <span>{items().length} shown · {is3d() && orbit() ? "drag to orbit" : "scroll to zoom"}</span></>}>
              <span>{perDollar() ? "tasks / $ = tasks per week ÷ the week's share of the plan's price" : "tasks / week = plan value ÷ cost per task"} · each line a plan's best trade-offs</span>
            </Show>
          </div>
        </div>
        <div class="sb-canvas-chart">
          <Show when={index()} fallback={<Skeleton />}>
            <Show when={view() === "plans" ? planSeries().length > 0 : items().length >= 2} fallback={<div class="sb-chart-empty" style={{ height: "100%" }}>No models have all of these axes</div>}>
              <EChart option={chartOption} onClick={onClick} label="Models chart" />
            </Show>
          </Show>
        </div>
      </section>

      <Show when={rail()}>
        <aside class="sb-rail">
          <Show when={plans().length}>
            <Inset title="Measured plans" tight aside={<span class="sb-tag" data-tone="quiet">value ÷ price</span>}>
              <For each={plans()}>{(plan) =>
                <div class="sb-plan-row">
                  <Swatch colour={familyColor(provider(plan.provider))} />
                  <span>{plan.name}{plan.monthly ? <span class="muted"> · ${plan.monthly}/mo</span> : null}</span><b>{plan.multiple ? `${plan.multiple.toFixed(1)}×` : `$${Math.round(plan.value).toLocaleString()}/wk`}</b>
                  <small>${Math.round(plan.value).toLocaleString()} of API value a week · {provider(plan.provider)} models · {plan.accounts} {plan.accounts === 1 ? "account" : "accounts"}</small>
                </div>}</For>
            </Inset>
          </Show>
          <Show when={view() === "plans"}>
            <For each={planSeries()}>{(series) =>
              <Inset title={series.name} tight aside={<span class="sb-tag" data-tone="quiet">{series.points.filter((q) => q.best).length}</span>}>
                <div class="sb-rows"><For each={series.points.filter((q) => q.best).sort((a, b) => b.score - a.score)}>{(q) =>
                  <DashRow primary={q.label} trailing={`${fmtScore(q.score, meta()?.format, true)} · ${perDollar() ? (q.tasks < 10 ? q.tasks.toFixed(1) : integer(q.tasks)) + "/$" : integer(q.tasks) + "/wk"}`}
                    lead={<Swatch colour={series.colour} />} />}</For></div>
              </Inset>}</For>
          </Show>
          <Show when={is3d() && orbit() && view() === "chart"}>
            <Inset title="Camera" tight>
              <Segmented label="Camera" size="sm" value={camera()} onChange={setCamera}
                options={[{ value: "iso", label: "3D" }, { value: "front", label: "Front" }, { value: "side", label: "Side" }, { value: "top", label: "Top" }]} />
            </Inset>
          </Show>
          <Show when={view() === "chart" && surfaceAxes()}>{(sa) =>
            <Inset title="Budget" tight>
              <label class="sb-range"><span class="sb-range-head">{title(sa().a.a)} ≤ <b>{fmtAxis(sa().a.a, caps()!.a)}</b></span>
                <input type="range" min="0" max="1000" value={posA()} onInput={(e) => setPosA(Number(e.currentTarget.value))} /></label>
              <label class="sb-range"><span class="sb-range-head">{title(sa().b.a)} ≤ <b>{fmtAxis(sa().b.a, caps()!.b)}</b></span>
                <input type="range" min="0" max="1000" value={posB()} onInput={(e) => setPosB(Number(e.currentTarget.value))} /></label>
              <div style={{ "margin-top": "var(--space-md)" }}>
                <Show when={probe()} fallback={<DashEmpty>None</DashEmpty>}>{(row) =>
                  <div class="sb-callout"><b>{label(row().p)}</b>
                    <dl class="sb-kv" style={{ "margin-top": "var(--space-sm)" }}>
                      <For each={axes()}>{(a, k) => <><dt>{title(a)}</dt><dd>{fmtAxis(a, row().v[k()])}</dd></>}</For>
                    </dl></div>}</Show>
              </div>
            </Inset>}</Show>
          <Show when={view() === "chart" && solver()}>{(s) => {
            const covering = () => plansFor(s().row.p.entity);
            return <Inset title="Cheapest at target" tight>
              <Slider label="Target" value={target()} onChange={setTarget}
                options={[50, 60, 70, 75, 80, 85, 90, 95, 100].map((v) => ({ value: String(v), label: `${v}%` }))} />
              <div class="sb-callout" style={{ "margin-top": "var(--space-md)" }}><b>{label(s().row.p)}</b>
                <dl class="sb-kv" style={{ "margin-top": "var(--space-sm)" }}>
                  <For each={axes()}>{(a, k) => <><dt>{title(a)}</dt><dd>{fmtAxis(a, s().row.v[k()])}</dd></>}</For>
                  <For each={covering()}>{(plan) => <Show when={tasksPerEntitlement(plan.value, s().row.p.observation.resources?.cost)}>{(per) =>
                    <><dt>Tasks / week · {plan.name}</dt><dd>{integer(per())}</dd></>}</Show>}</For>
                </dl></div>
            </Inset>;
          }}</Show>
          <Show when={compare().length}>
            <Inset title="Pinned" tight aside={<button type="button" class="sb-chip" onClick={() => setPinned(new Set<string>())}>Clear</button>}>
              <div class="sb-rows"><For each={compare()}>{(row: any) =>
                <DashRow element="button" onClick={() => pin(row.p.entity.id)} lead={<Swatch colour={familyColor(row.p.entity.family)} />}
                  primary={label(row.p)} trailing={fmtList(row)} />}</For></div>
            </Inset>
          </Show>
          <Show when={view() === "chart"}>
          <Inset title="Best trade-offs" tight aside={<span class="sb-tag" data-tone="quiet">{bestList().length}</span>}>
            <div class="sb-rows">
              <For each={bestList()} fallback={<DashEmpty>None</DashEmpty>}>{(row: any, i) =>
                <DashRow element="button" current={pinned().has(row.p.entity.id) || selected() === key(row.p)}
                  onClick={() => { if (is3d()) setSelected(selected() === key(row.p) ? null : key(row.p)); pin(row.p.entity.id); }}
                  lead={<Swatch colour={familyColor(row.p.entity.family)} />}
                  primary={<><span class="sb-rank">{String(i() + 1).padStart(2, "0")}</span>{label(row.p)}</>}
                  trailing={fmtList(row)} />}</For>
            </div>
          </Inset>
          </Show>
        </aside>
      </Show>
    </div>
  </Site>;
}

mount(() => <Models />);
