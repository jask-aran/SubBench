import { createEffect, createMemo, createResource, createSignal, For, Show, type JSX } from "solid-js";
import { Site, mount } from "../app/shell";
import { Skeleton } from "../app/bits";
import { Picker, ToggleMenu, type PickGroup } from "../app/picker";
import { ArrowLeftRightIcon, XIcon, SlidersHorizontalIcon, LinkIcon, CheckIcon, GripVerticalIcon } from "lucide-solid";
import { hoverHTML } from "../app/charts";
import { EChart, type ChartApi } from "../app/echart";
import { option2d, option3d, optionPlans, VIEWS3D, type Row, type PlanSeries } from "../app/modelchart";
// @ts-ignore
import { planPrice, valueMultiple } from "../lib/plans.js";
import { RESOURCE_FORMAT } from "../app/points";
import { loadFrontier, loadReports, personal } from "../app/data";
import { Segmented, Input, Swatch } from "../conduit/ui/controls";
// @ts-ignore
import { price, duration, integer, score as fmtScore } from "../lib/format.js";
// @ts-ignore
import { points as metricPoints, availableResources, familyColor, familyGroup, RESOURCES, tasksPerEntitlement, baseKey, AXIS_RESOURCES, axisKind, higherIsBetter, axisIsLog, axisValue, scoreLookup, paretoN, attainmentGridBy, bestWithinBy } from "../lib/frontier.js";

// The fewest models that make a chart worth drawing. A measure that shares fewer with the plotted ones is greyed.
const FEW = 5;

/*
 * Models: every public benchmark in one chart. A reader starts from a question, or composes a chart by dragging
 * measures from the Measures pane onto Up, Across and a third place (colour or depth). Each measure says how many
 * models it shares with what is already plotted. The best trade-offs are the models nothing beats on every chosen
 * measure at once. Your limits (cost, time, score) fade what is out of reach and ring the best model inside them;
 * in 3D with cost and time on the floor, the same limits draw the budget box under the best-within-budget surface.
 */

type Mode = "model" | "agent";
const params = new URLSearchParams(location.search);
const toLog = (pos: number, [lo, hi]: [number, number]) => 10 ** (Math.log10(lo) + (Math.log10(hi) - Math.log10(lo)) * pos / 1000);
const NONE = "";
// Colours for a provider's second and later plans: chart series slots no provider uses.
const PLAN_COLOURS = ["var(--series-5)", "var(--series-7)", "var(--series-4)", "var(--series-8)", "var(--series-1)"];

