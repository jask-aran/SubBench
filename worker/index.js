/**
 * SubBench dashboard Worker.
 *
 * Stores pushed evidence and serves the reports the collectors computed. Estimation stays
 * on the collector: quota-span weighting, unobserved-usage exclusion, reset-boundary
 * clustering and the staleness bound live in subbench/regression.py, and a second
 * implementation here would drift from it silently. With several collectors, the Worker
 * only merges their windows and pools their finished estimates (worker/pooling.js), at
 * most once per 10 minutes.
 *
 * Validation is a different thing and does live here. Checking that a percentage is a
 * percentage and a token count is not negative duplicates no derivation, and it is the
 * only thing standing between a malformed push and every estimate derived from that
 * window afterwards.
 */
import { poolHistory, poolInput } from "./pooling.js";

const SCHEMA_VERSION = 1;
const MAX_USAGE_ROWS = 5000;
const MAX_ENTITLEMENT_ROWS = 5000;
const PROVIDERS = new Set(["claude", "codex"]);
const REPORT_KINDS = new Set(["current", "history", "models", "weights", "series"]);
const TOKEN_FIELDS = [
  "input_tokens", "cached_input_tokens", "cache_write_tokens",
  "cache_read_tokens", "output_tokens", "reasoning_output_tokens",
];

const json = (payload, status = 200) =>
  new Response(JSON.stringify(payload), {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });

/*
 * Account ids are hashed on arrival, with the scheme the collector uses
 * (subbench/privacy.py), so the database never holds a provider's own account id even
 * when a collector from before 0.3.0 sends one. A value already hashed is left alone.
 */
export async function pseudonym(accountId) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`subbench:v1:${accountId}`));
  return "acct-" + [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 16);
}

// Also matches the escaped form inside report_text, where a report is a JSON string.
const UNHASHED_ACCOUNT = /\\?"account_id\\?"\s*:\s*\\?"(?!acct-)/;
const TEXT_KINDS = new Set([...REPORT_KINDS, "pool_input"]);
const BASE64 = /^[A-Za-z0-9+/]+={0,2}$/;
// A stored report that is gzip, kept as base64 text after this prefix.
const GZ = "gz:";

export async function pseudonymise(value, cache = new Map()) {
  if (Array.isArray(value)) {
    const out = [];
    for (const item of value) out.push(await pseudonymise(item, cache));
    return out;
  }
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, item] of Object.entries(value)) {
      if (key === "account_id" && typeof item === "string" && item && !item.startsWith("acct-")) {
        if (!cache.has(item)) cache.set(item, await pseudonym(item));
        out[key] = cache.get(item);
      } else {
        out[key] = await pseudonymise(item, cache);
      }
    }
    return out;
  }
  return value;
}

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
    // Older collectors do not send a plan. Null is a product in its own right, so an
    // absent plan is stored as absent rather than guessed.
    text(row.plan, "plan", { allowNull: true }),
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
   used_percent, resets_at, duration_minutes, source, plan)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (agent_id, provider, account_key, window, observed_at) DO UPDATE SET
  used_percent = excluded.used_percent,
  resets_at = excluded.resets_at,
  duration_minutes = excluded.duration_minutes,
  plan = excluded.plan`;

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

// agents.readings counts the agent's stored readings, so /api/health does not count the
// snapshot table. The agents row is written on every push anyway, so the count costs no
// extra row write. This statement runs before the snapshot upserts in the same batch, and
// adds only the keys that are not stored yet, so a re-sent reading is not counted again.
// ?3 is a JSON array of [observed_at, provider, account_key, window].
const AGENT_UPSERT = `
INSERT INTO agents (agent_id, label, first_seen, last_seen, readings)
SELECT ?1, NULL, ?2, ?2, COUNT(DISTINCT j.value) FROM json_each(?3) AS j
WHERE NOT EXISTS (
  SELECT 1 FROM entitlement_snapshots AS e
  WHERE e.agent_id = ?1 AND e.provider = json_extract(j.value, '$[1]')
    AND e.account_key = json_extract(j.value, '$[2]') AND e.window = json_extract(j.value, '$[3]')
    AND e.observed_at = json_extract(j.value, '$[0]'))
