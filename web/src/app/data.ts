import { createSignal } from "solid-js";
// @ts-ignore -- plain JS modules shared with the Node tests
import { indexBundle, matchModel, familySlot } from "../lib/frontier.js";
// @ts-ignore
import { planPrice } from "../lib/plans.js";
// @ts-ignore
import { summaryCards, headline, modelTotals, freshness } from "../lib/value.js";

/*
 * Loading shared by the pages. The benchmark bundle is a static file built into the site; the subscription reports
 * come from the Worker. Each loads on its own, so the benchmark pages work when the API is unreachable and the
 * subscription pages say so rather than failing.
 */

export type Health = { demo?: boolean; last_report_at?: string; last_ingest_at?: string; entitlement_rows?: number } | null;
export type Index = ReturnType<typeof indexBundle>;

export async function loadJSON<T = any>(path: string, timeout = 15000): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(path, { signal: controller.signal, headers: { accept: "application/json" } });
    if (!response.ok) throw new Error(`${path} → ${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timer);
  }
}

let frontier: Promise<Index> | undefined;
export const loadFrontier = () => (frontier ??= loadJSON("/data/frontier.json", 30000).then(indexBundle));

export type Reports = { history: any | null; models: any | null; current: any | null; error: string | null };
export async function loadReports(): Promise<Reports> {
  const [history, models, current] = await Promise.allSettled([loadJSON("/api/history"), loadJSON("/api/models"), loadJSON("/api/current")]);
  const value = (r: PromiseSettledResult<any>) => (r.status === "fulfilled" ? r.value : null);
  return {
    history: value(history), models: value(models), current: value(current),
    error: history.status === "rejected" ? String((history.reason as Error)?.message ?? history.reason) : null,
  };
}

// Health is read once per page load and again on the poll; the shell and the page share it.
const [health, setHealth] = createSignal<Health>(null);
const [healthError, setHealthError] = createSignal<string | null>(null);
export { health, healthError };
export async function refreshHealth() {
  try {
    setHealth(await loadJSON("/api/health"));
    setHealthError(null);
  } catch (error) {
    setHealth(null);
    setHealthError(String((error as Error).message ?? error));
  }
}
export const healthState = () => (healthError() ? { state: "down", label: "Offline" } : freshness(health()));

// A push lands every half hour. Polling faster, or while the tab is hidden, only spends D1 reads on a page that has
// not changed.
export const REFRESH_MS = 5 * 60 * 1000;
export function poll(refresh: () => void) {
  const tick = () => { if (!document.hidden) refresh(); };
  tick();
  const timer = setInterval(tick, REFRESH_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  return () => clearInterval(timer);
}

export type Mine = { entity: any; share: number; tokens: number; usageName: string; provider: string };
export type Personal = { mine: Map<string, Mine>; products: Array<{ card: any; headline: any }>; available: boolean };

/** What the person's own usage brings to the benchmark pages: which benchmarked models they actually run, and what
 *  one week of each subscription is worth at API prices. */
export function personal(index: Index, reports: Reports | null): Personal {
  const out: Personal = { mine: new Map(), products: [], available: false };
  if (!reports) return out;
  for (const row of modelTotals(reports.models?.models || [], { windows: 6 })) {
    const entity = matchModel(row.model, index.entities.model);
    if (!entity) continue;
    const prior = out.mine.get(entity.id);
    out.mine.set(entity.id, { entity, share: (prior?.share || 0) + row.share, tokens: (prior?.tokens || 0) + row.tokens, usageName: row.model, provider: row.provider });
  }
  const h = reports.history || {};
  out.products = summaryCards(h.products || [], h.windows || [], h.products_likely || [], h.products_open || [])
    .map((card: any) => ({ card, headline: headline(card) }))
    .filter((item: any) => item.headline);
  out.available = out.mine.size > 0 || out.products.length > 0;
  return out;
}

const PROVIDER_FAMILY: Record<string, string> = { codex: "OpenAI", claude: "Anthropic" };
export const PROVIDER_NAME: Record<string, string> = { codex: "OpenAI", claude: "Anthropic" };

/** Products take their provider's colour, so every Anthropic plan reads as Anthropic and every OpenAI plan as
 *  OpenAI. Plans of one provider are told apart by line style: the first solid, the next dashed, then dotted. */
// Each plan its own colour, the same on every page, so two plans of one provider can be told apart. A provider's
// cheapest full plan keeps the provider's colour; its other plans, and scoped limits such as "Claude Max 5X · Fable",
// take chart slots no provider uses.
const PLAN_COLOURS = ["var(--series-5)", "var(--series-7)", "var(--series-4)", "var(--series-8)", "var(--series-1)"];
export function productColors(names: Array<{ name: string; provider: string | null }>) {
  const out = new Map<string, string>();
  const monthly = (name: string) => planPrice(name)?.monthly ?? Infinity;
  const ordered = [...names].sort((a, b) => (a.provider ?? "").localeCompare(b.provider ?? "")
    || Number(a.name.includes(" · ")) - Number(b.name.includes(" · ")) || monthly(a.name) - monthly(b.name) || a.name.localeCompare(b.name));
  const main = new Set<string>();
  let spare = 0;
  for (const { name, provider } of ordered) {
    const slot = familySlot(PROVIDER_FAMILY[provider ?? ""] ?? provider ?? "");
    if (!main.has(provider ?? "") && !name.includes(" · ")) {
      main.add(provider ?? "");
      out.set(name, slot ? `var(--series-${slot})` : "var(--series-other)");
    } else out.set(name, PLAN_COLOURS[spare++ % PLAN_COLOURS.length]);
  }
  return out;
}

export function productDashes(names: Array<{ name: string; provider: string | null }>) {
  const seen = new Map<string, number>();
  const out = new Map<string, "solid" | "dash" | "dot">();
  for (const { name, provider } of [...names].sort((a, b) => a.name.localeCompare(b.name))) {
    const n = seen.get(provider ?? "") ?? 0;
    seen.set(provider ?? "", n + 1);
    out.set(name, (["solid", "dash", "dot"] as const)[Math.min(n, 2)]);
  }
  return out;
}