function Models() {
  const [index] = createResource(loadFrontier);
  const [reports] = createResource(loadReports);
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const weekly = () => me()?.products[0]?.headline.value ?? null;
  // Every plan SubBench has measured, pooled across the accounts that report it. A plan covers its provider's models:
  // Claude plans Anthropic's, ChatGPT plans OpenAI's. A scoped limit ("Claude Max 5X · Fable") is a model family's own
  // sub-limit: it covers that family only, and its plan then covers the provider's other models.
  type Plan = { name: string; provider: string; scope: string | null; value: number; kind: string; accounts: number; monthly: number | null; multiple: number | null; colour: string };
  const provider = (key: string) => (key === "claude" ? "Anthropic" : key === "codex" ? "OpenAI" : key);
  const scopeOf = (name: string) => name.split(" · ")[1]?.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-") || null;
  const plans = createMemo<Plan[]>(() => (me()?.products ?? [])
    .filter((item: any) => item.card.provider)
    .map((item: any) => ({ name: item.card.name, provider: item.card.provider, scope: scopeOf(item.card.name), value: item.headline.value, kind: item.headline.kind,
      accounts: item.headline.row?.account_count ?? 1,
      monthly: planPrice(item.card.name)?.monthly ?? null, multiple: valueMultiple(item.card.name, item.headline.value), colour: "" }))
    .sort((a: Plan, b: Plan) => a.provider.localeCompare(b.provider) || a.value - b.value)
    // Each plan its own colour, so two plans of one provider can be told apart. The provider's first full plan (not a
    // scoped limit such as Fable) keeps the provider's colour.
    .map((plan: Plan, i: number, all: Plan[]) => {
      const main = all.findIndex((other) => other.provider === plan.provider && !other.scope) === i;
      return { ...plan, colour: main ? familyColor(provider(plan.provider)) : PLAN_COLOURS[i % PLAN_COLOURS.length] };
    }));
  const providerOf = (entity: any) => {
    const text = `${entity.provider ?? ""} ${entity.family ?? ""} ${entity.agent ?? ""}`.toLowerCase();
    return text.includes("anthropic") || text.includes("claude") ? "claude" : text.includes("openai") || text.includes("codex") ? "codex" : null;
  };
  const inScope = (entity: any, scope: string) =>
    `-${[entity.id, entity.label, entity.model, entity.host_model_slug].filter(Boolean).join(" ")}-`.toLowerCase()
      .replace(/[^a-z0-9]+/g, "-").includes(`-${scope}-`);
  const covers = (plan: Plan, entity: any) => plan.provider === providerOf(entity) && (plan.scope
    ? inScope(entity, plan.scope)
    : !plans().some((other) => other.provider === plan.provider && other.scope && inScope(entity, other.scope)));
  const plansFor = (entity: any) => plans().filter((plan) => covers(plan, entity));
  const planNamed = (axis: string) => plans().find((plan) => `sub:${plan.name}` === axis) ?? null;
  const [onPlans, setOnPlans] = createSignal(false);
  // Chart: any axes. Plans: how much of the chosen benchmark's work each measured plan gets done per week.
  const [view, setView] = createSignal<"chart" | "plans">(params.get("view") === "plans" ? "plans" : "chart");
  // Plans per week (what a limit gets done) or per dollar (what the money gets done, comparable across plan sizes).
  const [basis, setBasis] = createSignal<"week" | "dollar" | "price">(params.get("basis") === "dollar" ? "dollar" : params.get("basis") === "price" ? "price" : "week");
  const perDollar = () => basis() === "dollar";

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
  const [surface, setSurface] = createSignal(params.get("surface") === "1");
  const [chartApi, setChartApi] = createSignal<ChartApi | null>(null);
  const [dragging, setDragging] = createSignal<string | null>(null);
  const [placing, setPlacing] = createSignal<string | null>(null);
  const [search, setSearch] = createSignal("");
  const [hidden, setHidden] = createSignal<Set<string>>(new Set());
  const [pinned, setPinned] = createSignal<Set<string>>(new Set());
  const [selected, setSelected] = createSignal<string | null>(null);
  // Your limits: slider positions 0-1000 on each measure's own range. A ceiling at 1000, or a floor at 0, is no limit.
  const [limitAt, setLimitAt] = createSignal<Record<string, number>>({});

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
      ...(plans().length && own().includes("cost") ? [{ title: "Your plans · tasks a week", items: plans().map((plan) => ({
        value: `sub:${plan.name}`, label: `Tasks / week · ${plan.name}`, short: plan.name,
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
    const q = new URLSearchParams({ ...(mode() === "agent" ? { mode: "agent" } : {}), ...(view() === "plans" ? { view: "plans", ...(basis() !== "week" ? { basis: basis() } : {}) } : {}), lens: lens(), x: xAxis(), y: yAxis(), ...(zAxis() ? { z: zAxis() } : {}), ...(zAxis() && !orbit() ? { orbit: "0" } : {}), ...(zAxis() && orbit() && surface() ? { surface: "1" } : {}) });
    history.replaceState(null, "", `${location.pathname}?${q}`);
  });

  const axes = () => (zAxis() ? [xAxis(), yAxis(), zAxis()] : [xAxis(), yAxis()]);
  const is3d = () => axes().length === 3;
  // Every benchmark's scores by model, so any measure can be counted against the plotted ones.
  const lookups = createMemo(() => {
    const out = new Map();
    if (index()) for (const m of metrics()) out.set(m.id, scoreLookup(index()!, m.id));
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
    return plan && covers(plan, p.entity) && cost > 0 ? plan.value / cost : null;
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
  // The budget box on the floor: the reader's limits on the two floor measures, when both are set.
  const caps = createMemo(() => {
    const s = surfaceAxes();
    if (!s) return null;
    const a = limitValue(s.a.a), b = limitValue(s.b.a);
    return a !== null && b !== null ? { a, b } : null;
  });
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
      mine: !!me()?.mine.has(row.p.entity.id), pinned: pinned().has(row.p.entity.id),
      dim: (bestOnly() && !best().has(key(row.p))) || (limited() && !within(row.p)),
      named: named.has(key(row.p)) || !!me()?.mine.has(row.p.entity.id), fit: fit() !== null && key(fit().p) === key(row.p),
    }));
  });
  // Plans: every measured plan's tasks per week on the chosen benchmark, with each plan's best trade-offs as its curve.
  const planSeries = createMemo<PlanSeries[]>(() => {
    const seen = new Map<string, number>();
    return plans().map((plan) => {
      const n = seen.get(plan.provider) ?? 0;
      seen.set(plan.provider, n + 1);
      const points = base().filter((p: any) => covers(plan, p.entity) && p.observation.resources?.cost > 0
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
      return { name: plan.name, colour: plan.colour, dash: n > 0, points: points.filter((q: any) => q.tasks > 0) };
    }).filter((s) => s.points.length);
  });
  // Plans in 3D: tasks per week, the plan's weekly price, the score. Each plan's curve sits at its price, and the
  // surface is the best score reachable at a given workload for at most a given weekly spend.
  const weekPrice = (plan: Plan) => plan.monthly ? plan.monthly * 12 / 52 : null;
  const plans3d = () => {
    const priced = planSeries().map((s, i) => ({ s, plan: plans().find((p) => p.name === s.name)! })).filter((x) => x.plan && weekPrice(x.plan));
    if (!priced.length) return null;
    const rows: Row[] = [];
    const rowsScore = new Map<string, number>();
    const lines: Array<{ colour: string; dash: boolean; points: number[][] }> = [];
    // One name per price level (the top score there): plans at the same price would otherwise stack their labels.
    const topAt = new Map<number, string>();
    for (const { s, plan } of priced) for (const q of s.points) {
      const w = weekPrice(plan)!, cur = topAt.get(w);
      const curScore = cur ? rowsScore.get(cur) ?? -Infinity : -Infinity;
      if (q.best && q.score > curScore) { topAt.set(w, `${q.key}|${plan.name}`); rowsScore.set(`${q.key}|${plan.name}`, q.score); }
    }
    const named = new Set(topAt.values());
    for (const { s, plan } of priced) {
      const w = weekPrice(plan)!;
      const best = s.points.filter((q) => q.best).sort((a, b) => a.tasks - b.tasks);
      for (const q of s.points) rows.push({ key: `${q.key}|${plan.name}`, label: q.label, short: q.short, colour: s.colour, group: plan.name,
        hover: q.hover, v: [q.tasks, w, q.score], best: q.best, mine: false, pinned: false, dim: false, named: named.has(`${q.key}|${plan.name}`) });
      if (best.length > 1) lines.push({ colour: s.colour, dash: s.dash, points: best.map((q) => [q.tasks, w, q.score]) });
    }
    const tasks = rows.map((r) => r.v[0]);
    const lo = Math.log10(Math.min(...tasks)), hi = Math.log10(Math.max(...tasks));
    const a = Array.from({ length: 36 }, (_, i) => 10 ** (lo + (hi - lo) * i / 35));
    const b = [...new Set(rows.map((r) => r.v[1]))].sort((x, y) => x - y);
    const z = b.map((price) => a.map((t) => {
      let m: number | null = null;
      for (const r of rows) if (r.v[1] <= price && r.v[0] >= t && (m === null || r.v[2] > m)) m = r.v[2];
      return m;
    }));
    return { rows, lines, surface: b.length > 1 ? { a, b, z } : null };
  };
  const chartOption = (size: { width: number; height: number }) => {
    if (view() === "plans" && basis() === "price") {
      const d = plans3d();
      if (d) return option3d({ size, rows: d.rows, lines: d.lines, surface: d.surface, labels: labels(), selected: selected(), view: "iso",
        axes: [{ title: "Tasks / week", log: true, unit: "integer", higherBetter: true }, { title: "Plan price / week", log: false, unit: "currency", higherBetter: false }, axisMeta("score")] });
    }
    if (view() === "plans") return optionPlans({ plans: planSeries(), score: axisMeta("score"), labels: labels(), size, perDollar: perDollar() });
    const r = rows();
    if (flat()) {
      const [x, y, z] = axes();
      return option2d({
        rows: r, axes: [axisMeta(x), axisMeta(y)], colourAxis: z ? axisMeta(z) : null, size, labels: labels(),
        // The staircase reads "nothing above and to the left" only for a cost-like x and a score y, in 2D.
        step: !z && !higherIsBetter(x) && higherIsBetter(y), tiers: !z && tiers() && mode() === "model",
        limits: LIMITS().flatMap((l) => {
          const v = limitValue(l.key), k = [x, y].indexOf(l.key);
          return v === null || k < 0 ? [] : [{ axis: k as 0 | 1, value: v, label: `${l.floor ? "≥" : "≤"} ${fmtLimit(l.key, v)}` }];
        }),
      });
    }
    // 3D: Across stays across, Up goes up and the third measure is depth. With a surface, the score goes up and the
    // two ceilings lie on the floor, whichever shelf each is on. echarts-gl orders its axes across, depth, up.
    const s = surfaceAxes();
    const order = s ? [s.a.k, s.b.k, s.s.k] : [0, 2, 1];
    const c = caps(), pr = probe();
    return option3d({
      size,
      rows: r.map((row) => ({ ...row, v: order.map((k) => row.v[k]) })), axes: order.map((k) => axisMeta(axes()[k])) as any,
      labels: labels(), selected: selected(), view: "iso", surface: grid(),
      probe: s && c ? { a: c.a, b: c.b, z: pr ? pr.v[s.s.k] : null, a0: span(s.a.k)[0], b0: span(s.b.k)[0] } : null,
    });
  };

  // ---- questions: named views, so a reader starts from a question rather than from empty axes
  type Question = { id: string; title: string; read: string; view?: "chart" | "plans"; basis?: "week" | "dollar" | "price";
    x?: string; y?: string; z?: string; orbit?: boolean; surface?: boolean; needs?: string[]; plans?: boolean };
  // The benchmark from another source that scores the most of this one's models. One from the same source (the parts
  // of an index) agrees with it by construction.
  const otherLens = createMemo(() => {
    if (!index()) return undefined;
    const ids = new Set(base().map((p: any) => p.entity.id));
    // The source is the group's first word: "AA indexes" and "AA benchmarks" are both Artificial Analysis.
    const source = (m: any) => String(m?.group ?? "").split(" ")[0];
    const pick = (other: boolean) => {
      let bestId: string | undefined, most = 9;
      for (const m of metrics() as any[]) {
        if (m.id === lens() || m.group === meta()?.group || (other && source(m) === source(meta()))) continue;
        let shared = 0;
        for (const id of scoreLookup(index()!, m.id).byEntity.keys()) if (ids.has(id)) shared++;
        if (shared > most) { most = shared; bestId = m.id; }
      }
      return bestId;
    };
    return pick(true) ?? pick(false);
  });
  const tokens = () => own().includes("output_tokens") ? "res:output_tokens" : own().includes("total_tokens") ? "res:total_tokens" : own().includes("steps") ? "res:steps" : null;
  const questions = createMemo<Question[]>(() => ([
    { id: "money", title: "Best score for the money", read: "Up is a better score, left is cheaper per task. The line joins the models nothing beats on both.",
      x: "res:cost", y: "score", needs: ["cost"] },
    { id: "plans", title: "What each plan gets done", read: "Tasks one weekly limit pays for, at public API prices. Each line is a plan's best trade-offs.",
      view: "plans", basis: "week", plans: true, needs: ["cost"] },
    { id: "dollar", title: "Most work per dollar", read: "Tasks per week for each dollar of the plan's price, so plans of different sizes compare.",
      view: "plans", basis: "dollar", plans: true, needs: ["cost"] },
    { id: "fast", title: "Smart and fast", read: "Up is a better score, left is quicker per task.", x: "res:time", y: "score", needs: ["time"] },
    ...(tokens() ? [{ id: "tokens", title: tokens() === "res:steps" ? "Fewest steps" : "Token appetite",
      read: "Up is a better score, left uses fewer tokens per task.", x: tokens()!, y: "score" }] : []),
    ...(otherLens() ? [{ id: "agree", title: "Do two benchmarks agree?",
      read: `Each point is a model on both. Points off the diagonal trend do better on one benchmark than the other.`, x: `lens:${otherLens()}`, y: "score" }] : []),
    { id: "surface", title: "Cost, time and score", read: "The surface is the best score you can get within a cost and a time budget. Drag to turn it.",
      x: "res:cost", y: "score", z: "res:time", orbit: true, surface: true, needs: ["cost", "time"] },
  ] as Question[]).filter((q) => (!q.needs || q.needs.every((r) => own().includes(r))) && (!q.plans || plans().length > 0)));
  const asked = createMemo(() => questions().find((q) => (q.view ?? "chart") === view() && (q.view === "plans"
    ? q.basis === basis()
    : q.x === xAxis() && q.y === yAxis() && (q.z ?? NONE) === zAxis() && (!q.z || orbit() === !!q.orbit))) ?? null);
  const ask = (q: Question) => {
    setView(q.view ?? "chart");
    if (q.basis) setBasis(q.basis);
    if (q.x) setX(q.x);
    if (q.y) setY(q.y);
    if (!q.view || q.view === "chart") setZ(q.z ?? NONE);
    if (q.z) { setOrbit(!!q.orbit); setSurface(!!q.surface); }
    setSelected(null);
  };
  const [copied, setCopied] = createSignal(false);
  const copyLink = async () => {
    try { await navigator.clipboard.writeText(location.href); setCopied(true); setTimeout(() => setCopied(false), 1600); } catch { /* no clipboard: the address bar has it */ }
  };

  // ---- your limits: what you will pay, wait and accept, on any chart
  const LIMITS = createMemo(() => [
    ...(own().includes("cost") ? [{ key: "res:cost", label: "Cost per task", floor: false }] : []),
    ...(own().includes("time") ? [{ key: "res:time", label: "Time per task", floor: false }] : []),
    { key: "score", label: meta()?.short_label || meta()?.label || "Score", floor: true },
  ]);
  // Declarations, not arrow constants: the 3D budget box above reads them before this point in the body.
  function limitRange(key: string): [number, number] {
    const vs = base().map((p: any) => axisValue(key, p)).filter((v: any) => Number.isFinite(v) && (key === "score" || v > 0)) as number[];
    return vs.length ? [Math.min(...vs), Math.max(...vs)] : [0, 1];
  }
  function limitLog(key: string) { return key !== "score"; }
  function limitValue(key: string): number | null {
    const pos = limitAt()[key];
    const floor = key === "score";
    if (pos === undefined || (floor ? pos <= 0 : pos >= 1000)) return null;
    const [lo, hi] = limitRange(key);
    return limitLog(key) ? toLog(pos, [lo, hi]) : lo + (hi - lo) * pos / 1000;
  }
  const limited = () => LIMITS().some((l) => limitValue(l.key) !== null);
  const within = (p: any) => LIMITS().every((l) => {
    const cap = limitValue(l.key);
    if (cap === null) return true;
    const v = axisValue(l.key, p);
    return Number.isFinite(v) && (l.floor ? v >= cap : v <= cap * (1 + 1e-9));
  });
  const fit = createMemo(() => {
    if (!limited()) return null;
    let best: any = null;
    for (const row of items() as any[]) {
      if (!within(row.p)) continue;
      const s = row.p.observation.score, c = row.p.observation.resources?.cost ?? Infinity;
      if (!best || s > best.p.observation.score || (s === best.p.observation.score && c < (best.p.observation.resources?.cost ?? Infinity))) best = row;
    }
    return best;
  });
  const fmtLimit = (key: string, v: number) => key === "score" ? fmtScore(v, meta()?.format) : (RESOURCE_FORMAT[RESOURCES[key.slice(4)]?.unit] ?? String)(v);

  // ---- the measures: every one a reader can place, and how many models it shares with what is plotted
  const placedAs = (a: string) => a === yAxis() ? "Up" : a === xAxis() ? "Across" : a && a === zAxis() ? (orbit() ? "Depth" : "Colour") : null;
  const shared = (a: string) => {
    const others = [xAxis(), yAxis()].filter((o) => o !== a);
    let n = 0;
    for (const p of base() as any[]) {
      if (hidden().has(familyGroup(p.entity.family))) continue;
      if (valueOn(a, p) === null || others.some((o) => valueOn(o, p) === null)) continue;
      n++;
    }
    return n;
  };
  const place = (slot: "x" | "y" | "z", a: string) => {
    setView("chart");
    // Dropping a measure where it already sits elsewhere moves it, and the measure it displaces takes its old place.
    const from = placedAs(a);
    const current = slot === "x" ? xAxis() : slot === "y" ? yAxis() : zAxis();
    if (from === "Up" && slot !== "y") setY(current || "score");
    if (from === "Across" && slot !== "x") setX(current || "res:cost");
    if ((from === "Depth" || from === "Colour") && slot !== "z") setZ(current && current !== a ? current : NONE);
    // A new third measure starts as colour: a flat chart reads at a glance, and depth is one click away.
    if (slot === "z" && !zAxis()) setOrbit(false);
    (slot === "x" ? setX : slot === "y" ? setY : setZ)(a);
    setSelected(null); setPlacing(null); setDragging(null);
  };
  const dropOn = (slot: "x" | "y" | "z") => ({
    onDragOver: (e: DragEvent) => { if (dragging()) { e.preventDefault(); (e.currentTarget as HTMLElement).dataset.over = ""; } },
    onDragLeave: (e: DragEvent) => { delete (e.currentTarget as HTMLElement).dataset.over; },
    onDrop: (e: DragEvent) => { e.preventDefault(); delete (e.currentTarget as HTMLElement).dataset.over; const a = e.dataTransfer?.getData("text/plain") || dragging(); if (a) place(slot, a); },
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

  const measureRow = (item: any, colour?: string) => {
    const n = createMemo(() => shared(item.value));
    const at = () => placedAs(item.value);
    return <li class="sb-measure" data-placed={at() ? "" : undefined} data-few={!at() && n() < FEW ? "" : undefined}
      draggable={true} title={item.detail ?? item.label}
      onDragStart={(e) => { e.dataTransfer?.setData("text/plain", item.value); if (e.dataTransfer) e.dataTransfer.effectAllowed = "copy"; setDragging(item.value); }}
      onDragEnd={() => setDragging(null)}>
      <button type="button" class="sb-measure-name" aria-expanded={placing() === item.value}
        onClick={() => setPlacing(placing() === item.value ? null : item.value)}>
        <GripVerticalIcon class="sb-measure-grip" />
        <Show when={colour}><Swatch colour={colour!} /></Show>
        <span>{item.short ?? item.label}</span>
        <small>{at() ?? `${n()}`}</small>
      </button>
      <Show when={placing() === item.value}>
        <div class="sb-measure-put" role="group" aria-label={`Put ${item.label}`}>
          <button type="button" onClick={() => place("y", item.value)}>Up</button>
          <button type="button" onClick={() => place("x", item.value)}>Across</button>
          <button type="button" onClick={() => place("z", item.value)}>Third</button>
        </div>
      </Show>
    </li>;
  };
  const planColour = (value: string) => plans().find((plan) => `sub:${plan.name}` === value)?.colour;
  const slot = (props: { name: string; slot: "x" | "y" | "z"; children: JSX.Element }) =>
    <span class="sb-shelf" data-slot={props.slot} data-dragging={dragging() ? "" : undefined} {...dropOn(props.slot)}>
      <b>{props.name}</b>{props.children}
    </span>;

  return <Site page="models" title="Models" wide fill>
    <nav class="sb-questions" aria-label="Questions">
      <For each={questions()}>{(q) =>
        <button type="button" class="sb-question" aria-pressed={asked()?.id === q.id} title={q.read} onClick={() => ask(q)}>{q.title}</button>}</For>
      <span class="grow" />
      <button type="button" class="sb-chip" title="Copy a link to this view" onClick={copyLink}>
        <Show when={copied()} fallback={<LinkIcon />}><CheckIcon /></Show>{copied() ? "Link copied" : "Copy link"}</button>
    </nav>

    <div class="sb-workbench">
      <aside class="sb-measures" aria-label="Measures">
        <div class="sb-measures-head">
            <Segmented label="Entities" size="sm" value={mode()} onChange={(v) => { setMode(v); setHidden(new Set<string>()); setPinned(new Set<string>()); }}
            options={[{ value: "model", label: "Models" }, { value: "agent", label: "Agents" }]} />
            <ToggleMenu label="Filter" count={hidden().size || onPlans() ? `${hidden().size ? `${families().length - hidden().size}/${families().length}` : "all"}${onPlans() ? " · my plans" : ""}` : undefined}
            rows={[
              ...(plans().length && view() === "chart" ? [{ label: "Only models on my plans", on: onPlans(), toggle: () => setOnPlans(!onPlans()) }] : []),
              ...families().map((f) => ({ label: f.name, on: !hidden().has(f.name), toggle: () => toggle(f.name), meta: String(f.count), swatch: f.colour })),
            ]}
            actions={<div style={{ display: "flex", gap: "var(--space-xs)", padding: "var(--space-xs)" }}>
              <button type="button" class="sb-chip" onClick={() => setHidden(new Set<string>())}>All</button>
              <button type="button" class="sb-chip" onClick={() => setHidden(new Set<string>(families().map((f) => f.name)))}>None</button>
            </div>} />
        </div>
        <Picker lead value={lens()} groups={lensGroups()} onChoose={choose(setLens)} placeholder="Find a benchmark or index" title="Benchmark" width="xl" />
        <p class="sb-measures-hint">Drag a measure onto the chart, or click it.</p>
        <For each={axisGroups()}>{(group) =>
          <section class="sb-measure-group">
            <h3>{group.title}</h3>
            <ul><For each={group.items}>{(item) => measureRow(item, planColour(item.value))}</For></ul>
          </section>}</For>
      </aside>

      <section class="sb-canvas-main" aria-label="Chart">
        <div class="sb-shelves">
          <Show when={view() === "chart"} fallback={<div class="sb-sentence">
            <span>{meta()?.label} against tasks per</span>
            <Segmented label="Basis" size="sm" value={basis()} onChange={(v) => setBasis(v as any)}
              options={[{ value: "week", label: "week" }, { value: "dollar", label: "dollar" }, { value: "price", label: "week × price, 3D" }]} />
          </div>}>
            {slot({ name: "Up", slot: "y", children:
              <Picker value={yAxis()} groups={axisGroups()} onChoose={(v) => place("y", v)} placeholder="Find a measure" title="Up the chart" /> })}
            <button type="button" class="sb-icon-button" aria-label="Swap Up and Across" title="Swap Up and Across" onClick={swap}><ArrowLeftRightIcon /></button>
            {slot({ name: "Across", slot: "x", children:
              <Picker value={xAxis()} groups={axisGroups()} onChoose={(v) => place("x", v)} placeholder="Find a measure" title="Across the chart" /> })}
            {slot({ name: zAxis() ? (orbit() ? "Depth" : "Colour") : "Third", slot: "z", children: <>
              <Picker value={zAxis()} groups={axisGroups()} onChoose={(v) => place("z", v)} placeholder="Find a measure" empty="Drop a measure" title="A third measure" />
              <Show when={zAxis()}>
                <Segmented label="Third measure as" size="sm" value={orbit() ? "orbit" : "flat"} onChange={(v) => setOrbit(v === "orbit")}
                  options={[{ value: "flat", label: "colour" }, { value: "orbit", label: "depth" }]} />
                <button type="button" class="sb-icon-button" aria-label="Remove the third measure" title="Remove the third measure" onClick={() => setZ(NONE)}><XIcon /></button>
              </Show></> })}
          </Show>
          <span class="grow" />
          <div class="sb-find"><Input type="search" placeholder="Find a model" aria-label="Find a model" value={search()} onInput={(e) => setSearch(e.currentTarget.value)} /></div>
          <ToggleMenu label="Display" icon={<SlidersHorizontalIcon />} rows={[
            { label: "Names", on: labels(), toggle: () => setLabels(!labels()) },
            { label: "Log scale", on: log(), toggle: () => setLog(!log()) },
            ...(mode() === "model" && !is3d() ? [{ label: "Reasoning tiers joined", on: tiers(), toggle: () => setTiers(!tiers()) }] : []),
            { label: "Best trade-offs only", on: bestOnly(), toggle: () => setBestOnly(!bestOnly()) },
            ...(surfaceAxes() && orbit() ? [{ label: "Best-within-budget surface", on: surface(), toggle: () => setSurface(!surface()) }] : []),
          ]} />
        </div>

        <div class="sb-canvas-head">
          <div class="sb-canvas-title">
            <h2>{asked()?.title ?? (view() === "plans" ? (basis() === "price" ? `${meta()?.label ?? ""} by tasks per week and plan price` : `${meta()?.label ?? ""} by tasks per ${perDollar() ? "dollar" : "week"}`)
              : `${title(yAxis())} against ${title(xAxis())}${is3d() ? (orbit() ? ` and ${title(zAxis())}` : `, coloured by ${title(zAxis())}`) : ""}`)}</h2>
            <p>{asked() ? `${meta()?.label ?? ""}. ${asked()!.read}` : `${meta()?.label ?? ""}. ${items().length} models have all of these.`}</p>
          </div>
          <div class="sb-chart-legend">
            <Show when={view() === "chart"}>
              <span><i data-best="" />best trade-off</span>
              <Show when={me()?.mine.size}><span><i data-ring="" />in your usage</span></Show>
              <span>{is3d() && orbit() ? "drag to turn · scroll to zoom" : "scroll to zoom · drag to pan"}</span>
            </Show>
          </div>
        </div>

        <div class="sb-canvas-chart">
          <Show when={index()} fallback={<Skeleton />}>
            <Show when={view() === "plans" ? planSeries().length > 0 : items().length >= 2} fallback={<div class="sb-chart-empty" style={{ height: "100%" }}>
              {view() === "plans" ? "No plan covers a model on this benchmark" : "Fewer than two models have all of these measures. Pick a measure with a higher count."}</div>}>
              <EChart option={chartOption} onClick={onClick} label="Models chart" api={setChartApi} />
            </Show>
          </Show>
          <Show when={(is3d() && orbit() && view() === "chart") || (view() === "plans" && basis() === "price")}>
            <div class="sb-camera" role="group" aria-label="Turn the chart">
              <For each={[["iso", "Angle"], ["front", "Front"], ["side", "Side"], ["top", "Top"]] as const}>{([v, name]) =>
                <button type="button" onClick={() => chartApi()?.snap({ ...VIEWS3D[v], distance: 205 })}>{name}</button>}</For>
            </div>
          </Show>
          <Show when={dragging()}>
            <div class="sb-dropzones">
              <div class="sb-dropzone" data-zone="y" {...dropOn("y")}>Up</div>
              <div class="sb-dropzone" data-zone="x" {...dropOn("x")}>Across</div>
              <div class="sb-dropzone" data-zone="z" {...dropOn("z")}>Third · colour or depth</div>
            </div>
          </Show>
        </div>

        <Show when={view() === "chart"}>
          <div class="sb-limits" aria-label="Your limits">
            <span class="sb-limits-name">Your limits</span>
            <For each={LIMITS()}>{(l) => {
              const value = () => limitValue(l.key);
              const pos = () => limitAt()[l.key] ?? (l.floor ? 0 : 1000);
              return <label class="sb-limit" data-set={value() !== null ? "" : undefined}>
                <span>{l.label} {l.floor ? "≥" : "≤"} <b>{value() === null ? "any" : fmtLimit(l.key, value()!)}</b></span>
                <input type="range" min="0" max="1000" value={pos()} aria-label={`${l.label} limit`}
                  onInput={(e) => setLimitAt({ ...limitAt(), [l.key]: Number(e.currentTarget.value) })} />
              </label>;
            }}</For>
            <div class="sb-fit">
              <Show when={limited()} fallback={<span class="muted">Set what you will pay, wait and accept. The best model inside is ringed.</span>}>
                <Show when={fit()} fallback={<span>Nothing fits these limits.</span>}>{(row) =>
                  <span>Best fit <b>{label(row().p)}</b> <span class="muted">
                    {[own().includes("cost") && Number.isFinite(row().p.observation.resources?.cost) ? price(row().p.observation.resources.cost) : null,
                      own().includes("time") && Number.isFinite(row().p.observation.resources?.time) ? duration(row().p.observation.resources.time) : null,
                      fmtScore(row().p.observation.score, meta()?.format)].filter(Boolean).join(" · ")}</span></span>}</Show>
                <button type="button" class="sb-chip" onClick={() => setLimitAt({})}>Clear</button>
              </Show>
              <Show when={compare().length}>
                <span class="sb-pinned"><For each={compare()}>{(row: any) =>
                  <button type="button" class="sb-chip" title="Unpin" onClick={() => pin(row.p.entity.id)}><Swatch colour={familyColor(row.p.entity.family)} />{label(row.p)} · {fmtList(row)}</button>}</For></span>
              </Show>
            </div>
          </div>
        </Show>
        <Show when={view() === "plans"}>
          <div class="sb-limits">
            <For each={plans()}>{(plan) =>
              <span class="sb-plan-key"><Swatch colour={plan.colour} />{plan.name}<span class="muted">
                {plan.monthly ? ` $${plan.monthly}/mo · ${plan.multiple?.toFixed(1)}× its price` : ` $${Math.round(plan.value).toLocaleString()} of API value a week`}</span></span>}</For>
          </div>
        </Show>
      </section>
    </div>
  </Site>;
}

mount(() => <Models />);
