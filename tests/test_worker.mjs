import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

import worker from "../worker/index.js";

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
  }

  prepare(sql) {
    this.healthSql = sql.includes("COUNT(*) FROM entitlement_snapshots") ? sql : this.healthSql;
    return {
      bind: (...params) => {
        const statement = { sql, params };
        this.bound.push(statement);
        return Object.assign({ sql, params }, {
          first: async () => this.first(sql, params),
          run: async () => this.run(sql, params),
        });
      },
      first: async () => this.first(sql, []),
    };
  }

  async first(sql, params) {
    if (sql.includes("SELECT payload, generated_at")) {
      const payload = this.reports[params[0]];
      return payload ? { payload: JSON.stringify(payload), generated_at: "2026-08-01T00:00:00Z" } : null;
    }
    if (sql.includes("COUNT(*) FROM entitlement_snapshots")) {
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

  async run() {
    return {};
  }

  async batch(statements) {
    for (const statement of statements) {
      if (statement.sql.includes("INSERT INTO reports")) {
        this.reports[statement.params[1]] = JSON.parse(statement.params[3]);
      }
    }
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
  const page = readFileSync(new URL("../src/subbench/server/static/index.html", import.meta.url), "utf8");

  assert.equal(response.status, 200);
  assert.equal("usage_rows" in body, false);
  assert.equal(db.healthSql.includes("COUNT(*) FROM usage_rows"), false);
  assert.equal(page.includes("usage_rows"), false);
});
