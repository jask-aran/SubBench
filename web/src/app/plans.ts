import { hoverHTML } from "./charts";
import { productColors } from "./data";
import type { PlanSeries } from "./modelchart";
// @ts-ignore
import { planPrice, valueMultiple } from "../lib/plans.js";
// @ts-ignore
import { price, integer, score as fmtScore } from "../lib/format.js";
// @ts-ignore
import { paretoN } from "../lib/frontier.js";

/*
 * The measured plans, as the Models page and the Subscriptions page both draw them. A plan covers its provider's
 * models: Claude plans Anthropic's, ChatGPT plans OpenAI's. A scoped limit ("Claude Max 5X · Fable") is a model
 * family's own sub-limit: it covers that family only, and its plan then covers the provider's other models.
 */

export type Plan = {
  name: string; provider: string; scope: string | null; value: number; kind: string; accounts: number;
  monthly: number | null; multiple: number | null; colour: string;
};

const scopeOf = (name: string) => name.split(" · ")[1]?.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-") || null;

/** Every plan SubBench has measured, pooled across the accounts that report it, from `personal(...).products`. */
export function measuredPlans(products: any[]): Plan[] {
  const list = products.filter((item: any) => item.card.provider);
  const colours = productColors(list.map((item: any) => ({ name: item.card.name, provider: item.card.provider })));
  return list
    .map((item: any) => ({
      name: item.card.name, provider: item.card.provider, scope: scopeOf(item.card.name), value: item.headline.value, kind: item.headline.kind,
      accounts: item.headline.row?.account_count ?? 1, monthly: planPrice(item.card.name)?.monthly ?? null,
      multiple: valueMultiple(item.card.name, item.headline.value), colour: colours.get(item.card.name) ?? "var(--series-other)",
    }))
    .sort((a: Plan, b: Plan) => a.provider.localeCompare(b.provider) || a.value - b.value);
}

export function providerOf(entity: any) {
  const text = `${entity.provider ?? ""} ${entity.family ?? ""} ${entity.agent ?? ""}`.toLowerCase();
  return text.includes("anthropic") || text.includes("claude") ? "claude" : text.includes("openai") || text.includes("codex") ? "codex" : null;
}

const inScope = (entity: any, scope: string) =>
  `-${[entity.id, entity.label, entity.model, entity.host_model_slug].filter(Boolean).join(" ")}-`.toLowerCase()
    .replace(/[^a-z0-9]+/g, "-").includes(`-${scope}-`);

export function covers(plans: Plan[], plan: Plan, entity: any) {
  return plan.provider === providerOf(entity) && (plan.scope
    ? inScope(entity, plan.scope)
    : !plans.some((other) => other.provider === plan.provider && other.scope && inScope(entity, other.scope)));
}

export const pointKey = (p: any) => p.entity.id + "|" + (p.observation.variant || "");
export const pointLabel = (p: any) => p.entity.label + (p.observation.variant && !p.entity.label.includes(p.observation.variant) ? ` (${p.observation.variant})` : "");
export const weekPrice = (plan: Plan) => (plan.monthly ? planPrice(plan.name).weekly as number : null);

/**
 * Each plan's tasks on one benchmark (`points` from `points(index, metric)`): per week, or per dollar of the price.
 * A plan's best trade-offs (more tasks and a higher score than any alternative on the same plan) are marked `best`.
 */
export function planSeries(plans: Plan[], points: any[], o: { perDollar: boolean; metric: any; keep?: (p: any) => boolean }): PlanSeries[] {
  return plans.map((plan) => {
    const out = points.filter((p: any) => covers(plans, plan, p.entity) && p.observation.resources?.cost > 0 && (!o.keep || o.keep(p)))
      .map((p: any) => {
        const week = plan.value / p.observation.resources.cost;
        const wp = weekPrice(plan);
        const dollar = wp ? week / wp : null;
        return { key: pointKey(p), label: pointLabel(p), short: p.entity.label, tasks: o.perDollar ? dollar ?? 0 : week, score: p.observation.score,
          hover: hoverHTML(pointLabel(p), [["Plan", plan.name + (plan.monthly ? ` · $${plan.monthly}/mo` : "")], ["Tasks / week", integer(week)],
            ...(dollar ? [["Tasks / $", dollar < 10 ? dollar.toFixed(1) : integer(dollar)] as [string, string]] : []),
            [o.metric?.label ?? "Score", fmtScore(p.observation.score, o.metric?.format)], ["Cost / task", price(p.observation.resources.cost)]]), best: false };
      });
    const keep = new Set(paretoN(out, [(q: any) => q.tasks, (q: any) => q.score], [true, true]).map((q: any) => q.key));
    for (const q of out) q.best = keep.has(q.key);
    // Each plan has its own colour, so no line needs a dash to be told apart.
    return { name: plan.name, colour: plan.colour, dash: false, points: out.filter((q: any) => q.tasks > 0) };
  }).filter((s) => s.points.length);
}
