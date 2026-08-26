// The dashboard is static HTML with no build step, so its logic is otherwise only
// exercised by opening a browser. These tests run the page script against a small DOM
// stub and assert on the structure it builds.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const PAGE = new URL("../src/subbench/server/static/index.html", import.meta.url);

class Node {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.style = {};
    this.classList = { add: (name) => { this.className = [this.className, name].filter(Boolean).join(" "); } };
    this.className = "";
    this.textContent = "";
  }

  append(...nodes) {
    for (const node of nodes) this.children.push(node);
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
  }

  // Enough of the table API for renderTable.
  insertRow() {
    const row = new Node("tr");
    row.insertCell = () => {
      const cell = new Node("td");
      row.append(cell);
      return cell;
    };
    this.append(row);
    return row;
  }

  // Every node of a given tag anywhere below this one.
  find(tag) {
    return this.children.flatMap((child) =>
      (child.tag === tag ? [child] : []).concat(child.find ? child.find(tag) : []));
  }

  get text() {
    return [this.textContent, ...this.children.map((child) => child.text || "")].join(" ");
  }
}

function harness() {
  const hosts = {
    summary: new Node("div"),
    charts: new Node("div"),
    estimates: new Node("table"),
    meta: new Node("div"),
    footer: new Node("footer"),
  };
  const document = {
    getElementById: (id) => hosts[id],
    createElement: (tag) => new Node(tag),
    createElementNS: (_ns, tag) => new Node(tag),
    createTextNode: (value) => Object.assign(new Node("#text"), { textContent: value }),
  };

  let source = readFileSync(PAGE, "utf8").match(/<script>([\s\S]*)<\/script>/)[1];
  // The page refreshes itself on load and on a timer. Neither belongs in a unit test.
  source = source.replace(/\nrefresh\(\);\n\s*setInterval\([^)]*\);\s*$/, "\n");
  source += "\nglobalThis.page = { renderSummary, renderCharts, renderTable, visibleEstimates, trendPoints };";

  const context = vm.createContext({ document, setInterval() {}, fetch() {}, console });
  vm.runInContext(source, context);
  return { page: context.page, hosts };
}

const DAY = 86400000;
const past = (days) => new Date(Date.now() - days * DAY).toISOString();

function windowRow(overrides = {}) {
  return {
    provider: "codex",
    plan: "plus",
    product: "ChatGPT Plus",
    account_id: "acct-A",
    window: "weekly",
    reset_key: past(3),
    estimate_usd: 100,
    covered_quota_percent: 60,
    tier: "confirmed",
    ...overrides,
  };
}

function trendRow(overrides = {}) {
  return {
    product: "ChatGPT Plus",
    provider: "codex",
    plan: "plus",
    window: "weekly",
    period_start: new Date(Date.now() - 7 * DAY).toISOString().slice(0, 10),
    estimate_usd: 100,
    window_count: 2,
    account_count: 2,
    ...overrides,
  };
}

test("one line per product, whatever the accounts behind it", () => {
  const { page, hosts } = harness();
  const points = page.trendPoints([
    trendRow({ period_start: "2026-08-03", estimate_usd: 105, window_count: 2, account_count: 2 }),
    trendRow({ period_start: "2026-08-10", estimate_usd: 152, window_count: 3, account_count: 2 }),
    trendRow({ product: "Claude", provider: "claude", plan: null, period_start: "2026-08-10",
               estimate_usd: 300, window_count: 1, account_count: 1 }),
  ]);
  page.renderCharts(points);

  const weekly = hosts.charts.children[0];
  // ChatGPT Plus has two periods and draws a line; Claude has one period and draws none.
  assert.equal(weekly.find("path").length, 1);
  assert.equal(weekly.find("circle").length, 3);

  const legend = weekly.children.find((child) => child.className === "legend");
  assert.deepEqual(legend.children.map((item) => item.text.trim()), ["ChatGPT Plus", "Claude"]);
});

test("the chart counts the windows pooled into its points", () => {
  const { page, hosts } = harness();
  page.renderCharts(page.trendPoints([
    trendRow({ period_start: "2026-08-03", window_count: 2 }),
    trendRow({ period_start: "2026-08-10", window_count: 3 }),
  ]));
  assert.match(hosts.charts.children[0].text, /5 confirmed windows over 2 points/);
});

test("a trend point names the accounts it pooled, not one source account", () => {
  const { page, hosts } = harness();
  page.renderCharts(page.trendPoints([
    trendRow({ period_start: "2026-08-03" }),
    trendRow({ period_start: "2026-08-10" }),
  ]));
  assert.match(hosts.charts.children[0].text, /2 windows · 2 accounts/);
});

test("the headline value is the pooled product figure, not one account's window", () => {
  const { page, hosts } = harness();
  page.renderSummary(
    [{ product: "ChatGPT Plus", window: "weekly", estimate_usd: 128, window_count: 2, account_count: 2 }],
    [windowRow({ account_id: "acct-A", estimate_usd: 105 }),
     windowRow({ account_id: "acct-B", estimate_usd: 152 })],
  );
  const text = hosts.summary.text;
  assert.match(text, /\$128\b/);
  assert.match(text, /2 windows · 2 accounts/);
  assert.doesNotMatch(text, /\$152\b/);
});

test("product names come from the payload, so a new plan names itself", () => {
  const { page, hosts } = harness();
  const rows = page.visibleEstimates([windowRow({ plan: "pro", product: "ChatGPT Pro" })]);
  page.renderTable(rows);
  assert.match(hosts.estimates.text, /ChatGPT Pro/);
});

test("open, unconfirmed and converted windows are all excluded", () => {
  const { page } = harness();
  const rows = page.visibleEstimates([
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
  const { page, hosts } = harness();
  page.renderSummary([], [windowRow({ product: "Claude Pro", tier: "provisional" })]);
  assert.match(hosts.summary.text, /Claude Pro/);
  assert.match(hosts.summary.text, /Not enough evidence yet/);
});

test("a product with a figure for one limit and not the other shows both", () => {
  const { page, hosts } = harness();
  page.renderSummary(
    [{ product: "Claude Pro", window: "five_hour", estimate_usd: 31, window_count: 1, account_count: 1 }],
    [windowRow({ product: "Claude Pro", window: "weekly", tier: "provisional" })],
  );
  assert.match(hosts.summary.text, /\$31/);
  assert.match(hosts.summary.text, /Not enough evidence yet/);
});
