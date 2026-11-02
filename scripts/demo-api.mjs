// Demonstration payloads for the dashboard API, for local development only.
//
// The shapes match what `subbench sync push` sends (subbench/push.py build_reports) and
// what the Worker returns. The values are synthetic and every health response carries
// `demo: true`, which the page shows as a "Demo data" badge, so a screenshot of the
// development server can never pass for a real measurement.

const DAY = 86400000;
const HOUR = 3600000;

function random(seed) {
  let state = seed >>> 0;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const PRODUCTS = [
  { provider: "codex", plan: "pro", product: "ChatGPT Pro", weekly: 1480, ratio: 7.4, accounts: ["acct-9f2c1e0a", "acct-41bd77c3"],
    models: [["gpt-5.6-sol", 0.52], ["gpt-5.6-terra", 0.33], ["gpt-5.6-luna", 0.15]] },
  { provider: "claude", plan: "max", product: "Claude Max", weekly: 2210, ratio: 7.98, accounts: [null],
    models: [["claude-opus-5", 0.61], ["claude-fable-5", 0.27], ["claude-sonnet-5", 0.12]] },
];

export function demoReports(now = Date.now()) {
  const rand = random(20261102);
  const iso = (ms) => new Date(ms).toISOString();
  // The next weekly reset, always in the future, so the newest window is the one still running.
  let nextReset = now - (now % (7 * DAY)) + 3 * DAY;
  if (nextReset <= now) nextReset += 7 * DAY;
  const windows = [];
  const series = [];
  const models = [];

  for (const spec of PRODUCTS) {
    for (let week = 9; week >= 0; week--) {
      const reset = nextReset - week * 7 * DAY;
      const drift = 1 + (9 - week) * 0.012 + (week === 3 ? -0.09 : 0);
      for (const account of spec.accounts) {
        const value = spec.weekly * drift * (0.94 + rand() * 0.12);
        const open = reset > now;
        const covered = open ? 30 + rand() * 25 : 70 + rand() * 28;
        windows.push({
          provider: spec.provider, plan: spec.plan, product: spec.product, account_id: account,
          window: "weekly", reset_key: iso(reset),
          estimate_usd: value, lower_usd: value * 0.9, upper_usd: value * 1.08,
          observation_count: 120 + Math.round(rand() * 200), slope_count: 900 + Math.round(rand() * 2000),
          quota_span_percent: covered + 4, covered_quota_percent: covered, coverage_percent: 88 + rand() * 10,
          interval_count: 60 + Math.round(rand() * 80), latest_observed_at: iso(Math.min(now, reset)),
          tier: open ? "provisional" : week < 7 ? "confirmed" : "likely", reason: "",
        });
        // Five-hour windows, a few per week.
        for (let k = 0; k < 5; k++) {
          const fiveReset = reset - (k * 31 + 7) * HOUR;
          const fiveValue = (value / spec.ratio) * (0.88 + rand() * 0.24);
          windows.push({
            provider: spec.provider, plan: spec.plan, product: spec.product, account_id: account,
            window: "five_hour", reset_key: iso(fiveReset),
            estimate_usd: fiveValue, lower_usd: fiveValue * 0.86, upper_usd: fiveValue * 1.12,
            observation_count: 30, slope_count: 300, quota_span_percent: 60, covered_quota_percent: 55,
            coverage_percent: 91, interval_count: 25, latest_observed_at: iso(Math.min(now, fiveReset)),
            tier: fiveReset > now ? "provisional" : "confirmed", reason: "",
          });
        }
        if (!open) {
          for (const [model, share] of spec.models) {
            const weight = share * (0.8 + rand() * 0.4);
            models.push({
              provider: spec.provider, account_id: account, window: "weekly", resets_at: iso(reset),
              model, total_tokens: Math.round(weight * 380e6 * (0.7 + rand() * 0.6)),
            });
          }
        }
      }
      if (reset <= now) {
        const pooled = windows.filter((w) => w.product === spec.product && w.window === "weekly" && w.reset_key === iso(reset));
        const mean = pooled.reduce((s, w) => s + w.estimate_usd, 0) / pooled.length;
        series.push({ product: spec.product, provider: spec.provider, plan: spec.plan, window: "weekly",
                      period_start: iso(reset - 7 * DAY).slice(0, 10), estimate_usd: mean,
                      window_count: pooled.length, account_count: spec.accounts.length });
        series.push({ product: spec.product, provider: spec.provider, plan: spec.plan, window: "five_hour",
                      period_start: iso(reset - 7 * DAY).slice(0, 10), estimate_usd: mean / spec.ratio * (0.95 + rand() * 0.1),
                      window_count: 5 * spec.accounts.length, account_count: spec.accounts.length });
      }
    }
  }

  const pool = (spec, window, tier) => {
    const rows = windows.filter((w) => w.product === spec.product && w.window === window
      && w.tier === tier && Date.parse(w.reset_key) <= now && Date.parse(w.reset_key) > now - 35 * DAY);
    if (!rows.length) return null;
    const values = rows.map((r) => r.estimate_usd).sort((a, b) => a - b);
    const median = values[Math.floor(values.length / 2)];
    return { provider: spec.provider, plan: spec.plan, product: spec.product, window,
             estimate_usd: median, lower_usd: values[0], upper_usd: values.at(-1),
             window_count: rows.length, account_count: spec.accounts.length,
             latest_completed_at: rows.at(-1).reset_key };
  };

  const products = [];
  for (const spec of PRODUCTS) for (const w of ["weekly", "five_hour"]) {
    const row = pool(spec, w, "confirmed");
    if (row) products.push(row);
  }
  const open = PRODUCTS.map((spec) => {
    const rows = windows.filter((w) => w.product === spec.product && w.window === "weekly" && Date.parse(w.reset_key) > now);
    const mean = rows.reduce((s, w) => s + w.estimate_usd, 0) / rows.length;
    return { product: spec.product, provider: spec.provider, plan: spec.plan, window: "weekly",
             estimate_usd: mean, lower_usd: mean * 0.86, upper_usd: mean * 1.15,
             window_count: rows.length, account_count: spec.accounts.length,
             covered_quota_percent: rows[0].covered_quota_percent };
  });

  const total = new Map();
  for (const m of models) {
    const key = m.provider + m.account_id + m.resets_at;
    total.set(key, (total.get(key) || 0) + m.total_tokens);
  }
  for (const m of models) m.share_percent = (100 * m.total_tokens) / total.get(m.provider + m.account_id + m.resets_at);

  const generated = iso(now - 12 * 60000);
  return {
    history: { windows, products, products_likely: [], products_open: open, product_series: series, generated_at: generated },
    models: { models, generated_at: generated },
    current: {
      current: [],
      regime_changes: [],
      divergences: [],
      window_ratios: PRODUCTS.map((s) => ({ provider: s.provider, account_id: s.accounts[0], short_window: "five_hour",
                                          long_window: "weekly", ratio: s.ratio, per_long_window: 1 / s.ratio })),
      generated_at: generated,
    },
    weights: { providers: [], generated_at: generated },
    series: { settled: [], ratios: [], generated_at: generated },
    health: { schema_version: 1, demo: true, now: iso(now), entitlement_rows: 48213, agents: 2,
              last_ingest_at: generated, last_report_at: generated, rejected: 0 },
  };
}
