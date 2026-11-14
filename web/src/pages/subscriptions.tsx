import { createEffect, createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Section, Inset, Figure, Legend, Skeleton } from "../app/bits";
import { Plot, basic } from "../app/plot";
import { EChart, type ChartApi } from "../app/echart";
import { optionPlans } from "../app/modelchart";
import { Picker, type PickGroup } from "../app/picker";
import { valueLines, hoverHTML } from "../app/charts";
import { loadFrontier, loadReports, personal, productColors, poll, PROVIDER_NAME, type Reports } from "../app/data";
import { measuredPlans, planSeries } from "../app/plans";
import { Meter, Swatch, Segmented } from "../conduit/ui/controls";
import { DashRow, DashEmpty, DashMore } from "../conduit/ui/dashboard";
import { ExternalLinkIcon } from "lucide-solid";
// @ts-ignore
import { money, range, plural, shortDate, integer } from "../lib/format.js";
// @ts-ignore
import { planPrice, valueMultiple } from "../lib/plans.js";
// @ts-ignore
import { headline, summaryCards, visibleEstimates, trendPoints, modelTotals, windowLabel } from "../lib/value.js";
// @ts-ignore
import { points as metricPoints, matchModel } from "../lib/frontier.js";

/*
 * Subscriptions, the home page: what each plan is worth, side by side. The figure strip compares the plans at a
 * glance. Below, the plans' limits, model mix and checks sit in one column, and the other column holds views
 * composed from the Models page and seen through the plans: value over time, and what each plan gets done on a
 * benchmark, per week or per dollar. Pointing at a plan anywhere brings its line forward in the view. Each plan view
 * opens in Models, where it can be taken further.
 */

const MIX_COLOURS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)", "var(--series-7)", "var(--series-8)"];
type View = "week" | "dollar" | "value";
const VIEWS: Array<{ id: View; title: string; read: string }> = [
  { id: "week", title: "Tasks a week", read: "How many benchmark tasks one weekly limit pays for, at public API prices, against the score. Each line is a plan's best trade-offs: up is a better model, right is more tasks." },
  { id: "dollar", title: "Tasks per dollar", read: "The same tasks over the week's share of the plan's price, so plans of different sizes compare on what the money buys." },
  { id: "value", title: "Value over time", read: "The API-equivalent value of one full limit, week by week, pooled across accounts." },
];
const params = new URLSearchParams(location.search);

