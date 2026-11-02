import { createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Hero, Section, Inset, Figure, Legend, Skeleton } from "../app/bits";
import { Plot, basic } from "../app/plot";
import { valueLines, hoverHTML } from "../app/charts";
import { loadReports, productColors, poll, type Reports } from "../app/data";
import { Meter, Swatch, Segmented } from "../conduit/ui/controls";
import { DashRow, DashEmpty, DashMore } from "../conduit/ui/dashboard";
// @ts-ignore
import { money, range, plural, shortDate, integer } from "../lib/format.js";
// @ts-ignore
import { WINDOWS, summaryCards, visibleEstimates, trendPoints, mixByWindow, modelTotals, sourceAccount, windowLabel } from "../lib/value.js";

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

  const providers = createMemo(() => [...new Set((reports()?.models?.models || []).map((row: any) => row.provider))] as string[]);
  const ratios = () => reports()?.current?.window_ratios || [];
  const divergences = () => reports()?.current?.divergences || [];

  return <Site page="subscriptions" title="Subscriptions">
    <Hero small kicker="Subscriptions" title="Value of one full limit"
      lede="The public API price of the usage one full limit window delivers. A figure appears once its window is complete and the estimator marks it confirmed; the smaller range beneath it is what it is settled to. Every completed window from the last 35 days is pooled, across every account on the plan." />

    <Show when={!reports.loading} fallback={<Skeleton height="10rem" />}>
      <Show when={cards().length} fallback={<Inset><DashEmpty>{reports()?.error ? `Subscription reports are unavailable: ${reports()!.error}` : "Nothing is being measured yet."}</DashEmpty></Inset>}>
        <div class="sb-grid two">
          <For each={cards()}>{(card: any) =>
            <Inset>
              <div class="sb-inset-head"><div><h3 style={{ display: "flex", "align-items": "center", gap: "var(--space-sm)" }}><Swatch colour={colours().get(card.name)} />{card.name}</h3></div></div>
              <div class="sb-limits">
                <For each={card.limits}>{(limit: any) =>
                  <div>
                    <Figure label={limit.window.label}
                      value={limit.state === "settled" ? money(limit.row.estimate_usd) : limit.state === "range" ? range(limit.row) : undefined}
                      meta={limit.state === "settled" ? <>{range(limit.row)} · {plural(limit.row.window_count, "window")} · {plural(limit.row.account_count, "account")}</>
                        : limit.state === "range" ? "range only, not yet settled" : undefined} />
                    <Show when={limit.live}>{(live: any) =>
                      <div class="sb-live"><b>in progress {money(live().estimate_usd)}</b> · {range(live())} · {Math.round(live().covered_quota_percent)}% measured</div>}</Show>
                  </div>}</For>
              </div>
            </Inset>}</For>
        </div>
      </Show>
    </Show>

    <Section title="Value over time" note="One line per product. Each point pools every completed, confirmed window that finished in that period, across every account holding the plan, so it measures the product rather than one subscriber.">
      <div class="sb-grid two">
        <For each={[{ title: "Weekly limit", data: weekly }, { title: "5-hour limit", data: five }]}>{(panel) =>
          <Inset title={panel.title} note={panel.data()?.note}>
            <Show when={panel.data()} fallback={<Show when={!reports.loading} fallback={<Skeleton height="260px" />}>
              <div class="sb-chart-empty">No completed high-confidence {panel.title.toLowerCase()} estimate yet.</div></Show>}>
              <Plot plotly={basic} data={() => panel.data()!.chart.traces} layout={() => panel.data()!.chart.layout} label={`${panel.title} value over time`} />
              <Legend items={panel.data()!.names.map((name) => ({ label: name, colour: colours().get(name) }))} />
            </Show>
          </Inset>}</For>
      </div>
    </Section>

    <Section title="Model mix" note="Token share of each model in recent weekly windows. Quota per dollar moves with the mix, so a change in value usually starts here.">
      <Show when={providers().length} fallback={<Inset><DashEmpty>No model mix reported yet.</DashEmpty></Inset>}>
        <div class="sb-grid two">
          <For each={providers()}>{(provider) => <MixPanel models={reports()?.models?.models || []} provider={provider} />}</For>
        </div>
      </Show>
    </Section>

    <Section title="Consistency checks" note="Two independent measurements of one allowance must agree. A disagreement means an assumption is wrong, or the provider changed something.">
      <div class="sb-grid two">
        <Inset title="Divergences">
          <Show when={divergences().length} fallback={<div class="sb-callout" data-tone="ok"><b>Every independent measurement agrees</b> within the 35% threshold.</div>}>
            <div class="sb-rows"><For each={divergences()}>{(row: any) =>
              <div class="sb-callout" data-tone="warn"><b>{row.provider} · {row.subject}</b> differs by {Number(row.difference_percent ?? row.difference ?? 0).toFixed(1)}%. {row.detail}</div>}</For></div>
          </Show>
        </Inset>
        <Inset title="Window ratios" note="How many full 5-hour limits fit in one weekly limit">
          <Show when={ratios().length} fallback={<DashEmpty>No ratio measured yet.</DashEmpty>}>
            <div class="sb-rows"><For each={ratios()}>{(row: any) =>
              <DashRow primary={row.provider === "codex" ? "ChatGPT" : row.provider === "claude" ? "Claude" : row.provider}
                context={row.account_id ? String(row.account_id).slice(0, 13) : "single source"}
                trailing={`${Number(row.per_long_window ?? 1 / row.ratio).toFixed(2)} ${row.short_window.replace("_", "-")} limits per ${row.long_window}`} />}</For></div>
          </Show>
        </Inset>
      </div>
    </Section>

    <Section title="Completed estimates" note="The individual windows behind the figures above, each with the account it was measured on. Lower-confidence and open windows are not listed."
      tools={<Segmented label="Limit" size="sm" value={limitFilter()} onChange={(v) => { setLimitFilter(v); setShown(20); }}
        options={[{ value: "weekly", label: "Weekly" }, { value: "five_hour", label: "5-hour" }, { value: "all", label: "All" }]} />}>
      <Inset flush>
        <div class="sb-table-wrap">
          <table class="sb-table">
            <Show when={estimates().length} fallback={<caption>No completed high-confidence estimate yet.</caption>}>
              <thead><tr><th>Completed</th><th>Product</th><th>Limit</th><th class="num">API equivalent</th><th class="num">Coverage</th><th>Source account</th></tr></thead>
              <tbody><For each={estimates().slice(0, shown())}>{(row: any) =>
                <tr>
                  <td class="data">{shortDate(row.completed_at)}</td>
                  <td><span style={{ display: "inline-flex", gap: "var(--space-sm)", "align-items": "center" }}><Swatch colour={colours().get(row.product)} />{row.product}</span></td>
                  <td>{windowLabel(row.window)}</td>
                  <td class="num">{money(row.estimate_usd)}</td>
                  <td class="num">{Number.isFinite(row.coverage_percent) ? `${Math.round(row.coverage_percent)}%` : "—"}</td>
                  <td class="data" title={row.account_id ?? undefined}>{sourceAccount(row)}</td>
                </tr>}</For></tbody>
            </Show>
          </table>
        </div>
        <Show when={estimates().length > shown()}>
          <div style={{ padding: "var(--space-sm)" }}><DashMore count={estimates().length - shown()} onClick={() => setShown(shown() + 40)}>Show more</DashMore></div>
        </Show>
      </Inset>
    </Section>

    <Section title="How the number is made">
      <div class="sb-grid three">
        <Inset tight><div class="sb-prose"><p><b>1 · Value the usage.</b> Token counts from the local agent logs, priced at the public API rate for each model and token class, summed inside the reset window.</p></div></Inset>
        <Inset tight><div class="sb-prose"><p><b>2 · Slope against the meter.</b> Every pair of quota readings where both quota and value advance implies a value for the whole window. Pairs under 2 points, stale, or spanning unseen usage are rejected.</p></div></Inset>
        <Inset tight><div class="sb-prose"><p><b>3 · Weighted median.</b> The figure is the quota-weighted median of the valid slopes, so one 50-point pair counts as much as fifty 1-point pairs.</p></div></Inset>
      </div>
    </Section>
  </Site>;
}

function MixPanel(props: { models: any[]; provider: string }) {
  const totals = createMemo(() => modelTotals(props.models, { provider: props.provider, windows: 6 }));
  const colourOf = createMemo(() => new Map(totals().map((row: any, i: number) => [row.model, MIX_COLOURS[i] ?? "var(--series-other)"])));
  const windows = createMemo(() => mixByWindow(props.models, { provider: props.provider, window: "weekly", limit: 8 }));
  return <Inset title={props.provider === "codex" ? "ChatGPT · Codex" : props.provider === "claude" ? "Claude · Claude Code" : props.provider} note="Recent weekly windows, newest last">
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
