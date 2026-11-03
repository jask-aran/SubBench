import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import { poolHistory } from "../worker/pooling.js";
import worker, { poolIfDirty, pseudonym } from "../worker/index.js";

const REPORTS = {
  current: { current: [], regime_changes: [] },
  history: { windows: [] },
  models: { models: [] },
  weights: { providers: [] },
  series: { settled: [], replay: [], ratios: [] },
};

class FakeD1 {
  constructor() {
    this.bound = [];
    this.healthSql = "";
    this.reports = {};
    this.agents = new Map();
    this.agentReports = new Map();
    this.pooled = null;
    this.state = null;
    this.missingPoolTable = false;
    this.poolReadError = null;
    this.executed = [];
    this.queries = [];
  }

  prepare(sql) {
    this.healthSql = sql.includes("entitlement_snapshots") ? sql : this.healthSql;
    const statement = (params) => ({
      sql, params,
      first: async () => this.first(sql, params),
      all: async () => this.all(sql, params),
      run: async () => this.run(sql, params),
    });
    return Object.assign(statement([]), {
      bind: (...params) => {
        const bound = statement(params);
        this.bound.push(bound);
        return bound;
      },
    });
  }

  async all(sql, params) {
    this.queries.push({ sql, params });
    if (sql.includes("JOIN agents")) {
      return { results: [...this.agents.entries()]
        .filter(([id, lastSeen]) => lastSeen >= params[0] && this.agentReports.has(id))
        .sort((a, b) => a[1] > b[1] ? -1 : a[1] < b[1] ? 1 : 0)
        .slice(0, params[1])
        .map(([id]) => ({ payload: JSON.stringify(this.agentReports.get(id)) })) };
    }
    throw new Error(`Unexpected query: ${sql}`);
  }

  async first(sql, params) {
    this.queries.push({ sql, params });
    if (sql.includes("FROM pool_state")) return this.state;
    if (sql.includes("FROM pooled_reports")) {
      if (this.missingPoolTable) throw new Error("D1_ERROR: no such table: pooled_reports: SQLITE_ERROR");
      if (this.poolReadError) throw this.poolReadError;
      return this.pooled;
    }
    if (sql.includes("SELECT payload, generated_at")) {
      const payload = this.reports[params[0]];
      return payload ? { payload: JSON.stringify(payload), generated_at: "2026-08-01T00:00:00Z" } : null;
    }
    if (sql.includes("MAX(rowid) FROM entitlement_snapshots")) {
      return {
        entitlement_rows: 1,
        agents: 1,
        last_ingest_at: "2026-08-01T00:00:00Z",
        last_report_at: "2026-08-01T00:00:00Z",
        rejected: 0,
      };
    }
    return null;
  }

  async run(sql, params) {
    this.executed.push({ sql, params });
    if (sql.includes("INSERT INTO reports")) {
      this.reports[params[1]] = JSON.parse(params[3]);
      if (params[1] === "pool_input") this.agentReports.set(params[0], JSON.parse(params[3]));
    } else if (sql.includes("INSERT INTO agents")) {
      this.agents.set(params[0], params[2]);
    } else if (sql.includes("INSERT INTO pool_state")) {
      this.state = { ...this.state, dirty: 1, revision: this.state ? this.state.revision + 1 : 0 };
    } else if (sql.includes("INSERT INTO pooled_reports")) {
      if (this.state?.dirty === 1 && this.state.revision === params[3]) {
        this.pooled = { generated_at: params[1], payload: params[2] };
      }
    } else if (sql.includes("DELETE FROM pooled_reports")) {
      if (this.state?.dirty === 1 && this.state.revision === params[1]) this.pooled = null;
    } else if (sql.includes("UPDATE pool_state")) {
      if (this.state?.dirty === 1 && this.state.revision === params[1]) {
        this.state = { ...this.state, dirty: 0, pooled_at: params[0] };
      }
    }
    return {};
  }

  async batch(statements) {
    for (const { sql, params } of statements) await this.run(sql, params);
    return {};
  }
}

function env(db) {
  return {
    DB: db,
    SUBBENCH_INGEST_TOKEN: "token",
    ASSETS: { fetch: async () => new Response("not found", { status: 404 }) },
  };
}

function request(path, options = {}) {
  return new Request(`https://example.test${path}`, options);
}

