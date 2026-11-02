import { hoverHTML, type ScatterPoint } from "./charts";
// @ts-ignore
import { points as metricPoints, familyColor, familyGroup, resourceValue, pareto2d, baseKey, RESOURCES, blendNormalization, tasksPerEntitlement } from "../lib/frontier.js";
// @ts-ignore
import { price, duration, integer, score as fmtScore } from "../lib/format.js";
import type { Index, Mine } from "./data";

export const RESOURCE_FORMAT: Record<string, (v: number) => string> = {
  currency: price, duration, integer, blend: (v: number) => v.toFixed(3), speed: (v: number) => `${Math.round(v)} tok/s`,
};

export type Joined = { observation: any; entity: any };

/** Every point of a metric with its x on the chosen resource, filtered, and the 2D Pareto set marked. */
export function joinedScatter(index: Index, metricId: string, resource: string, opts: {
  mine?: Map<string, Mine>; hidden?: Set<string>; search?: string; pinned?: Set<string>; weight?: number;
  weeklyValue?: number | null; frontierOnly?: boolean;
} = {}) {
  const metric = index.metrics.get(metricId);
  const all: Joined[] = metricPoints(index, metricId);
  const needle = (opts.search || "").trim().toLowerCase();
  const visible = all.filter(({ entity }) => !opts.hidden?.has(familyGroup(entity.family))
    && (!needle || [entity.label, entity.id, entity.family, entity.provider, entity.agent, entity.model].filter(Boolean).join(" ").toLowerCase().includes(needle)));
  const normalization = blendNormalization(visible);
  const getX = (p: Joined) => resourceValue(p, resource, { normalization, weight: opts.weight ?? 0.5 });
  const comparable = visible.filter((p) => Number.isFinite(getX(p)) && (!RESOURCES[resource]?.log || getX(p) > 0));
  const frontier: Joined[] = pareto2d(comparable, getX);
  const onFrontier = new Set(frontier.map((p) => p.entity.id + "|" + (p.observation.variant || "")));
  const fmtX = RESOURCE_FORMAT[RESOURCES[resource]?.unit] ?? ((v: number) => String(v));
  const scatter: ScatterPoint[] = comparable.map((p) => {
    const o = p.observation, e = p.entity;
    const key = e.id + "|" + (o.variant || "");
    const mine = opts.mine?.get(e.id);
    const rows: Array<[string, string]> = [[metric.label, fmtScore(o.score, metric.format)], [RESOURCES[resource]?.label ?? resource, fmtX(getX(p))]];
    if (resource !== "cost" && Number.isFinite(o.resources?.cost)) rows.push(["Cost / task", price(o.resources.cost)]);
    if (resource !== "time" && Number.isFinite(o.resources?.time)) rows.push(["Time / task", duration(o.resources.time)]);
    if (Number.isFinite(o.resources?.output_tokens)) rows.push(["Output tokens", integer(o.resources.output_tokens)]);
    const per = tasksPerEntitlement(opts.weeklyValue ?? null, o.resources?.cost);
    if (per) rows.push(["Tasks per weekly limit", integer(per)]);
    return {
      id: key, x: getX(p), y: o.score, colour: familyColor(e.family), label: e.label + (o.variant && !e.label.includes(o.variant) ? ` (${o.variant})` : ""),
      short: e.label, hover: hoverHTML(e.label + (o.variant ? ` · ${o.variant}` : ""), rows,
        [e.family, mine ? `yours · ${Math.round(mine.share * 100)}% of your tokens` : null, onFrontier.has(key) ? "on the Pareto frontier" : null].filter(Boolean).join(" · ")),
      frontier: onFrontier.has(key), mine: !!mine, pinned: opts.pinned?.has(e.id), group: baseKey(e, o),
      dim: opts.frontierOnly ? !onFrontier.has(key) && !mine : false,
    };
  });
  return { metric, all, visible, comparable, frontier, scatter, getX, fmtX };
}