function Subscriptions() {
  const [reports, { refetch }] = createResource<Reports>(loadReports);
  const [index] = createResource(loadFrontier);
  // The reports poll with health: a push lands every half hour.
  poll(() => refetch());
  const history = () => reports()?.history;
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const cards = createMemo(() => {
    const h = history() || {};
    return summaryCards(h.products || [], h.windows || [], h.products_likely || [], h.products_open || []);
  });
  const colours = createMemo(() => productColors(cards().map((c: any) => ({ name: c.name, provider: c.provider }))));
  const plans = createMemo(() => measuredPlans(me()?.products ?? []));
  // Plans in one list, by provider and then by weekly value, so like sits beside like.
  const ordered = createMemo(() => {
    const weekly = (card: any) => card.limits.find((l: any) => l.window.key === "weekly")?.row?.estimate_usd ?? Infinity;
    return [...cards()].sort((a: any, b: any) => (PROVIDER_NAME[a.provider] ?? a.provider ?? "").localeCompare(PROVIDER_NAME[b.provider] ?? b.provider ?? "") || weekly(a) - weekly(b));
  });

  // ---- the view
  const [view, setView] = createSignal<View>((VIEWS.find((v) => v.id === params.get("view"))?.id) ?? "week");
  const [lens, setLens] = createSignal(params.get("lens") || "aa-intelligence");
  const [trendWindow, setTrendWindow] = createSignal<"weekly" | "five_hour">("weekly");
  const [focus, setFocus] = createSignal<string | null>(null);
  const [api, setApi] = createSignal<ChartApi | null>(null);
  createEffect(() => api()?.focus(focus()));
  createEffect(() => {
    const q = new URLSearchParams({ ...(view() !== "week" ? { view: view() } : {}), ...(lens() !== "aa-intelligence" ? { lens: lens() } : {}) });
    window.history.replaceState(null, "", `${location.pathname}${q.size ? `?${q}` : ""}`);
  });
  const asked = () => VIEWS.find((v) => v.id === view())!;
  const metric = () => index()?.metrics.get(lens()) as any;

  // Benchmarks a plan view can use: model benchmarks with a cost per task for at least one covered model.
  const lensGroups = createMemo<PickGroup[]>(() => {
    const ix = index();
    if (!ix) return [];
    const groups = new Map<string, any[]>();
    for (const m of ix.metrics.values() as Iterable<any>) {
      if (m.entity_type !== "model") continue;
      const n = (metricPoints(ix, m.id) as any[]).filter((p) => p.observation.resources?.cost > 0).length;
      if (n < 2) continue;
      (groups.get(m.group) ?? groups.set(m.group, []).get(m.group)!).push({ value: m.id, label: m.label, meta: String(n) });
    }
    return [...groups.entries()].map(([title, items]) => ({ title, items }));
  });
  const series = createMemo(() => index() ? planSeries(plans(), metricPoints(index()!, lens()), { perDollar: view() === "dollar", metric: metric() }) : []);
  const planOption = (size: { width: number; height: number }) => optionPlans({
    plans: series(), size, labels: size.width > 640, perDollar: view() === "dollar",
    score: { title: metric()?.label ?? "Score", log: false, percent: metric()?.format === "percent", higherBetter: true },
  });

  const lines = (window: string) => createMemo(() => {
    const h = history();
    if (!h) return null;
    const pts = trendPoints(h.product_series).filter((row: any) => row.window === window);
    if (!pts.length) return null;
    const names = [...new Set(pts.map((row: any) => row.product))] as string[];
    const chart = valueLines(names.map((name) => ({
      name, colour: colours().get(name) ?? "var(--series-1)",
      points: pts.filter((row: any) => row.product === name).map((row: any) => ({ x: row.completed_ms, y: row.estimate_usd,
        tip: hoverHTML(name, [["Period", shortDate(row.completed_at)], ["Value", money(row.estimate_usd)], ["Pooled", `${plural(row.window_count, "window")} · ${plural(row.account_count, "account")}`]]) })),
      open: null,
    })), { height: 260 });
    const count = pts.reduce((sum: number, row: any) => sum + (row.window_count || 1), 0);
    return { chart, names, note: `${plural(count, "confirmed window")} over ${plural(pts.length, "point")}` };
  });
  const weekly = lines("weekly");
  const five = lines("five_hour");
  const activeTrend = () => (trendWindow() === "weekly" ? weekly() : five());
  const modelsHref = () => `/models/?view=plans${view() === "dollar" ? "&basis=dollar" : ""}&lens=${encodeURIComponent(lens())}`;

  // ---- the person's top model, matched to the benchmark bundle when it can be
  const topModel = createMemo(() => {
    const top = modelTotals(reports()?.models?.models || [], { windows: 6 })[0];
    if (!top) return null;
    const entity = index() ? matchModel(top.model, index()!.entities.model) : null;
    return { name: entity?.label ?? top.model, share: top.share, matched: !!entity };
  });

  // ---- checks
  const providers = createMemo(() => [...new Set((reports()?.models?.models || []).map((row: any) => row.provider))] as string[]);
  const ratios = () => reports()?.current?.window_ratios || [];
  // One figure per provider: the median across the accounts that report it, so no single account is shown.
  const pooledRatios = createMemo(() => {
    const by = new Map<string, number[]>();
    for (const row of ratios()) {
      const value = Number(row.per_long_window ?? 1 / row.ratio);
      if (Number.isFinite(value)) (by.get(row.provider) ?? by.set(row.provider, []).get(row.provider)!).push(value);
    }
    return [...by.entries()].map(([provider, values]) => {
      const sorted = values.sort((x, y) => x - y), mid = Math.floor(sorted.length / 2);
      return { provider, accounts: sorted.length, value: sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2 };
    });
  });
  const divergences = () => reports()?.current?.divergences || [];

  const [limitFilter, setLimitFilter] = createSignal<"weekly" | "five_hour" | "all">("weekly");
  const [shown, setShown] = createSignal(20);
  const estimates = createMemo(() => visibleEstimates(history()?.windows || []).slice().reverse()
    .filter((row: any) => limitFilter() === "all" || row.window === limitFilter()));
  const point = (name: string) => ({ onMouseEnter: () => setFocus(name), onMouseLeave: () => setFocus(null), onFocusIn: () => setFocus(name), onFocusOut: () => setFocus(null) });

  return <Site page="subscriptions" title="Subscriptions" wide>

    <Inset>
      <div class="sb-grid figures">
        <Show when={!reports.loading} fallback={<Skeleton height="4.5rem" />}>
          <For each={me()?.products || []} fallback={<Figure label="Subscriptions" empty={reports()?.error ? "Unavailable" : "None yet"} />}>{(item) =>
            <div class="sb-figure-point" data-focus={focus() === item.card.name ? "" : undefined} tabindex="0" {...point(item.card.name)}>
              <Figure label={item.card.name} colour={colours().get(item.card.name)} value={money(item.headline.value)} unit="/ week"
                meta={<>{item.headline.kind === "settled" ? <>≈ <b>{money(item.headline.value * 52 / 12)}</b> / month{valueMultiple(item.card.name, item.headline.value) ? <> · <b>{valueMultiple(item.card.name, item.headline.value).toFixed(1)}×</b> its ${planPrice(item.card.name).monthly} price</> : null}</>
                  : item.headline.kind === "live" ? <>in progress · {Math.round(item.headline.row.covered_quota_percent)}% measured</> : <>range only, not yet settled</>}</>} />
            </div>}</For>
        </Show>
        <Figure label="Most-used model" value={topModel() ? `${Math.round(topModel()!.share * 100)}%` : undefined} unit="of tokens"
          empty="None yet" meta={topModel() ? <><b>{topModel()!.name}</b>{topModel()!.matched ? "" : " · not benchmarked"}</> : undefined} />
      </div>
    </Inset>

    <div class="sb-home">
      <div class="sb-home-plans">
        <Inset title="Limits" note="API-equivalent value of one full limit">
          <Show when={!reports.loading} fallback={<Skeleton height="8rem" />}>
            <Show when={cards().length} fallback={<DashEmpty>{reports()?.error ? `Subscription data unavailable: ${reports()!.error}` : "Nothing measured yet"}</DashEmpty>}>
              <table class="sb-plan-table">
                <thead><tr><th>Plan</th><th class="num">Weekly</th><th class="num">5-hour</th></tr></thead>
                <tbody><For each={ordered()}>{(card: any) => {
                  const cell = (key: string) => {
                    const limit = card.limits.find((l: any) => l.window.key === key);
                    if (!limit) return <span class="muted">—</span>;
                    return <>
                      <b>{limit.state === "settled" ? money(limit.row.estimate_usd) : limit.state === "range" ? range(limit.row) : "—"}</b>
                      <small title={limit.state === "settled" ? `${range(limit.row)} · ${plural(limit.row.window_count, "window")} · ${plural(limit.row.account_count, "account")}` : undefined}>
                        {limit.state === "settled" ? plural(limit.row.window_count, "window") : limit.state === "range" ? "unsettled" : ""}</small>
                      <Show when={limit.live}>{(live: any) => <small class="sb-live-note" title={`${range(live())} · ${Math.round(live().covered_quota_percent)}% of the limit measured`}>now {money(live().estimate_usd)}</small>}</Show>
                    </>;
                  };
                  const m = () => headline(card) && valueMultiple(card.name, headline(card).value);
                  return <tr data-focus={focus() === card.name ? "" : undefined} tabindex="0" {...point(card.name)}>
                    <th scope="row"><span class="sb-plan-name"><Swatch colour={colours().get(card.name)} />{card.name}</span>
                      <small>{planPrice(card.name) ? `$${planPrice(card.name).monthly}/mo` : "scoped limit"}{m() ? ` · ${m().toFixed(1)}×` : ""}</small></th>
                    <td class="num">{cell("weekly")}</td>
                    <td class="num">{cell("five_hour")}</td>
                  </tr>;
                }}</For></tbody>
              </table>
            </Show>
          </Show>
        </Inset>

        <For each={providers()}>{(provider) => <MixPanel models={reports()?.models?.models || []} provider={provider} />}</For>

        <Inset title="Checks">
          <div class="sb-rows">
            <Show when={divergences().length} fallback={<div class="sb-callout" data-tone="ok"><b>All sources agree</b> (within 35%)</div>}>
              <For each={divergences()}>{(row: any) =>
                <div class="sb-callout" data-tone="warn"><b>{row.provider} · {row.subject}</b> differs by {Number(row.difference_percent ?? row.difference ?? 0).toFixed(1)}%. {row.detail}</div>}</For>
            </Show>
            <For each={pooledRatios()}>{(row) =>
              <DashRow primary={`${row.provider === "codex" ? "ChatGPT" : row.provider === "claude" ? "Claude" : row.provider} · 5-hour limits per week`}
                context={`median of ${row.accounts} ${row.accounts === 1 ? "account" : "accounts"}`} trailing={row.value.toFixed(2)} />}</For>
          </div>
        </Inset>
      </div>

      <section class="sb-home-view sb-canvas-main" aria-label="View">
        <nav class="sb-questions" aria-label="Views">
          <For each={VIEWS}>{(v) => <button type="button" class="sb-question" aria-pressed={view() === v.id} onClick={() => setView(v.id)}>{v.title}</button>}</For>
          <span class="grow" />
          <Show when={view() !== "value"} fallback={<Segmented label="Limit" size="sm" value={trendWindow()} onChange={setTrendWindow}
            options={[{ value: "weekly", label: "Weekly" }, { value: "five_hour", label: "5-hour" }]} />}>
            <Picker value={lens()} groups={lensGroups()} onChoose={setLens} placeholder="Find a benchmark" title="Benchmark" width="lg" />
            <a class="sb-chip" href={modelsHref()} title="Open this view in Models to add measures, limits and filters"><ExternalLinkIcon />Open in Models</a>
          </Show>
        </nav>
        <div class="sb-canvas-title">
          <h2>{view() === "value" ? `Value over time · ${trendWindow() === "weekly" ? "weekly limit" : "5-hour limit"}` : `${metric()?.label ?? ""} · ${asked().title.toLowerCase()}`}</h2>
          <p>{asked().read}{view() === "value" && activeTrend() ? ` ${activeTrend()!.note}.` : ""}</p>
        </div>
        <div class="sb-canvas-chart">
          <Show when={view() !== "value"} fallback={
            <Show when={activeTrend()} fallback={<Show when={!reports.loading} fallback={<Skeleton />}><div class="sb-chart-empty" style={{ height: "100%" }}>No confirmed windows yet</div></Show>}>
              <Plot plotly={basic} data={() => activeTrend()!.chart.traces} layout={() => ({ ...activeTrend()!.chart.layout, height: undefined, autosize: true })} label="Value over time" />
            </Show>}>
            <Show when={index() && !reports.loading} fallback={<Skeleton />}>
              <Show when={series().length} fallback={<div class="sb-chart-empty" style={{ height: "100%" }}>No plan covers a model with a cost per task on this benchmark. Pick another.</div>}>
                <EChart option={planOption} api={setApi} label="What each plan gets done" />
              </Show>
            </Show>
          </Show>
        </div>
        <Legend items={plans().map((plan) => ({ label: plan.name, colour: plan.colour, kind: "line" as const }))} />
      </section>
    </div>

    <Section title="Windows"
      tools={<Segmented label="Limit" size="sm" value={limitFilter()} onChange={(v) => { setLimitFilter(v); setShown(20); }}
        options={[{ value: "weekly", label: "Weekly" }, { value: "five_hour", label: "5-hour" }, { value: "all", label: "All" }]} />}>
      <Inset flush>
        <div class="sb-table-wrap">
          <table class="sb-table">
            <Show when={estimates().length} fallback={<caption>No confirmed windows yet</caption>}>
              <thead><tr><th>Completed</th><th>Product</th><th>Limit</th><th class="num">API equivalent</th><th class="num">Coverage</th></tr></thead>
              <tbody><For each={estimates().slice(0, shown())}>{(row: any) =>
                <tr>
                  <td class="data">{shortDate(row.completed_at)}</td>
                  <td><span style={{ display: "inline-flex", gap: "var(--space-sm)", "align-items": "center" }}><Swatch colour={colours().get(row.product)} />{row.product}</span></td>
                  <td>{windowLabel(row.window)}</td>
                  <td class="num">{money(row.estimate_usd)}</td>
                  <td class="num">{Number.isFinite(row.coverage_percent) ? `${Math.round(row.coverage_percent)}%` : "—"}</td>
                </tr>}</For></tbody>
            </Show>
          </table>
        </div>
        <Show when={estimates().length > shown()}>
          <div style={{ padding: "var(--space-sm)" }}><DashMore count={estimates().length - shown()} onClick={() => setShown(shown() + 40)}>Show more</DashMore></div>
        </Show>
      </Inset>
    </Section>

  </Site>;
}

// The models a provider's plans were used on, over the last six weekly windows: one bar, shares in the key.
function MixPanel(props: { models: any[]; provider: string }) {
  // Models under half a percent of the tokens are left out of the key; they would not show in the bar.
  const totals = createMemo(() => modelTotals(props.models, { provider: props.provider, windows: 6 }).filter((row: any) => row.share >= 0.005));
  const colourOf = createMemo(() => new Map(totals().map((row: any, i: number) => [row.model, MIX_COLOURS[i] ?? "var(--series-other)"])));
  return <Show when={totals().length}>
    <Inset title={props.provider === "codex" ? "Model mix · Codex" : props.provider === "claude" ? "Model mix · Claude Code" : "Model mix"} note="Tokens over the last six weeks">
      <Meter parts={totals().map((row: any) => ({ value: row.tokens ?? row.share, colour: colourOf().get(row.model) ?? "var(--series-other)" }))} />
      <Legend items={totals().map((row: any) => ({ label: `${row.model} · ${Math.round(row.share * 100)}%`, colour: colourOf().get(row.model), kind: "dot" as const }))} />
    </Inset>
  </Show>;
}

mount(() => <Subscriptions />);
