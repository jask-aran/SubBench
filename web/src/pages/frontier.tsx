import { createEffect, createMemo, createResource, createSignal, For, Show } from "solid-js";
import { Site, mount } from "../app/shell";
import { Hero, Inset, Legend, Bars, FamilyChips, Skeleton, Section } from "../app/bits";
import { Plot, basic } from "../app/plot";
import { frontierScatter } from "../app/charts";
import { joinedScatter, RESOURCE_FORMAT } from "../app/points";
import { loadFrontier, loadReports, personal } from "../app/data";
import { Segmented, Input, Swatch, Slider } from "../conduit/ui/controls";
import { DashRow, DashEmpty } from "../conduit/ui/dashboard";
import { Select } from "../conduit/menus";
// @ts-ignore
import { price, duration, integer, score as fmtScore } from "../lib/format.js";
// @ts-ignore
import { points as metricPoints, availableResources, familyColor, familyGroup, solve, resourceValue, RESOURCES, tasksPerEntitlement } from "../lib/frontier.js";

type Mode = "model" | "agent";
const params = new URLSearchParams(location.search);

function Frontier() {
  const [index] = createResource(loadFrontier);
  const [reports] = createResource(loadReports);
  const me = createMemo(() => (index() ? personal(index()!, reports() ?? null) : null));
  const weekly = () => me()?.products[0]?.headline.value ?? null;

  const [mode, setMode] = createSignal<Mode>(params.get("mode") === "agent" ? "agent" : "model");
  const [metric, setMetric] = createSignal(params.get("lens") || "aa-intelligence");
  const [resource, setResource] = createSignal(params.get("x") || "cost");
  const [log, setLog] = createSignal(true);
  const [tiers, setTiers] = createSignal(true);
  const [frontierOnly, setFrontierOnly] = createSignal(false);
  const [search, setSearch] = createSignal("");
  const [hidden, setHidden] = createSignal<Set<string>>(new Set());
  const [pinned, setPinned] = createSignal<Set<string>>(new Set());
  const [target, setTarget] = createSignal("90");
  const [solveBy, setSolveBy] = createSignal<"x" | "cost" | "time">("x");

  const metrics = createMemo(() => (index() ? [...index()!.metrics.values()].filter((m: any) => m.entity_type === mode()) : []));
  // A lens of the other mode, or one missing from the bundle, falls back to that mode's headline lens.
  createEffect(() => {
    if (!index()) return;
    if (!metrics().some((m: any) => m.id === metric())) setMetric(mode() === "model" ? "aa-intelligence" : "agent-index");
  });
  const resources = createMemo(() => (index() ? availableResources(metricPoints(index()!, metric())) as string[] : []));
  createEffect(() => {
    const list = resources();
    if (list.length && !list.includes(resource())) setResource(list.includes("cost") ? "cost" : list[0]);
  });
  createEffect(() => {
    const next = new URLSearchParams({ ...(mode() === "agent" ? { mode: "agent" } : {}), lens: metric(), x: resource() });
    history.replaceState(null, "", `${location.pathname}?${next}`);
  });

  const ranking = () => resources().length === 0;
  const joined = createMemo(() => index() && !ranking()
    ? joinedScatter(index()!, metric(), resource(), { mine: me()?.mine, hidden: hidden(), search: search(), pinned: pinned(), weeklyValue: weekly(), frontierOnly: frontierOnly() })
    : null);

  const solver = createMemo(() => {
    const j = joined();
    if (!j) return null;
    const by = solveBy() === "x" ? resource() : solveBy();
    const getValue = (p: any) => by === resource() ? j.getX(p) : resourceValue(p, by, {});
    const winner = solve(j.comparable, { target: Number(target()) / 100, getValue });
    if (!winner) return null;
    const best = Math.max(...j.comparable.map((p: any) => p.observation.score));
    return { winner, by, value: getValue(winner), threshold: best * Number(target()) / 100 };
  });

  const chart = createMemo(() => {
    const j = joined();
    if (!j) return null;
    const win = solver()?.winner;
    const pts = j.scatter.map((p) => (win && p.id === win.entity.id + "|" + (win.observation.variant || "") ? { ...p, pinned: true } : p));
    const unit = RESOURCES[resource()]?.unit;
    return frontierScatter(pts, {
      xLog: log() && !!RESOURCES[resource()]?.log, xTitle: RESOURCES[resource()]?.label ?? resource(), yTitle: j.metric.label,
      yFormat: j.metric.format === "percent" ? ".0%" : "", xFormat: unit === "currency" ? "$.3~g" : undefined, groups: tiers() && mode() === "model", height: 540, gutter: true,
    });
  });

  const families = createMemo(() => {
    if (!index()) return [];
    const counts = new Map<string, number>();
    for (const p of metricPoints(index()!, metric())) counts.set(familyGroup(p.entity.family), (counts.get(familyGroup(p.entity.family)) || 0) + 1);
    return [...counts.entries()].sort((a, b) => Number(a[0] === "Other") - Number(b[0] === "Other") || b[1] - a[1]).map(([name, count]) => ({ name, count, colour: name === "Other" ? "var(--series-other)" : familyColor(name) }));
  });
  const toggle = (name: string) => { const next = new Set<string>(hidden()); next.has(name) ? next.delete(name) : next.add(name); setHidden(next); };
  const solo = (name: string) => setHidden(new Set<string>(families().map((f) => f.name).filter((n) => n !== name)));
  const pin = (id: string) => { const next = new Set(pinned()); next.has(id) ? next.delete(id) : next.add(id); setPinned(next); };

  const scopeNote = createMemo(() => {
    const m: any = index()?.metrics.get(metric());
    if (!m) return null;
    if (ranking()) return "No benchmark-specific cost or time is published for this lens, so it is a capability ranking rather than borrowing the composite index's resources.";
    if (resource() === "weighted_cost") return "Weighted eval cost is this benchmark's contribution to the Intelligence Index cost: useful for relative efficiency, but not an unweighted per-task price.";
    if (resource() === "blend") return "The blend min–max normalises cost and time across the visible set, so changing the filters changes this relative index.";
    const scopes = new Set((joined()?.comparable || []).map((p: any) => p.observation.resource_scope));
    if (scopes.has("configuration-wide") && !m.composite) return "Cost, time and tokens here are the whole harness–model configuration's across the agent suite, not this component benchmark's alone.";
    return null;
  });

  const rankRows = createMemo(() => {
    if (!index() || !ranking()) return [];
    const m: any = index()!.metrics.get(metric());
    return metricPoints(index()!, metric())
      .filter((p: any) => !hidden().has(familyGroup(p.entity.family)))
      .sort((a: any, b: any) => b.observation.score - a.observation.score).slice(0, 30)
      .map((p: any) => ({ key: p.entity.id + (p.observation.variant || ""), label: p.entity.label, colour: familyColor(p.entity.family), value: p.observation.score,
        display: fmtScore(p.observation.score, m.format), tag: me()?.mine.has(p.entity.id) ? "yours" : undefined }));
  });

  const compare = createMemo(() => {
    const j = joined();
    if (!j || !pinned().size) return [];
    return j.comparable.filter((p: any) => pinned().has(p.entity.id));
  });
  const fmtX = (v: number) => (RESOURCE_FORMAT[RESOURCES[resource()]?.unit] ?? String)(v);

  return <Site page="frontier" title="Frontier explorer">
    <Hero small kicker="Frontier explorer" title="Capability against what it costs"
      lede="Pick a capability lens and a resource. The stepped line joins the configurations no visible alternative beats on both axes; anything under it is paying for nothing. Click points to pin them for comparison." />

    <div class="sb-section-tools" style={{ "margin-bottom": "var(--space-lg)" }}>
      <Segmented label="Entity type" value={mode()} onChange={(v) => { setMode(v); setHidden(new Set<string>()); setPinned(new Set<string>()); }}
        options={[{ value: "model", label: "Models" }, { value: "agent", label: "Coding agents" }]} />
      <Select aria-label="Capability lens" search="Find a lens" bare={false} value={metric()} onChange={(e) => setMetric(e.currentTarget.value)}>
        <For each={metrics()}>{(m: any) => <option value={m.id}>{m.label} · {m.count}</option>}</For>
      </Select>
      <Show when={!ranking()}>
        <Select aria-label="Resource axis" bare={false} value={resource()} onChange={(e) => setResource(e.currentTarget.value)}>
          <For each={resources()}>{(key) => <option value={key}>{RESOURCES[key]?.label ?? key}</option>}</For>
        </Select>
        <button type="button" class="sb-chip" aria-pressed={log()} onClick={() => setLog(!log())}>Log scale</button>
        <Show when={mode() === "model"}><button type="button" class="sb-chip" aria-pressed={tiers()} onClick={() => setTiers(!tiers())}>Tier lines</button></Show>
        <button type="button" class="sb-chip" aria-pressed={frontierOnly()} onClick={() => setFrontierOnly(!frontierOnly())}>Frontier only</button>
      </Show>
      <div style={{ "margin-left": "auto", width: "14rem", "max-width": "100%" }}>
        <Input type="search" placeholder="Find a model" aria-label="Find a model" value={search()} onInput={(e) => setSearch(e.currentTarget.value)} />
      </div>
    </div>

    <div class="sb-grid main-aside">
      <Inset title={(index()?.metrics.get(metric()) as any)?.label ?? "Loading"} note={(index()?.metrics.get(metric()) as any)?.description}
        aside={<span class="sb-tag" data-tone="quiet">{ranking() ? "ranking" : `${joined()?.comparable.length ?? 0} visible`}</span>}>
        <Show when={scopeNote()}><div class="sb-callout" data-tone="warn" style={{ "margin-bottom": "var(--space-md)" }}>{scopeNote()}</div></Show>
        <Show when={index()} fallback={<Skeleton height="480px" />}>
          <Show when={!ranking()} fallback={<Bars rows={rankRows()} />}>
            <Show when={(joined()?.comparable.length ?? 0) > 0} fallback={<div class="sb-chart-empty">No comparable records. Change the lens or show more families.</div>}>
              <Plot plotly={basic} data={() => chart()!.traces} layout={() => chart()!.layout} label="Capability against resource"
                onClick={(event) => { const id = event?.points?.[0]?.customdata; if (typeof id === "string") pin(id.split("|")[0]); }} />
            </Show>
          </Show>
        </Show>
        <div style={{ "margin-top": "var(--space-md)" }}>
          <FamilyChips families={families()} hidden={hidden()} onToggle={toggle} onSolo={solo} />
        </div>
        <Legend items={[{ label: "Pareto frontier", colour: "var(--foreground)" }, { label: "Yours, pinned or solved", colour: "var(--foreground)", kind: "ring" }]} />
      </Inset>

      <div class="sb-grid">
        <Inset title="Efficiency solver" note={`Clear ${target()}% of the best visible score, then minimise`}>
          <Slider label="Target" value={target()} onChange={setTarget}
            options={[50, 60, 70, 75, 80, 85, 90, 95, 100].map((v) => ({ value: String(v), label: `${v}%` }))} />
          <div style={{ margin: "var(--space-md) 0" }}>
            <Segmented label="Minimise" size="sm" value={solveBy()} onChange={setSolveBy}
              options={[{ value: "x", label: "x-axis" }, { value: "cost", label: "Cost" }, { value: "time", label: "Time" }]} />
          </div>
          <Show when={solver()} fallback={<DashEmpty>{ranking() ? "This lens publishes no resource to minimise." : "Nothing visible clears this target with that resource."}</DashEmpty>}>
            {(s) => <div class="sb-callout">
              <b>{s().winner.entity.label}</b> scores {fmtScore(s().winner.observation.score, (index()!.metrics.get(metric()) as any).format)}
              {" "}for {(RESOURCE_FORMAT[RESOURCES[s().by]?.unit] ?? String)(s().value)} {RESOURCES[s().by]?.short}. It clears {fmtScore(s().threshold, (index()!.metrics.get(metric()) as any).format)}.
              <Show when={tasksPerEntitlement(weekly(), s().winner.observation.resources?.cost)}>{(per) => <> One weekly limit buys about <b>{integer(per())}</b> such tasks.</>}</Show>
            </div>}
          </Show>
        </Inset>
        <Inset title="Pareto set" note="Non-dominated on these axes, cheapest first" aside={<span class="sb-tag" data-tone="quiet">{joined()?.frontier.length ?? 0}</span>}>
          <div class="sb-rows">
            <For each={joined()?.frontier || []} fallback={<DashEmpty>{ranking() ? "A ranking has no resource frontier." : "Nothing visible."}</DashEmpty>}>{(p: any, i) =>
              <DashRow element="button" onClick={() => pin(p.entity.id)} current={pinned().has(p.entity.id)}
                lead={<Swatch colour={familyColor(p.entity.family)} />}
                primary={<><span class="sb-rank">{String(i() + 1).padStart(2, "0")}</span>{p.entity.label}{p.observation.variant && !p.entity.label.includes(p.observation.variant) ? ` (${p.observation.variant})` : ""}</>}
                trailing={`${fmtScore(p.observation.score, (index()!.metrics.get(metric()) as any).format, true)} · ${fmtX(joined()!.getX(p))}`} />}</For>
          </div>
        </Inset>
      </div>
    </div>

    <Show when={compare().length}>
      <Section title="Comparison" note="Pinned configurations on this lens. Click a point or a Pareto row to pin or unpin it."
        tools={<button type="button" class="sb-chip" onClick={() => setPinned(new Set())}>Clear</button>}>
        <Inset flush>
          <div class="sb-table-wrap"><table class="sb-table">
            <thead><tr><th>Configuration</th><th>Family</th><th class="num">Score</th><th class="num">Cost / task</th><th class="num">Time / task</th><th class="num">Output tokens</th><th class="num">Tasks / weekly limit</th></tr></thead>
            <tbody><For each={compare()}>{(p: any) => {
              const m: any = index()!.metrics.get(metric());
              const per = tasksPerEntitlement(weekly(), p.observation.resources?.cost);
              return <tr>
                <td><span style={{ display: "inline-flex", gap: "var(--space-sm)", "align-items": "center" }}><Swatch colour={familyColor(p.entity.family)} />{p.entity.label}{p.observation.variant ? ` · ${p.observation.variant}` : ""}</span></td>
                <td class="data">{p.entity.family}</td>
                <td class="num">{fmtScore(p.observation.score, m.format)}</td>
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

mount(() => <Frontier />);