test("measurement-only ingest stores reports and no raw usage rows", async () => {
  const db = new FakeD1();
  const response = await worker.fetch(request("/ingest", {
    method: "POST",
    headers: { authorization: "Bearer token", "content-type": "application/json" },
    body: JSON.stringify({
      schema_version: 1,
      agent_id: "agent-1",
      entitlements: [{
        observed_at: "2026-08-01T00:00:00Z",
        provider: "codex",
        account_id: "account-1",
        window: "weekly",
        used_percent: 10,
        resets_at: "2026-08-08T00:00:00Z",
        duration_minutes: 10080,
        source: "test",
      }],
      usage: [],
      reports: REPORTS,
    }),
  }), env(db));

  assert.equal(response.status, 200);
  assert.deepEqual((await response.json()).accepted, {
    entitlements: 1,
    usage: 0,
    reports: 5,
  });
  assert.equal(db.bound.some((statement) => statement.sql.includes("INSERT INTO usage_rows")), false);
  assert.equal(db.state.dirty, 1);
  assert.equal(db.pooled, null);
  assert.equal(db.queries.some(({ sql }) => sql.includes("JOIN agents")), false);

  for (const kind of Object.keys(REPORTS)) {
    const publicResponse = await worker.fetch(request(`/api/${kind}`), env(db));
    const publicBody = await publicResponse.json();
    delete publicBody.generated_at;
    assert.equal(publicResponse.status, 200);
    assert.deepEqual(publicBody, REPORTS[kind]);
  }
});

test("health does not count raw usage rows and the footer does not show them", async () => {
  const db = new FakeD1();
  const response = await worker.fetch(request("/api/health"), env(db));
  const body = await response.json();
  // The site's polling lives in one module that every page uses.
  const page = readFileSync(new URL("../web/src/app/data.ts", import.meta.url), "utf8");

  assert.equal(response.status, 200);
  assert.equal("usage_rows" in body, false);
  assert.equal(db.healthSql.includes("COUNT(*) FROM usage_rows"), false);
  assert.equal(db.healthSql.includes("COUNT(*) FROM entitlement_snapshots"), false);
  assert.equal(db.healthSql.includes("MAX(rowid) FROM entitlement_snapshots"), true);
  assert.equal(page.includes("usage_rows"), false);
  assert.equal(page.includes("document.hidden"), true);
  assert.equal(page.includes("setInterval(refresh, 60000)"), false);
  assert.equal(page.includes("5 * 60 * 1000"), true);
});

test("the plan reported beside a meter is stored, and its absence is not guessed", async () => {
  const db = new FakeD1();
  const send = (plan) => worker.fetch(request("/ingest", {
    method: "POST",
    headers: { authorization: "Bearer token", "content-type": "application/json" },
    body: JSON.stringify({
      schema_version: 1,
      agent_id: "agent-1",
      entitlements: [{
        observed_at: plan === null ? "2026-08-02T00:00:00Z" : "2026-08-01T00:00:00Z",
        provider: "codex",
        account_id: "account-1",
        window: "weekly",
        used_percent: 10,
        resets_at: "2026-08-08T00:00:00Z",
        duration_minutes: 10080,
        source: "test",
        ...(plan === undefined ? {} : { plan }),
      }],
      usage: [],
    }),
  }), env(db));

  assert.equal((await send("plus")).status, 200);
  assert.equal((await send(null)).status, 200);
  assert.equal((await send(undefined)).status, 200);

  const inserts = db.bound.filter((statement) =>
    statement.sql.includes("INSERT INTO entitlement_snapshots"));
  assert.equal(inserts.length, 3);
  assert.equal(db.state, null);
  // The plan is the last bound parameter of the upsert.
  assert.deepEqual(inserts.map((statement) => statement.params.at(-1)), ["plus", null, null]);
});

test("account ids are hashed on arrival, the same way the collector hashes them", async () => {
  // subbench.privacy.pseudonym("7f3c2a10-0d4e-4b7a-9c1e-5a6b7c8d9e0f")
  assert.equal(await pseudonym("7f3c2a10-0d4e-4b7a-9c1e-5a6b7c8d9e0f"), "acct-0b8bdf4cd97df362");
  const db = new FakeD1();
  const raw = "00000000-1111-4222-8333-444444444444";
  const reports = { ...REPORTS, history: { windows: [{ account_id: raw, product: "ChatGPT Plus" }] } };
  const response = await worker.fetch(request("/ingest", {
    method: "POST",
    headers: { authorization: "Bearer token", "content-type": "application/json" },
    body: JSON.stringify({
      schema_version: 1, agent_id: "agent-1", usage: [], reports,
      entitlements: [{ observed_at: "2026-08-01T00:00:00Z", provider: "codex", account_id: raw, window: "weekly",
        used_percent: 10, resets_at: "2026-08-08T00:00:00Z", duration_minutes: 10080, source: "test" }],
    }),
  }), env(db));
  assert.equal(response.status, 200);
  const stored = JSON.stringify(db.bound.map((statement) => statement.params));
  assert.equal(stored.includes(raw), false);
  assert.equal(stored.includes(await pseudonym(raw)), true);
});

