/**
 * SubBench dashboard Worker.
 *
 * Stores pushed evidence and serves the reports the agent computed. It deliberately
 * contains no estimator logic: quota-span weighting, unobserved-usage exclusion,
 * reset-boundary clustering and the staleness bound all live in subbench/regression.py,
 * and a second implementation here would drift from it silently with no way to tell which
 * copy was right.
 *
 * Validation is a different thing and does live here. Checking that a percentage is a
 * percentage and a token count is not negative duplicates no derivation, and it is the
 * only thing standing between a malformed push and every estimate derived from that
 * window afterwards.
 */

const SCHEMA_VERSION = 1;
const MAX_USAGE_ROWS = 5000;
const MAX_ENTITLEMENT_ROWS = 5000;
const PROVIDERS = new Set(["claude", "codex"]);
const REPORT_KINDS = new Set(["current", "history", "models", "weights"]);
const TOKEN_FIELDS = [
  "input_tokens", "cached_input_tokens", "cache_write_tokens",
  "cache_read_tokens", "output_tokens", "reasoning_output_tokens",
];

const json = (payload, status = 200) =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

class Invalid extends Error {
  constructor(status, detail) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

const text = (value, field, { allowNull = false } = {}) => {
  if (value === null || value === undefined) {
    if (allowNull) return null;
    throw new Invalid(400, `${field} must not be null`);
  }
  if (typeof value !== "string" || !value.trim()) {
    throw new Invalid(400, `${field} must be a non-empty string`);
  }
  return value;
};

const count = (value, field) => {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Invalid(400, `${field} must be a number`);
  }
  if (value < 0) throw new Invalid(400, `${field} must not be negative`);
  return Math.trunc(value);
};

const decimalText = (value, field) => {
  if (value === null || value === undefined) return null;
  const raw = String(value);
  if (!/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(raw)) {
    throw new Invalid(400, `${field} must be a decimal string`);
  }
  return raw;
};

function entitlementRow(row, agentId) {
  const provider = text(row.provider, "provider");
  if (!PROVIDERS.has(provider)) throw new Invalid(400, `unknown provider: ${provider}`);

  const used = row.used_percent;
  if (typeof used !== "number" || !Number.isFinite(used)) {
    throw new Invalid(400, "used_percent must be a number");
  }
  // A meter outside 0-100 is a parse error at the source. Stored, one bad reading would
  // dominate every span-weighted slope in its window.
  if (used < 0 || used > 100) throw new Invalid(400, `used_percent out of range: ${used}`);

  const accountId = text(row.account_id, "account_id", { allowNull: true });
  return [
    agentId,
    text(row.observed_at, "observed_at"),
    provider,
    accountId,
    accountId ?? "",
    text(row.window, "window"),
    used,
    text(row.resets_at, "resets_at", { allowNull: true }),
    row.duration_minutes === null || row.duration_minutes === undefined
      ? null
      : count(row.duration_minutes, "duration_minutes"),
    text(row.source, "source", { allowNull: true }) ?? "pushed",
  ];
}

function usageRow(row, agentId) {
  const provider = text(row.provider, "provider");
  if (!PROVIDERS.has(provider)) throw new Invalid(400, `unknown provider: ${provider}`);
  const accountId = text(row.account_id, "account_id", { allowNull: true });
  const model = text(row.model, "model", { allowNull: true });
  const lastSeen = text(row.last_seen_at, "last_seen_at");
  return [
    agentId,
    text(row.import_key, "import_key"),
    text(row.imported_at, "imported_at", { allowNull: true }) ?? lastSeen,
    lastSeen,
    provider,
    accountId,
    accountId ?? "",
    text(row.period_start, "period_start", { allowNull: true }),
    model,
    model ?? "",
    ...TOKEN_FIELDS.map((field) => count(row[field] ?? 0, field)),
    decimalText(row.reported_cost_usd, "reported_cost_usd"),
    text(row.source_path, "source_path"),
  ];
}

