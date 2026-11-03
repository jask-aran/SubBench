import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import test from "node:test";
import { poolHistory } from "../worker/pooling.js";

const directory = new URL("./pooling/golden/", import.meta.url);
for (const name of readdirSync(directory).filter((name) => /^case-.*\.json$/.test(name)).sort()) {
  test(`poolHistory matches ${name}`, () => {
    const fixture = JSON.parse(readFileSync(new URL(name, directory), "utf8"));
    const result = poolHistory(fixture.reports, new Date(fixture.now));
    assert.deepStrictEqual(JSON.parse(JSON.stringify(result)), fixture.expected);
  });
}

test("pooling keeps merge slots, ties, zero bounds, and UTC periods", () => {
  const row = {
    provider: "claude", account_id: null, plan: "max_5x", window: "five_hour",
    reset_key: "2026-11-02T00:30:00+02:00", tier: "confirmed",
    estimate_usd: 100, lower_usd: 0, upper_usd: null,
    covered_quota_percent: null, latest_observed_at: "2026-11-01T20:00:00Z",
  };
  const fuller = { ...row, covered_quota_percent: 30, estimate_usd: 200 };
  const tied = { ...fuller, estimate_usd: 999 };
  const invalid = { ...row, reset_key: "2026-02-30T00:00:00Z" };
  const output = poolHistory([{ windows: [row, invalid] }, { windows: [fuller, tied] }], new Date("2026-11-03T00:00:00Z"));
  assert.deepEqual(output.windows, [fuller, invalid]);
  assert.equal(output.products[0].product, "Claude Max 5X");
  assert.equal(output.products[0].account_count, 1);
  assert.equal(output.products[0].lower_usd, 200);
  assert.equal(output.products[0].upper_usd, 200);
  assert.equal(output.product_series.length, 1);
  assert.equal(output.product_series[0].period_start, "2026-11-01");
});

test("product titles follow Python Unicode titlecase and sorting uses code points", () => {
  const base = {
    provider: "codex", account_id: null, window: "weekly",
    reset_key: "2026-11-01T00:00:00Z", tier: "confirmed", estimate_usd: 1,
    covered_quota_percent: 1, latest_observed_at: "2026-11-01T00:00:00Z",
  };
  const windows = ["ßeta", "ǳ", "ΟΣ", "𐀀", ""].map((plan) => ({ ...base, plan }));
  const result = poolHistory([{ windows }], new Date("2026-11-03T00:00:00Z"));
  assert.deepEqual(result.products.map((row) => row.product), [
    "ChatGPT Sseta", "ChatGPT ǲ", "ChatGPT Ος", "ChatGPT ", "ChatGPT 𐀀",
  ]);
});

test("ISO week reset keys accepted by Python contribute to the series", () => {
  const row = {
    provider: "codex", account_id: "acct-one", plan: "plus", window: "five_hour",
    reset_key: "2026-W44-7T00:00:00+00:00", tier: "confirmed", estimate_usd: 10,
    covered_quota_percent: 1, latest_observed_at: "2026-11-01T00:00:00Z",
  };
  const result = poolHistory([{ windows: [row] }], new Date("2026-11-03T00:00:00Z"));
  assert.equal(result.products[0].estimate_usd, 10);
  assert.equal(result.product_series[0].period_start, "2026-11-01");
});
