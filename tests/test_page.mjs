// The site's display rules live in plain modules (web/src/lib) that the Solid pages render, so the rules are tested
// here directly under Node with no browser and no build. Each test is a rule the page must keep, carried over from
// the single-page dashboard this site replaced.
import assert from "node:assert/strict";
import test from "node:test";

import { visibleEstimates, trendPoints, summaryCards, headline, modelTotals, mixByWindow, freshness, settled, STALE_AFTER_MS }
  from "../web/src/lib/value.js";
import { money, range, ago } from "../web/src/lib/format.js";

const DAY = 86400000;
const past = (days) => new Date(Date.now() - days * DAY).toISOString();

function windowRow(overrides = {}) {
  return {
    provider: "codex", plan: "plus", product: "ChatGPT Plus", account_id: "acct-A", window: "weekly",
    reset_key: past(3), estimate_usd: 100, covered_quota_percent: 60, tier: "confirmed", ...overrides,
  };
}

function trendRow(overrides = {}) {
  return {
    product: "ChatGPT Plus", provider: "codex", plan: "plus", window: "weekly",
    period_start: new Date(Date.now() - 7 * DAY).toISOString().slice(0, 10),
    estimate_usd: 100, window_count: 2, account_count: 2, ...overrides,
  };
}

const limit = (card, key) => card.limits.find((item) => item.window.key === key);

test("one series per product, whatever the accounts behind it", () => {
  const points = trendPoints([
    trendRow({ period_start: "2026-08-03", estimate_usd: 105 }),
    trendRow({ period_start: "2026-08-10", estimate_usd: 152, window_count: 3 }),
    trendRow({ product: "Claude", provider: "claude", plan: null, period_start: "2026-08-10", estimate_usd: 300 }),
  ]);
  assert.deepEqual([...new Set(points.map((p) => p.product))].sort(), ["ChatGPT Plus", "Claude"]);
  assert.deepEqual(points.map((p) => p.completed_ms), [...points.map((p) => p.completed_ms)].sort((a, b) => a - b));
});

test("a trend point without a value or a date is dropped", () => {
  assert.equal(trendPoints([trendRow({ estimate_usd: 0 }), trendRow({ period_start: "not a date" })]).length, 0);
});

test("the headline value is the pooled product figure, not one account's window", () => {
  const [card] = summaryCards(
    [{ product: "ChatGPT Plus", window: "weekly", estimate_usd: 128, lower_usd: 120, upper_usd: 140, window_count: 2, account_count: 2 }],
    [windowRow({ account_id: "acct-A", estimate_usd: 105 }), windowRow({ account_id: "acct-B", estimate_usd: 152 })],
  );
  assert.equal(limit(card, "weekly").state, "settled");
  assert.equal(limit(card, "weekly").row.estimate_usd, 128);
  assert.equal(headline(card).value, 128);
});

test("product names come from the payload, so a new plan names itself", () => {
  const rows = visibleEstimates([windowRow({ plan: "pro", product: "ChatGPT Pro" })]);
  assert.equal(rows[0].product, "ChatGPT Pro");
});

test("open, unconfirmed, converted and empty windows are all excluded", () => {
  const rows = visibleEstimates([
    windowRow({ reset_key: past(-2) }),
    windowRow({ tier: "likely" }),
    windowRow({ tier: "provisional" }),
    windowRow({ reset_key: past(3) + "~via~five_hour" }),
    windowRow({ estimate_usd: 0 }),
  ]);
  assert.equal(rows.length, 0);
});

// A product that vanishes is indistinguishable from one nobody is watching.
test("a measured product with nothing confirmed still gets a card", () => {
  const cards = summaryCards([], [windowRow({ product: "Claude Pro", tier: "provisional" })]);
  assert.deepEqual(cards.map((c) => c.name), ["Claude Pro"]);
  assert.equal(limit(cards[0], "weekly").state, "empty");
  assert.equal(headline(cards[0]), null);
});

test("a product with a figure for one limit and not the other shows both", () => {
  const [card] = summaryCards(
    [{ product: "Claude Pro", window: "five_hour", estimate_usd: 31, window_count: 1, account_count: 1 }],
    [windowRow({ product: "Claude Pro", window: "weekly", tier: "provisional" })],
  );
  assert.equal(limit(card, "five_hour").state, "settled");
  assert.equal(limit(card, "weekly").state, "empty");
});