function addHistory(db, id, payload, ageDays = 0) {
  db.agents.set(id, new Date(Date.now() - ageDays * 86400000).toISOString());
  db.agentReports.set(id, payload);
  db.reports.history = payload;
}

function historyWindow(account, estimate) {
  return {
    provider: "codex", account_id: account, plan: "plus", window: "weekly",
    reset_key: new Date(Date.now() - 86400000).toISOString(), tier: "confirmed",
    estimate_usd: estimate, lower_usd: estimate / 2, upper_usd: estimate * 2,
    covered_quota_percent: 50, latest_observed_at: new Date().toISOString(),
  };
}

test("a clean pool makes no report query", async () => {
  for (const state of [null, { dirty: 0 }]) {
    const db = new FakeD1();
    db.state = state;
    await poolIfDirty(env(db));
    assert.equal(db.queries.length, 1);
    assert.equal(db.executed.length, 0);
  }
});

test("zero or one active collector removes a previous pooled report", async () => {
  for (const count of [0, 1]) {
    const db = new FakeD1();
    db.state = { dirty: 1, revision: 0 };
    db.pooled = { payload: "{}", generated_at: "old" };
    addHistory(db, "expired", { windows: [] }, 36);
    if (count) addHistory(db, "active", { windows: [] });
    await poolIfDirty(env(db));
    assert.equal(db.pooled, null);
    assert.equal(db.state.dirty, 0);
    assert.ok(db.state.pooled_at);
    const response = await worker.fetch(request("/api/history"), env(db));
    assert.deepEqual(await response.json(), { windows: [], generated_at: "2026-08-01T00:00:00Z" });
  }
});

test("two active collectors store a pooled report and history serves it", async () => {
  const db = new FakeD1();
  db.state = { dirty: 1, revision: 0 };
  const first = { windows: [historyWindow("acct-one", 100)] };
  const second = { windows: [historyWindow("acct-two", 200)] };
  addHistory(db, "one", first, 1);
  addHistory(db, "two", second);
  addHistory(db, "expired", { windows: [historyWindow("acct-expired", 999)] }, 36);
  await poolIfDirty(env(db));
  assert.deepEqual(JSON.parse(db.pooled.payload), poolHistory([second, first], new Date(db.pooled.generated_at)));
  assert.equal(db.state.dirty, 0);
  assert.equal(db.queries.filter(({ sql }) => sql.includes("JOIN agents")).length, 1);
  assert.equal(db.queries.find(({ sql }) => sql.includes("JOIN agents")).params[1], 500);
  const response = await worker.fetch(request("/api/history"), env(db));
  assert.deepEqual(await response.json(), { ...JSON.parse(db.pooled.payload), generated_at: db.pooled.generated_at });
});

test("POOL_MAX_AGENTS selects the most recently seen collectors", async () => {
  const db = new FakeD1();
  db.state = { dirty: 1, revision: 0 };
  addHistory(db, "oldest", { windows: [historyWindow("acct-old", 999)] }, 2);
  addHistory(db, "middle", { windows: [historyWindow("acct-middle", 100)] }, 1);
  addHistory(db, "newest", { windows: [historyWindow("acct-new", 200)] });
  await poolIfDirty({ ...env(db), POOL_MAX_AGENTS: "2" });
  const payload = JSON.parse(db.pooled.payload);
  assert.equal(payload.agent_count, 2);
  // Only the two newest collectors went in: the oldest one's 999 would have moved nothing,
  // but its account would raise the count to 3.
  assert.equal(payload.products[0].account_count, 2);
  assert.equal(payload.products[0].estimate_usd >= 100 && payload.products[0].estimate_usd <= 200, true);
});