const ENTITLEMENT_UPSERT = `
INSERT INTO entitlement_snapshots
  (agent_id, observed_at, provider, account_id, account_key, window,
   used_percent, resets_at, duration_minutes, source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (agent_id, provider, account_key, window, observed_at) DO UPDATE SET
  used_percent = excluded.used_percent,
  resets_at = excluded.resets_at,
  duration_minutes = excluded.duration_minutes`;

const USAGE_UPSERT = `
INSERT INTO usage_rows
  (agent_id, import_key, imported_at, last_seen_at, provider, account_id, account_key,
   period_start, model, model_key, input_tokens, cached_input_tokens,
   cache_write_tokens, cache_read_tokens, output_tokens, reasoning_output_tokens,
   reported_cost_usd, source_path)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (agent_id, import_key, source_path, model_key) DO UPDATE SET
  last_seen_at = excluded.last_seen_at,
  reported_cost_usd = excluded.reported_cost_usd`;

const REPORT_UPSERT = `
INSERT INTO reports (agent_id, kind, generated_at, payload) VALUES (?, ?, ?, ?)
ON CONFLICT (agent_id, kind) DO UPDATE SET
  generated_at = excluded.generated_at, payload = excluded.payload`;

const AGENT_UPSERT = `
INSERT INTO agents (agent_id, label, first_seen, last_seen) VALUES (?, NULL, ?, ?)
ON CONFLICT (agent_id) DO UPDATE SET last_seen = excluded.last_seen`;

function parse(payload) {
  if (typeof payload !== "object" || payload === null) {
    throw new Invalid(400, "payload must be an object");
  }
  const version = payload.schema_version;
  if (!Number.isInteger(version)) throw new Invalid(400, "schema_version must be an integer");
  // Fail loudly rather than storing rows this Worker would misread.
  if (version > SCHEMA_VERSION) {
    throw new Invalid(409, `schema_version ${version} unsupported, this server speaks ${SCHEMA_VERSION}`);
  }
  if (version < SCHEMA_VERSION) throw new Invalid(400, `schema_version ${version} is no longer accepted`);

  const agentId = text(payload.agent_id, "agent_id");
  const entitlements = payload.entitlements ?? [];
  const usage = payload.usage ?? [];
  const reports = payload.reports ?? {};
  if (!Array.isArray(entitlements) || !Array.isArray(usage)) {
    throw new Invalid(400, "entitlements and usage must be arrays");
  }
  if (usage.length > MAX_USAGE_ROWS) {
    throw new Invalid(413, `${usage.length} usage rows exceeds the ${MAX_USAGE_ROWS} limit`);
  }
  if (entitlements.length > MAX_ENTITLEMENT_ROWS) {
    throw new Invalid(413, `${entitlements.length} entitlement rows exceeds the ${MAX_ENTITLEMENT_ROWS} limit`);
  }
  for (const kind of Object.keys(reports)) {
    if (!REPORT_KINDS.has(kind)) throw new Invalid(400, `unknown report kind: ${kind}`);
  }

  return {
    agentId,
    entitlements: entitlements.map((row) => entitlementRow(row, agentId)),
    usage: usage.map((row) => usageRow(row, agentId)),
    reports,
    cursor: {
      entitlement_cursor: entitlements.length ? entitlements[entitlements.length - 1].observed_at : null,
      usage_cursor: usage.length ? usage[usage.length - 1].last_seen_at : null,
    },
  };
}