test("a settled figure carries the range it is settled to", () => {
  const [card] = summaryCards(
    [{ product: "ChatGPT Plus", window: "weekly", estimate_usd: 152, lower_usd: 136, upper_usd: 160, window_count: 3, account_count: 2 }],
    [windowRow()],
  );
  assert.equal(money(limit(card, "weekly").row.estimate_usd), "$152");
  assert.equal(range(limit(card, "weekly").row), "$136–$160");
});

test("a likely window is a range with no invented figure", () => {
  const [card] = summaryCards([], [windowRow({ product: "Claude Pro", window: "five_hour" })],
    [{ product: "Claude Pro", window: "five_hour", estimate_usd: 31.85, lower_usd: 21.5, upper_usd: 44.56, window_count: 2, account_count: 1 }]);
  const five = limit(card, "five_hour");
  assert.equal(five.state, "range");
  assert.equal(range(five.row), "$21.50–$44.56");
});

test("the in progress figure appears beside a settled one", () => {
  const [card] = summaryCards(
    [{ product: "ChatGPT Plus", window: "weekly", estimate_usd: 152, lower_usd: 136, upper_usd: 160, window_count: 3, account_count: 2 }],
    [windowRow()], [],
    [{ product: "ChatGPT Plus", window: "weekly", estimate_usd: 93, lower_usd: 81, upper_usd: 106, window_count: 2, account_count: 2, covered_quota_percent: 41.4 }]);
  const weekly = limit(card, "weekly");
  assert.equal(weekly.state, "settled");
  assert.equal(weekly.live.estimate_usd, 93);
});

test("the headline falls back to the in progress weekly figure, never to a 5-hour one", () => {
  const [card] = summaryCards(
    [{ product: "Claude Pro", window: "five_hour", estimate_usd: 31, window_count: 1, account_count: 1 }],
    [windowRow({ product: "Claude Pro" })], [],
    [{ product: "Claude Pro", window: "weekly", estimate_usd: 250, lower_usd: 200, upper_usd: 300, window_count: 1, account_count: 1, covered_quota_percent: 30 }]);
  assert.deepEqual([headline(card).kind, headline(card).value], ["live", 250]);
});

test("model totals count weekly windows only, so contained 5-hour tokens are not counted twice", () => {
  const rows = [
    { provider: "claude", window: "weekly", resets_at: "2026-08-08T00:00:00Z", model: "claude-opus-5", total_tokens: 900 },
    { provider: "claude", window: "weekly", resets_at: "2026-08-08T00:00:00Z", model: "claude-sonnet-5", total_tokens: 100 },
    { provider: "claude", window: "five_hour", resets_at: "2026-08-07T05:00:00Z", model: "claude-opus-5", total_tokens: 500 },
  ];
  const totals = modelTotals(rows);
  assert.equal(totals[0].model, "claude-opus-5");
  assert.equal(totals[0].tokens, 900);
  assert.equal(Math.round(totals[0].share * 100), 90);
});

test("the mix chart gives each window its parts, newest last", () => {
  const rows = [
    { provider: "codex", window: "weekly", resets_at: "2026-08-15", model: "a", total_tokens: 3 },
    { provider: "codex", window: "weekly", resets_at: "2026-08-08", model: "a", total_tokens: 1 },
    { provider: "codex", window: "weekly", resets_at: "2026-08-08", model: "b", total_tokens: 3 },
  ];
  const mix = mixByWindow(rows, { provider: "codex" });
  assert.deepEqual(mix.map((w) => w.resets_at), ["2026-08-08", "2026-08-15"]);
  assert.deepEqual(mix[0].parts.map((p) => p.model), ["b", "a"]);
});

test("a stale report never looks current, and demo data says so", () => {
  const now = Date.now();
  assert.equal(freshness({ last_report_at: new Date(now - 10 * 60000).toISOString() }, now).state, "live");
  assert.equal(freshness({ last_report_at: new Date(now - STALE_AFTER_MS - 60000).toISOString() }, now).state, "stale");
  assert.equal(freshness({ last_report_at: null }, now).state, "stale");
  assert.equal(freshness({ demo: true, last_report_at: new Date(now).toISOString() }, now).state, "demo");
  assert.equal(freshness(null, now).state, "down");
});

test("a reset in the future or a converted key is not settled", () => {
  assert.equal(settled(past(-1)), false);
  assert.equal(settled(past(1) + "~via~five_hour"), false);
  assert.equal(settled(past(1)), true);
});

test("money groups thousands and keeps cents under 100", () => {
  assert.deepEqual([money(1559.4), money(128), money(48.08), money(Number.NaN)], ["$1,559", "$128", "$48.08", "—"]);
  assert.equal(ago(null), "never");
});
