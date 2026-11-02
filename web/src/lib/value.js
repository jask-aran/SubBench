// Shaping of the SubBench reports for display. Pure functions over the JSON the Worker
// serves: every estimate is derived by the collector (subbench/regression.py) and this
// module only decides what is shown, and how prominently. It contains no estimator.

export const WINDOWS = [
  { key: "weekly", label: "Weekly limit", short: "Weekly" },
  { key: "five_hour", label: "5-hour limit", short: "5-hour" },
];

export const windowLabel = (key) => WINDOWS.find((w) => w.key === key)?.short ?? key;

// Product names arrive with the data. The collector builds them from the plan the provider
// reports beside the meter, so an upgrade renames the product without a code change.
export function product(row) {
  return String(row.product || row.provider || "Unknown");
}

export function sourceAccount(row) {
  return row.account_id ? String(row.account_id).slice(0, 8) : "single source";
}

// A window is settled once its reset time has passed. A converted estimate ("~via~")
// is a different measurement of the same allowance, never a completed window itself.
export function settled(resetKey, now = Date.now()) {
  if (!resetKey || String(resetKey).includes("~via~")) return false;
  const stamp = Date.parse(resetKey);
  return Number.isFinite(stamp) && stamp <= now;
}

// Only completed, confirmed windows appear as individual estimates.
export function visibleEstimates(history, now = Date.now()) {
  return (history || [])
    .filter((row) =>
      row.tier === "confirmed"
      && (row.window === "weekly" || row.window === "five_hour")
      && settled(row.reset_key, now)
      && Number(row.estimate_usd) > 0)
    .map((row) => ({
      ...row,
      product: product(row),
      completed_at: row.reset_key,
      completed_ms: Date.parse(row.reset_key),
    }))
    .sort((a, b) => a.completed_ms - b.completed_ms);
}

// Trend points arrive already pooled per product and period, so a chart plots what a
// product is worth rather than what one account measured.
export function trendPoints(series) {
  return (series || [])
    .filter((row) => Number(row.estimate_usd) > 0)
    .map((row) => ({
      ...row,
      product: product(row),
      completed_at: row.period_start,
      completed_ms: Date.parse(row.period_start),
    }))
    .filter((row) => Number.isFinite(row.completed_ms))
    .sort((a, b) => a.completed_ms - b.completed_ms);
}

export function productNames(rows) {
  return [...new Set(rows.map((row) => row.product))].sort();
}

// One card model per product. Each limit resolves to exactly one of three states:
//   settled  a pooled confirmed figure, with the range it is settled to
//   range    everything but the interval width passed, so only the range is shown
//   empty    measured, but not enough evidence yet
// plus an optional "in progress" figure pooled from windows still running. Every product
// being measured gets a card: dropping one would make a product still gathering evidence
// look the same as one SubBench is not watching.
export function summaryCards(products = [], measured = [], wide = [], open = []) {
  const key = (row) => product(row) + " " + row.window;
  const index = (rows) => new Map(rows.map((row) => [key(row), row]));
  const pooled = index(products);
  const ranged = index(wide);
  const running = index(open);

  const names = [...new Set([
    ...products.map(product),
    ...wide.map(product),
    ...productNames((measured || []).map((row) => ({ product: product(row) }))),
  ])].sort();

  return names.map((name) => {
    const provider = [...products, ...wide, ...open, ...(measured || [])]
      .find((row) => product(row) === name)?.provider ?? null;
    const limits = WINDOWS.map((window) => {
      const id = name + " " + window.key;
      const row = pooled.get(id);
      const wider = ranged.get(id);
      const live = running.get(id) ?? null;
      if (row) return { window, state: "settled", row, live };
      if (wider) return { window, state: "range", row: wider, live };
      return { window, state: "empty", row: null, live };
    });
    return { name, provider, limits };
  });
}

// The single best headline per product: the settled weekly figure when there is one,
// otherwise the in-progress weekly figure, otherwise nothing.
export function headline(card) {
  const weekly = card.limits.find((limit) => limit.window.key === "weekly");
  if (weekly?.state === "settled") return { kind: "settled", value: weekly.row.estimate_usd, row: weekly.row };
  if (weekly?.live) return { kind: "live", value: weekly.live.estimate_usd, row: weekly.live };
  if (weekly?.state === "range") {
    return { kind: "range", value: (weekly.row.lower_usd + weekly.row.upper_usd) / 2, row: weekly.row };
  }
  return null;
}

// Per-model token share across the most recent windows. Weekly windows contain the
// five-hour ones, so only weekly rows are summed when any exist; adding both would count
// the same tokens twice.
export function modelTotals(models = [], { provider = null, windows = 5 } = {}) {
  let rows = models.filter((row) => !provider || row.provider === provider);
  if (rows.some((row) => row.window === "weekly")) rows = rows.filter((row) => row.window === "weekly");
  const resets = [...new Set(rows.map((row) => String(row.resets_at)))].sort().slice(-windows);
  const keep = new Set(resets);
  const totals = new Map();
  for (const row of rows) {
    if (!keep.has(String(row.resets_at))) continue;
    const id = row.provider + "\u0000" + row.model;
    const entry = totals.get(id) || { provider: row.provider, model: row.model, tokens: 0 };
    entry.tokens += Number(row.total_tokens) || 0;
    totals.set(id, entry);
  }
  const all = [...totals.values()];
  const sum = all.reduce((total, row) => total + row.tokens, 0);
  return all
    .map((row) => ({ ...row, share: sum ? row.tokens / sum : 0 }))
    .sort((a, b) => b.tokens - a.tokens);
}

// Stacked model mix per window, newest last, for the mix chart.
export function mixByWindow(models = [], { provider, window = "weekly", limit = 8 } = {}) {
  const rows = models.filter((row) => row.provider === provider && row.window === window);
  const groups = new Map();
  for (const row of rows) {
    const id = String(row.resets_at);
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(row);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .slice(-limit)
    .map(([resets_at, items]) => {
      const total = items.reduce((sum, item) => sum + Number(item.total_tokens || 0), 0);
      return {
        resets_at,
        total,
        parts: items
          .map((item) => ({ model: item.model, tokens: Number(item.total_tokens || 0),
                            share: total ? Number(item.total_tokens || 0) / total : 0 }))
          .sort((a, b) => b.tokens - a.tokens),
      };
    });
}

// A stale report must never look current. A push lands every half hour.
export const STALE_AFTER_MS = 75 * 60 * 1000;

export function freshness(health, now = Date.now()) {
  if (!health) return { state: "down", label: "No connection" };
  if (health.demo) return { state: "demo", label: "Demo data" };
  const last = health.last_report_at ? Date.parse(health.last_report_at) : NaN;
  if (!Number.isFinite(last)) return { state: "stale", label: "No reports yet" };
  return now - last > STALE_AFTER_MS ? { state: "stale", label: "Stale" } : { state: "live", label: "Live" };
}