async function ingest(request, env) {
  const expected = env.SUBBENCH_INGEST_TOKEN;
  if (!expected || request.headers.get("authorization") !== `Bearer ${expected}`) {
    return json({ error: "unauthorized" }, 401);
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return json({ error: "body must be JSON" }, 400);
  }

  let batch;
  try {
    batch = parse(payload);
  } catch (error) {
    if (!(error instanceof Invalid)) throw error;
    await env.DB.prepare(
      "INSERT INTO ingest_log (received_at, agent_id, status, detail) VALUES (?, ?, ?, ?)"
    ).bind(new Date().toISOString(), payload?.agent_id ?? null, error.status, error.detail).run();
    return json({ error: error.detail }, error.status);
  }

  const now = new Date().toISOString();
  const statements = [env.DB.prepare(AGENT_UPSERT).bind(batch.agentId, now, now)];
  for (const row of batch.entitlements) statements.push(env.DB.prepare(ENTITLEMENT_UPSERT).bind(...row));
  for (const row of batch.usage) statements.push(env.DB.prepare(USAGE_UPSERT).bind(...row));
  for (const [kind, report] of Object.entries(batch.reports)) {
    statements.push(env.DB.prepare(REPORT_UPSERT).bind(batch.agentId, kind, now, JSON.stringify(report)));
  }
  // One batch. A partial write would leave quota advancing against spend that was never
  // stored, which the estimator reads as unobserved usage and discards.
  await env.DB.batch(statements);
  await env.DB.prepare(
    "INSERT INTO ingest_log (received_at, agent_id, status, detail) VALUES (?, ?, ?, ?)"
  ).bind(now, batch.agentId, 200, null).run();

  return json({
    accepted: {
      entitlements: batch.entitlements.length,
      usage: batch.usage.length,
      reports: Object.keys(batch.reports).length,
    },
    cursor: batch.cursor,
  });
}

async function report(env, kind) {
  // Newest agent wins. Pooling several agents means recomputing over combined pairs,
  // which cannot be reconstructed from per-agent reports, so it needs derivation here
  // rather than a different query.
  const row = await env.DB.prepare(
    "SELECT payload, generated_at FROM reports WHERE kind = ? ORDER BY generated_at DESC LIMIT 1"
  ).bind(kind).first();
  if (!row) return json({ [kind]: [], generated_at: null });
  return json({ ...JSON.parse(row.payload), generated_at: row.generated_at });
}

async function health(env) {
  const row = await env.DB.prepare(`
    SELECT (SELECT COUNT(*) FROM entitlement_snapshots) AS entitlement_rows,
           (SELECT COUNT(*) FROM usage_rows) AS usage_rows,
           (SELECT COUNT(*) FROM agents) AS agents,
           (SELECT MAX(last_seen) FROM agents) AS last_ingest_at,
           (SELECT MAX(generated_at) FROM reports) AS last_report_at,
           (SELECT COUNT(*) FROM ingest_log WHERE status >= 400) AS rejected`).first();
  return json({ schema_version: SCHEMA_VERSION, now: new Date().toISOString(), ...row });
}

export default {
  async fetch(request, env) {
    const path = new URL(request.url).pathname.replace(/\/$/, "") || "/";

    if (request.method === "POST" && path === "/ingest") return ingest(request, env);
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405);

    try {
      if (path === "/api/current") return await report(env, "current");
      if (path === "/api/history") return await report(env, "history");
      if (path === "/api/models") return await report(env, "models");
      if (path === "/api/weights") return await report(env, "weights");
      if (path === "/api/health") return await health(env);
    } catch (error) {
      // A read failure must not render a stale number as current.
      return json({ error: `${error.name}: ${error.message}` }, 503);
    }

    return env.ASSETS.fetch(request);
  },

  async scheduled(event, env) {
    // Usage rows are prunable once their window is long settled; entitlement snapshots
    // never are, because an unsampled moment cannot be recovered.
    const days = Number(env.USAGE_RETENTION_DAYS ?? 90);
    const cutoff = new Date(Date.now() - days * 86400000).toISOString();
    await env.DB.batch([
      env.DB.prepare("DELETE FROM usage_rows WHERE last_seen_at < ?").bind(cutoff),
      env.DB.prepare("DELETE FROM ingest_log WHERE received_at < ?").bind(cutoff),
    ]);
  },
};