test("a history push during pooling keeps its dirty mark and the previous pool", async () => {
  for (const agents of [0, 2]) {
    const db = new FakeD1();
    db.state = { dirty: 1, revision: 0 };
    db.pooled = { payload: '{"marker":"previous"}', generated_at: "old" };
    for (let i = 0; i < agents; i++) addHistory(db, `agent-${i}`, { windows: [] });
    const originalAll = db.all.bind(db);
    db.all = async (...args) => {
      const rows = await originalAll(...args);
      const response = await worker.fetch(request("/ingest", {
        method: "POST",
        headers: { authorization: "Bearer token", "content-type": "application/json" },
        body: JSON.stringify({ schema_version: 1, agent_id: "concurrent", reports: { history: { windows: [] } } }),
      }), env(db));
      assert.equal(response.status, 200);
      return rows;
    };
    await poolIfDirty(env(db));
    assert.deepEqual(db.state, { dirty: 1, revision: 1 });
    assert.deepEqual(db.pooled, { payload: '{"marker":"previous"}', generated_at: "old" });
  }
});

test("only the daily cron prunes usage and ingest logs", async () => {
  const daily = new FakeD1();
  await worker.scheduled({ cron: "0 4 * * *" }, env(daily));
  assert.deepEqual(daily.executed.map(({ sql }) => sql), [
    "DELETE FROM usage_rows WHERE last_seen_at < ?", "DELETE FROM ingest_log WHERE received_at < ?",
  ]);
  assert.equal(daily.queries.length, 0);
  const frequent = new FakeD1();
  frequent.state = { dirty: 1, revision: 0 };
  await worker.scheduled({ cron: "*/10 * * * *" }, env(frequent));
  assert.equal(frequent.state.dirty, 0);
  assert.equal(frequent.executed.some(({ sql }) => /DELETE FROM (usage_rows|ingest_log)/.test(sql)), false);
});

test("history uses the newest stored report when no pooled report exists", async () => {
  const db = new FakeD1();
  const newest = { windows: [], marker: "newest collector" };
  db.reports.history = newest;
  const response = await worker.fetch(request("/api/history"), env(db));
  assert.deepEqual(await response.json(), { ...newest, generated_at: "2026-08-01T00:00:00Z" });
  assert.equal(db.queries.length, 2);
  assert.ok(db.queries[1].sql.includes("ORDER BY generated_at DESC LIMIT 1"));
});

test("history falls back only when the pool table is absent", async () => {
  const db = new FakeD1();
  db.reports.history = { windows: [], marker: "stored" };
  db.missingPoolTable = true;
  let response = await worker.fetch(request("/api/history"), env(db));
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ...db.reports.history, generated_at: "2026-08-01T00:00:00Z" });
  db.missingPoolTable = false;
  db.poolReadError = new Error("database unavailable");
  response = await worker.fetch(request("/api/history"), env(db));
  assert.equal(response.status, 503);
});

test("an unknown cron does nothing", async () => {
  const db = new FakeD1();
  await worker.scheduled({ cron: "unexpected" }, env(db));
  assert.deepEqual(db.queries, []);
  assert.deepEqual(db.executed, []);
});

test("ingest stores the compact pool input and logs no accepted push", async () => {
  const db = new FakeD1();
  const window = { ...historyWindow("acct-one", 50), extra_field: "not needed by pooling" };
  const response = await worker.fetch(request("/ingest", {
    method: "POST",
    headers: { authorization: "Bearer token", "content-type": "application/json" },
    body: JSON.stringify({ schema_version: 1, agent_id: "agent-1", entitlements: [], usage: [],
      reports: { history: { windows: [window] } } }),
  }), env(db));
  assert.equal(response.status, 200);
  const stored = db.agentReports.get("agent-1");
  assert.equal(stored.windows.length, 1);
  assert.equal("extra_field" in stored.windows[0], false);
  assert.equal(db.bound.some((statement) => statement.sql.includes("INSERT INTO ingest_log")), false);
});

test("a rejected history ingest does not mark the pool dirty", async () => {
  const db = new FakeD1();
  const response = await worker.fetch(request("/ingest", {
    method: "POST",
    headers: { authorization: "Bearer token", "content-type": "application/json" },
    body: JSON.stringify({ schema_version: 1, agent_id: "agent", reports: { history: {} },
      entitlements: [{ provider: "codex", used_percent: 101 }] }),
  }), env(db));
  assert.equal(response.status, 400);
  assert.equal(db.state, null);
  assert.equal(db.executed.some(({ sql }) => sql.includes("INSERT INTO pool_state")), false);
});