ON CONFLICT (agent_id) DO UPDATE SET
  last_seen = excluded.last_seen, readings = readings + excluded.readings`;

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
  const reportText = payload.report_text ?? {};
  if (typeof reportText !== "object" || reportText === null || Array.isArray(reportText)) {
    throw new Invalid(400, "report_text must be an object");
  }
  for (const [kind, value] of Object.entries(reportText)) {
    if (!TEXT_KINDS.has(kind)) throw new Invalid(400, `unknown report kind: ${kind}`);
    if (typeof value !== "string") throw new Invalid(400, `report_text.${kind} must be a JSON string`);
  }
  const reportGz = payload.report_gz ?? {};
  if (typeof reportGz !== "object" || reportGz === null || Array.isArray(reportGz)) {
    throw new Invalid(400, "report_gz must be an object");
  }
  for (const [kind, value] of Object.entries(reportGz)) {
    if (!TEXT_KINDS.has(kind)) throw new Invalid(400, `unknown report kind: ${kind}`);
    // Checked as base64 only; the gzip itself is never opened here (see report()).
    if (typeof value !== "string" || !value || !BASE64.test(value)) {
      throw new Invalid(400, `report_gz.${kind} must be base64 gzip`);
    }
  }

  return {
    agentId,
    entitlements: entitlements.map((row) => entitlementRow(row, agentId)),
    usage: usage.map((row) => usageRow(row, agentId)),
    reports,
    reportText,
    reportGz,
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
  let body;
  try {
    body = await request.text();
    payload = JSON.parse(body);
  } catch {
    return json({ error: "body must be JSON" }, 400);
  }

  // A collector from 0.3.0 on hashes every account id itself. Walking a 700 KB payload to
  // find none cost over half of an ingest's CPU, so walk only when the raw text holds an
  // account id without the hash prefix.
  if (UNHASHED_ACCOUNT.test(body)) {
    payload = await pseudonymise(payload);
    if (payload?.report_text && typeof payload.report_text === "object") {
      for (const [kind, value] of Object.entries(payload.report_text)) {
        if (typeof value !== "string") continue;
        try {
          payload.report_text[kind] = JSON.stringify(await pseudonymise(JSON.parse(value)));
        } catch {
          return json({ error: `report_text.${kind} must be JSON` }, 400);
        }
      }
    }
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
  const keys = JSON.stringify(batch.entitlements.map((row) => [row[1], row[2], row[4], row[5]]));
  const statements = [env.DB.prepare(AGENT_UPSERT).bind(batch.agentId, now, keys)];
  for (const row of batch.entitlements) statements.push(env.DB.prepare(ENTITLEMENT_UPSERT).bind(...row));
  for (const row of batch.usage) statements.push(env.DB.prepare(USAGE_UPSERT).bind(...row));
  // Reports sent as text are stored as they are: parsing and serialising 700 KB of report
  // objects was most of a push's CPU time. Older collectors still send objects.
  // Gzipped reports (report_gz) come from collectors that hash every account id
  // themselves; the Worker stores them unopened, which is what keeps a push cheap.
  const texts = Object.fromEntries(Object.entries(batch.reportGz).map(([kind, value]) => [kind, GZ + value]));
  for (const [kind, value] of Object.entries(batch.reportText)) {
    if (!Object.hasOwn(texts, kind)) texts[kind] = value;
  }
  for (const [kind, report] of Object.entries(batch.reports)) {
    if (!Object.hasOwn(texts, kind)) texts[kind] = JSON.stringify(report);
  }
  const hasHistory = Object.hasOwn(texts, "history");
  if (hasHistory && !Object.hasOwn(texts, "pool_input")) {
    // The fields pooling reads, stored beside the full report so a pass parses only these.
    let history = batch.reports.history;
    if (history === undefined) {
      try {
        history = JSON.parse(await storedText(texts.history));
      } catch {
        return json({ error: "history must be JSON" }, 400);
      }
    }
    texts.pool_input = JSON.stringify(poolInput(history));
  }
  for (const [kind, text] of Object.entries(texts)) {
    statements.push(env.DB.prepare(REPORT_UPSERT).bind(batch.agentId, kind, now, text));
  }
  if (hasHistory) {
    statements.push(env.DB.prepare(
      "INSERT INTO pool_state (id, dirty) VALUES (1, 1) ON CONFLICT (id) DO UPDATE SET dirty = 1, revision = revision + 1"
    ));
  }
  // One batch. A partial write would leave quota advancing against spend that was never
  // stored, which the estimator reads as unobserved usage and discards.
  // Accepted pushes are not logged: nothing reads them (health counts rejections, and
  // agents.last_seen dates the last push), and each was a D1 write per push.
  await env.DB.batch(statements);

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
  if (kind === "history") {
    try {
      const pooled = await env.DB.prepare(
        "SELECT payload, generated_at FROM pooled_reports WHERE kind = ?"
      ).bind(kind).first();
      if (pooled) return served(pooled.payload, pooled.generated_at);
    } catch (error) {
      if (!/no such table: (?:main\.)?pooled_reports\b/i.test(String(error.message))) throw error;
    }
  }
  const row = await env.DB.prepare(
    "SELECT payload, generated_at FROM reports WHERE kind = ? ORDER BY generated_at DESC LIMIT 1"
  ).bind(kind).first();
  if (!row) return json({ [kind]: [], generated_at: null });
  return served(row.payload, row.generated_at);
}

function fromBase64(text) {
  if (typeof Uint8Array.fromBase64 === "function") return Uint8Array.fromBase64(text);
  const binary = atob(text);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

// The JSON text of a stored report, opening it if it is gzip. Only pooling and an
// ingest without pool_input need the text; serving never does.
async function storedText(payload) {
  if (!payload.startsWith(GZ)) return payload;
  const stream = new Blob([fromBase64(payload.slice(GZ.length))]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Response(stream).text();
}

// A stored report as the response. Gzip goes out as it is, for the browser to open, so a
// page view parses nothing; parsing and serialising a 340 KB report took 7 ms of CPU.
// A text report gets its generated_at spliced in rather than parsed and rebuilt.
function served(payload, generatedAt) {
  const headers = { "content-type": "application/json; charset=utf-8" };
  if (payload.startsWith(GZ)) {
    return new Response(fromBase64(payload.slice(GZ.length)),
      { headers: { ...headers, "content-encoding": "gzip" }, encodeBody: "manual" });
  }
  const body = payload.trimEnd();
  const rest = body.length > 2 ? "," : "";
  return new Response(`${body.slice(0, -1)}${rest}"generated_at":${JSON.stringify(generatedAt)}}`, { headers });
}

export async function poolIfDirty(env) {
  const state = await env.DB.prepare("SELECT dirty, revision FROM pool_state WHERE id = 1").first();
  if (!state?.dirty) return;
  const now = new Date();
  const cutoff = new Date(+now - 35 * 86400000).toISOString();
  // POOL_MAX_AGENTS bounds one pooling pass, independent of the push rate.
  const configured = Number(env.POOL_MAX_AGENTS ?? 500);
  const limit = Number.isInteger(configured) && configured > 0 ? configured : 500;
  const { results } = await env.DB.prepare(`
    SELECT reports.payload FROM reports JOIN agents ON agents.agent_id = reports.agent_id
    WHERE reports.kind = 'pool_input' AND agents.last_seen >= ?
    ORDER BY agents.last_seen DESC LIMIT ?
  `).bind(cutoff, limit).all();
  const generatedAt = now.toISOString();
  const inputs = results.length < 2 ? [] : await Promise.all(results.map(async (row) => JSON.parse(await storedText(row.payload))));
  const statement = results.length < 2
    ? env.DB.prepare(`
        DELETE FROM pooled_reports WHERE kind = ?
        AND EXISTS (SELECT 1 FROM pool_state WHERE id = 1 AND dirty = 1 AND revision = ?)
      `).bind("history", state.revision)
    : env.DB.prepare(`
        INSERT INTO pooled_reports (kind, generated_at, payload)
        SELECT ?, ?, ? WHERE EXISTS
          (SELECT 1 FROM pool_state WHERE id = 1 AND dirty = 1 AND revision = ?)
        ON CONFLICT (kind) DO UPDATE SET
          generated_at = excluded.generated_at, payload = excluded.payload
      `).bind("history", generatedAt,
        JSON.stringify(poolHistory(inputs, now)), state.revision);
  await env.DB.batch([
    statement,
    // A new history push changes revision, even within the same millisecond.
    env.DB.prepare(`
      UPDATE pool_state SET dirty = 0, pooled_at = ? WHERE id = 1 AND dirty = 1 AND revision = ?
    `).bind(generatedAt, state.revision),
  ]);
}

async function health(env) {
  // COUNT(*) would read every snapshot, and this route is polled. The snapshot table has no
  // rowid (WITHOUT ROWID), so the count comes from agents.readings (see AGENT_UPSERT).
  const row = await env.DB.prepare(`
    SELECT (SELECT COALESCE(SUM(readings), 0) FROM agents) AS entitlement_rows,
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
      if (path === "/api/series") return await report(env, "series");
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
    if (event.cron === "*/10 * * * *") return poolIfDirty(env);
    if (event.cron !== "0 4 * * *") return;
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
