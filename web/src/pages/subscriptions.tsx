import { createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Section, Inset, Figure, Legend, Skeleton } from "../app/bits";
import { Plot, basic } from "../app/plot";
import { valueLines, hoverHTML } from "../app/charts";
import { loadReports, productColors, productDashes, PROVIDER_NAME, poll, type Reports } from "../app/data";
import { Meter, Swatch, Segmented } from "../conduit/ui/controls";
import { DashRow, DashEmpty, DashMore } from "../conduit/ui/dashboard";
// @ts-ignore
import { money, range, plural, shortDate, integer } from "../lib/format.js";
// @ts-ignore
import { planPrice, valueMultiple } from "../lib/plans.js";
// @ts-ignore
import { WINDOWS, headline, summaryCards, visibleEstimates, trendPoints, mixByWindow, modelTotals, windowLabel } from "../lib/value.js";

const MIX_COLOURS = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)", "var(--series-7)", "var(--series-8)"];

function Subscriptions() {
  const [reports, { refetch }] = createResource<Reports>(loadReports);
  // The reports poll with health: a push lands every half hour.
  poll(() => refetch());
  const history = () => reports()?.history;
  const cards = createMemo(() => {
    const h = history() || {};
    return summaryCards(h.products || [], h.windows || [], h.products_likely || [], h.products_open || []);
  });
  const colours = createMemo(() => productColors(cards().map((c: any) => ({ name: c.name, provider: c.provider }))));
  const dashes = createMemo(() => productDashes(cards().map((c: any) => ({ name: c.name, provider: c.provider }))));
  // One block per provider, its plans side by side, cheapest first, so like sits beside like.
  const groups = createMemo(() => {
    const byProvider = new Map<string, any[]>();
    for (const card of cards()) {
      const key = card.provider ?? "other";
      if (!byProvider.has(key)) byProvider.set(key, []);
      byProvider.get(key)!.push(card);
    }
    const weekly = (card: any) => card.limits.find((l: any) => l.window.key === "weekly")?.row?.estimate_usd ?? Infinity;
    return [...byProvider.entries()]
      .map(([provider, list]) => ({ provider, name: PROVIDER_NAME[provider] ?? provider, cards: list.sort((a, b) => weekly(a) - weekly(b)) }))
      .sort((a, b) => a.name.localeCompare(b.name));
  });
  const [limitFilter, setLimitFilter] = createSignal<"weekly" | "five_hour" | "all">("weekly");
  const [shown, setShown] = createSignal(20);
  const estimates = createMemo(() => visibleEstimates(history()?.windows || []).slice().reverse()
    .filter((row: any) => limitFilter() === "all" || row.window === limitFilter()));

  const lines = (window: string) => createMemo(() => {
    const h = history();
    if (!h) return null;
    const pts = trendPoints(h.product_series).filter((row: any) => row.window === window);
    if (!pts.length) return null;
    const names = [...new Set(pts.map((row: any) => row.product))] as string[];
    const chart = valueLines(names.map((name) => ({
      name, colour: colours().get(name) ?? "var(--series-1)", dash: dashes().get(name),
      points: pts.filter((row: any) => row.product === name).map((row: any) => ({ x: row.completed_ms, y: row.estimate_usd,
        tip: hoverHTML(name, [["Period", shortDate(row.completed_at)], ["Value", money(row.estimate_usd)], ["Pooled", `${plural(row.window_count, "window")} · ${plural(row.account_count, "account")}`]]) })),
      open: null,
    })), { height: 260 });
    const count = pts.reduce((sum: number, row: any) => sum + (row.window_count || 1), 0);
    return { chart, names, note: `${plural(count, "confirmed window")} over ${plural(pts.length, "point")}` };
  });
  const weekly = lines("weekly");
  const five = lines("five_hour");
  const [trendWindow, setTrendWindow] = createSignal<"weekly" | "five_hour">("weekly");
  const activeTrend = () => (trendWindow() === "weekly" ? weekly() : five());

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

  return <Site page="subscriptions" title="Subscriptions">

    <Show when={!reports.loading} fallback={<Skeleton height="10rem" />}>
      <Show when={cards().length} fallback={<Inset><DashEmpty>{reports()?.error ? `Subscription data unavailable: ${reports()!.error}` : "Nothing measured yet"}</DashEmpty></Inset>}>
        <For each={groups()}>{(group) =>
          <section class="sb-provider" aria-label={group.name}>
            <h2><Swatch colour={colours().get(group.cards[0].name)} />{group.name}</h2>
            <div class="sb-provider-grid">
              <For each={group.cards}>{(card: any) => <ProductCard card={card} colour={colours().get(card.name)} dash={dashes().get(card.name)} />}</For>
            </div>
            <Show when={providers().includes(group.provider)}>
              <div style={{ "margin-top": "var(--space-lg)" }}><MixPanel models={reports()?.models?.models || []} provider={group.provider} /></div>
            </Show>
          </section>}</For>
      </Show>
    </Show>

    <Section title="Value over time"
      tools={<Segmented label="Limit" size="sm" value={trendWindow()} onChange={setTrendWindow}
        options={[{ value: "weekly", label: "Weekly" }, { value: "five_hour", label: "5-hour" }]} />}>
      <Inset title={trendWindow() === "weekly" ? "Weekly limit" : "5-hour limit"} aside={<span class="sb-tag" data-tone="quiet">{activeTrend()?.note ?? ""}</span>}>
        <div class="sb-canvas-chart fixed">
          <Show when={activeTrend()} fallback={<Show when={!reports.loading} fallback={<Skeleton />}>
            <div class="sb-chart-empty" style={{ height: "100%" }}>No confirmed windows yet</div></Show>}>
            <Plot plotly={basic} data={() => activeTrend()!.chart.traces} layout={() => ({ ...activeTrend()!.chart.layout, height: undefined, autosize: true })} label="Value over time" />
          </Show>
        </div>
        <Show when={activeTrend()}><Legend items={activeTrend()!.names.map((name) => ({ label: name, colour: colours().get(name), kind: legendKind(dashes().get(name)) }))} /></Show>
      </Inset>
    </Section>

    <Section title="Checks">
      <div class="sb-grid two">
        <Inset title="Divergences">
          <Show when={divergences().length} fallback={<div class="sb-callout" data-tone="ok"><b>All agree</b> (within 35%)</div>}>
            <div class="sb-rows"><For each={divergences()}>{(row: any) =>
              <div class="sb-callout" data-tone="warn"><b>{row.provider} · {row.subject}</b> differs by {Number(row.difference_percent ?? row.difference ?? 0).toFixed(1)}%. {row.detail}</div>}</For></div>
          </Show>
        </Inset>
        <Inset title="5-hour limits per week">
          <Show when={ratios().length} fallback={<DashEmpty>None yet</DashEmpty>}>
            <div class="sb-rows"><For each={pooledRatios()}>{(row) =>
              <DashRow primary={row.provider === "codex" ? "ChatGPT" : row.provider === "claude" ? "Claude" : row.provider}
                context={`median of ${row.accounts} ${row.accounts === 1 ? "account" : "accounts"}`}
                trailing={row.value.toFixed(2)} />}</For></div>
          </Show>
        </Inset>
      </div>
    </Section>

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

const legendKind = (dash?: string) => (dash === "dash" ? "dash" : dash === "dot" ? "dotted" : "line") as "dash" | "dotted" | "line";

function ProductCard(props: { card: any; colour?: string; dash?: string }) {
  return <Inset>
    <div class="sb-inset-head"><div><h3 style={{ display: "flex", "align-items": "center", gap: "var(--space-sm)" }}>
      <Legend items={[{ label: "", colour: props.colour, kind: legendKind(props.dash) }]} />{props.card.name}</h3>
      <Show when={planPrice(props.card.name)}>{(cost) => <p>${cost().monthly} / month list price</p>}</Show></div>
      <Show when={headline(props.card) && valueMultiple(props.card.name, headline(props.card).value)}>{(m) =>
        <span class="sb-tag" title="Measured API value of a week's use over the week's share of the price">{m().toFixed(1)}× its price</span>}</Show></div>
    <div class="sb-limits">
      <For each={props.card.limits}>{(limit: any) =>
        <div>
          <Figure label={limit.window.label}
            value={limit.state === "settled" ? money(limit.row.estimate_usd) : limit.state === "range" ? range(limit.row) : undefined}
            meta={limit.state === "settled" ? <>{range(limit.row)} · {plural(limit.row.window_count, "window")} · {plural(limit.row.account_count, "account")}</>
              : limit.state === "range" ? "unsettled" : undefined} />
          <Show when={limit.live}>{(live: any) =>
            <div class="sb-live"><b>in progress {money(live().estimate_usd)}</b> · {range(live())} · {Math.round(live().covered_quota_percent)}% measured</div>}</Show>
        </div>}</For>
    </div>
  </Inset>;
}

function MixPanel(props: { models: any[]; provider: string }) {
  const totals = createMemo(() => modelTotals(props.models, { provider: props.provider, windows: 6 }));
  const colourOf = createMemo(() => new Map(totals().map((row: any, i: number) => [row.model, MIX_COLOURS[i] ?? "var(--series-other)"])));
  const windows = createMemo(() => mixByWindow(props.models, { provider: props.provider, window: "weekly", limit: 8 }));
  return <Inset title={props.provider === "codex" ? "Model mix · Codex" : props.provider === "claude" ? "Model mix · Claude Code" : "Model mix"}>
    <div class="sb-rows" style={{ gap: "var(--space-md)" }}>
      <For each={windows()}>{(w: any) =>
        <div style={{ display: "grid", "grid-template-columns": "5.5rem 1fr 4.5rem", "align-items": "center", gap: "var(--space-lg)" }}>
          <span class="sb-note" style={{ "font-family": "var(--font-data)" }}>{shortDate(w.resets_at).replace(/, \d{4}$/, "")}</span>
          <Meter parts={w.parts.map((part: any) => ({ value: part.tokens, colour: colourOf().get(part.model) ?? "var(--series-other)" }))} />
          <span class="sb-note" style={{ "font-family": "var(--font-data)", "text-align": "right" }}>{integer(w.total)}</span>
        </div>}</For>
    </div>
    <Legend items={totals().map((row: any) => ({ label: `${row.model} · ${Math.round(row.share * 100)}%`, colour: colourOf().get(row.model), kind: "dot" as const }))} />
  </Inset>;
}

mount(() => <Subscriptions />);
