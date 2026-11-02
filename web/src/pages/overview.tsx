import { createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Section, Inset, Figure, Legend, Bars, Skeleton, link } from "../app/bits";
import { Plot, basic } from "../app/plot";
import { frontierScatter, valueLines, hoverHTML } from "../app/charts";
import { joinedScatter } from "../app/points";
import { loadFrontier, loadReports, personal, productColors, type Reports } from "../app/data";
import { Button, Segmented } from "../conduit/ui/controls";
import { Select } from "../conduit/menus";
// @ts-ignore
import { money, integer, price, score as fmtScore, plural, shortDate } from "../lib/format.js";
// @ts-ignore
import { trendPoints, modelTotals } from "../lib/value.js";
// @ts-ignore
import { familyColor, tasksPerEntitlement, matchModel } from "../lib/frontier.js";

const BUY_METRICS = ["deepswe-pass1", "aa-eval-terminalbench-4-0", "aa-eval-frontiercode-2-0", "aa-intelligence", "frontiercode-score"];
// The chart lens: the retired per-model Coding Index where an older bundle still has it, else the Intelligence Index.
const MINI_METRICS = ["aa-intelligence", "aa-coding"];

function Overview() {
  const [index] = createResource(loadFrontier);
  const [reports] = createResource<Reports>(loadReports);
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const colours = createMemo(() => productColors((me()?.products || []).map((p) => ({ name: p.card.name, provider: p.card.provider }))));

  const [product, setProduct] = createSignal<string | null>(null);
  const chosen = createMemo(() => {
    const list = me()?.products || [];
    return list.find((p) => p.card.name === product()) ?? list[0] ?? null;
  });
  const [buyMetric, setBuyMetric] = createSignal("deepswe-pass1");

  const trend = createMemo(() => {
    const h = reports()?.history;
    if (!h) return null;
    const pts = trendPoints(h.product_series).filter((row: any) => row.window === "weekly");
    const names = [...new Set(pts.map((row: any) => row.product))] as string[];
    return valueLines(names.map((name) => ({
      name, colour: colours().get(name) ?? "var(--series-1)",
      points: pts.filter((row: any) => row.product === name).map((row: any) => ({ x: row.completed_ms, y: row.estimate_usd,
        tip: hoverHTML(name, [["Week of", shortDate(row.completed_at)], ["Weekly value", money(row.estimate_usd)], ["Pooled", `${plural(row.window_count, "window")} · ${plural(row.account_count, "account")}`]]) })),
      open: (() => {
        const live = (h.products_open || []).find((row: any) => row.product === name && row.window === "weekly");
        return live ? { x: Date.now(), y: live.estimate_usd, tip: hoverHTML(name + " · in progress", [["So far", money(live.estimate_usd)], ["Range", `${money(live.lower_usd)}–${money(live.upper_usd)}`], ["Measured", `${Math.round(live.covered_quota_percent)}% of quota`]]) } : null;
      })(),
    })), { height: 300 });
  });

  const buys = createMemo(() => {
    const ix = index(), pick = chosen();
    if (!ix || !pick) return [];
    const value = pick.headline.value;
    const joined = joinedScatter(ix, buyMetric(), "cost", { mine: me()?.mine });
    const keep = new Map<string, any>();
    for (const p of joined.frontier) keep.set(p.entity.id + p.observation.variant, p);
    for (const p of joined.comparable) if (me()?.mine.has(p.entity.id)) keep.set(p.entity.id + p.observation.variant, p);
    return [...keep.values()]
      .map((p) => ({ p, per: tasksPerEntitlement(value, p.observation.resources?.cost) }))
      .filter((r) => r.per)
      .sort((a, b) => b.p.observation.score - a.p.observation.score)
      .slice(0, 14)
      .map(({ p, per }) => ({
        key: p.entity.id + (p.observation.variant || ""), label: p.entity.label + (p.observation.variant && !p.entity.label.includes(p.observation.variant) ? ` (${p.observation.variant})` : ""),
        colour: familyColor(p.entity.family), value: per!, display: `${integer(per)} tasks`,
        sub: `${fmtScore(p.observation.score, joined.metric.format)} · ${price(p.observation.resources.cost)}/task`,
        tag: me()?.mine.has(p.entity.id) ? "yours" : undefined,
      }));
  });

  const buyOptions = createMemo(() => (index() ? BUY_METRICS.filter((id) => index()!.metrics.has(id)).map((id) => index()!.metrics.get(id)) : []));

  const mini = createMemo(() => {
    const ix = index();
    if (!ix) return null;
    const id = MINI_METRICS.find((m) => ix.metrics.has(m)) ?? "aa-intelligence";
    const joined = joinedScatter(ix, id, "cost", { mine: me()?.mine, weeklyValue: chosen()?.headline.value });
    return frontierScatter(joined.scatter, { xLog: true, xTitle: `Cost per ${joined.metric.label} task (USD)`, yTitle: joined.metric.label, yFormat: "", groups: true, height: innerWidth > 700 ? 460 : 380, gutter: innerWidth > 700 });
  });

  // The person's top model by tokens, matched to the benchmark bundle when it can be. A model newer than the
  // benchmark snapshot is still named, and says so, rather than letting a smaller matched model stand in for it.
  const topModel = createMemo(() => {
    const top = modelTotals(reports()?.models?.models || [], { windows: 6 })[0];
    if (!top) return null;
    const entity = index() ? matchModel(top.model, index()!.entities.model) : null;
    return { name: entity?.label ?? top.model, share: top.share, matched: !!entity };
  });

  return <Site page="overview" title="Overview">

    <Inset>
      <div class="sb-grid figures">
        <Show when={!reports.loading} fallback={<Skeleton height="4.5rem" />}>
          <For each={me()?.products || []} fallback={<Figure label="Subscriptions" empty={reports()?.error ? "Unavailable" : "None yet"} />}>{(item) =>
            <Figure label={item.card.name} colour={colours().get(item.card.name)} value={money(item.headline.value)} unit="/ week"
              meta={<>{item.headline.kind === "settled" ? <>≈ <b>{money(item.headline.value * 52 / 12)}</b> / month · {plural(item.headline.row.window_count, "window")}</>
                : item.headline.kind === "live" ? <>in progress · {Math.round(item.headline.row.covered_quota_percent)}% measured</> : <>range only, not yet settled</>}</>} />}</For>
        </Show>
        <Figure label="Models benchmarked" value={index() ? String(index()!.bundle.counts.models) : "—"}
          meta={index() ? <>{index()!.bundle.counts.agents} agent configs · {index()!.bundle.counts.metrics} lenses</> : "loading…"} />
        <Figure label="Your most-used model" value={topModel() ? `${Math.round(topModel()!.share * 100)}%` : undefined} unit="of tokens"
          empty="None yet" meta={topModel() ? <><b>{topModel()!.name}</b>{topModel()!.matched ? "" : " · not benchmarked"}</> : undefined} />
      </div>
    </Inset>

    <Section title="Weekly value"
      tools={<Button variant="ghost" size="sm" onClick={link("/subscriptions/")}>All limits</Button>}>
      <Inset>
        <Show when={trend()} fallback={<Show when={!reports.loading} fallback={<Skeleton height="300px" />}><div class="sb-chart-empty">Subscription reports are unavailable right now.</div></Show>}>
          <Plot plotly={basic} data={() => trend()!.traces} layout={() => trend()!.layout} label="Weekly subscription value over time" />
          <Legend items={[...colours().entries()].map(([label, colour]) => ({ label, colour }))} />
        </Show>
      </Inset>
    </Section>

    <Section title="Tasks per weekly limit"
      tools={<>
        <Show when={(me()?.products.length ?? 0) > 1}>
          <Segmented label="Subscription" size="sm" value={chosen()?.card.name ?? ""} onChange={setProduct}
            options={(me()?.products || []).map((p) => ({ value: p.card.name, label: p.card.name }))} />
        </Show>
        <Select aria-label="Benchmark" value={buyMetric()} onChange={(event) => setBuyMetric(event.currentTarget.value)} bare={false}>
          <For each={buyOptions()}>{(metric: any) => <option value={metric.id}>{metric.label}</option>}</For>
        </Select>
      </>}>
      <div class="sb-grid">
        <Inset title={chosen() ? `${chosen()!.card.name} · ${money(chosen()!.headline.value)} per week` : "Tasks per weekly limit"}>
          <Show when={index() && !reports.loading} fallback={<Skeleton height="18rem" />}>
            <Bars rows={buys()} empty={chosen() ? "No cost data for this benchmark" : "No weekly value yet"} />
          </Show>
        </Inset>
      </div>
    </Section>

    <Section title="Score vs cost"
      tools={<Button variant="ghost" size="sm" onClick={link("/frontier/")}>Open explorer</Button>}>
      <Inset>
        <Show when={mini()} fallback={<Skeleton height="420px" />}>
          <Plot plotly={basic} data={() => mini()!.traces} layout={() => mini()!.layout} label="Intelligence Index against cost per task" />
          <Legend items={[{ label: "Pareto frontier", colour: "var(--foreground)" }, { label: "Your models", colour: "var(--foreground)", kind: "ring" },
            ...["Anthropic", "OpenAI", "Google", "Alibaba", "Moonshot AI", "DeepSeek", "xAI"].map((f) => ({ label: f, colour: familyColor(f), kind: "dot" as const })),
            { label: "Other", colour: "var(--series-other)", kind: "dot" }]} />
        </Show>
      </Inset>
    </Section>

  </Site>;
}

mount(() => <Overview />);
