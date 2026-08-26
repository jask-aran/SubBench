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
  source += "\nglobalThis.page = { renderSummary, renderCharts, renderTable, visibleEstimates, seriesKey };";

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

test("two accounts on one plan are charted as two separate lines", () => {
  const { page, hosts } = harness();
  const rows = page.visibleEstimates([
    windowRow({ account_id: "acct-A", reset_key: past(9), estimate_usd: 105 }),
    windowRow({ account_id: "acct-B", reset_key: past(6), estimate_usd: 152 }),
    windowRow({ account_id: "acct-A", reset_key: past(3), estimate_usd: 166 }),
  ]);
  page.renderCharts(rows);

  // acct-A has two points and draws a line; acct-B has one and draws none. A single
  // product-wide series would have drawn one line through all three.
  const weekly = hosts.charts.children[0];
  assert.equal(weekly.find("path").length, 1);
  assert.equal(weekly.find("circle").length, 3);

  const legend = weekly.children.find((child) => child.className === "legend");
  assert.equal(legend.children.length, 2);
});

test("one account draws one line", () => {
  const { page, hosts } = harness();
  const rows = page.visibleEstimates([
    windowRow({ reset_key: past(9) }),
    windowRow({ reset_key: past(3) }),
  ]);
  page.renderCharts(rows);
  assert.equal(hosts.charts.children[0].find("path").length, 1);
});

test("the headline value is the pooled product figure, not one account's window", () => {
  const { page, hosts } = harness();
  const rows = page.visibleEstimates([
    windowRow({ account_id: "acct-A", estimate_usd: 105 }),
    windowRow({ account_id: "acct-B", estimate_usd: 152 }),
  ]);
  page.renderSummary(
    [{ product: "ChatGPT Plus", window: "weekly", estimate_usd: 128, window_count: 2, account_count: 2 }],
    rows,
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

test("a product with no pooled figure says so rather than showing nothing", () => {
  const { page, hosts } = harness();
  page.renderSummary([], page.visibleEstimates([windowRow()]));
  assert.match(hosts.summary.text, /Not enough evidence yet/);
});
