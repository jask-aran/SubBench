# SubBench web dashboard — design

Date: 2026-07-31
Status: proposed

## Purpose

Publish SubBench's entitlement-value estimates as a live web page on Cloudflare, fed by
the local collector. Every displayed number carries an explicit confidence tier, so the
page is useful from the first hour of collection without ever implying more certainty
than the evidence supports.

This is the first of three sub-projects. The other two — per-model quota weights, and a
Pareto frontier over model × subscription — are out of scope here and are described only
where they constrain this design.

## Goals

- A public URL showing current API-equivalent entitlement value per provider and window.
- Estimates derived **server-side** from pushed evidence, so ingest can be validated and
  an estimator improvement re-derives all stored history without agent involvement.
- One estimator implementation. The server imports the same `subbench.regression` code
  the CLI uses.
- Graded confidence rather than suppression: nothing is hidden, everything is labelled.

## Non-goals

- Multi-user aggregation. The schema is designed not to preclude it; nothing implements it.
- Model × subscription Pareto analysis. It depends on per-model quota weights, which need
  many reset windows with varying model mix, and on benchmark data that lives in another
  repository.
- Re-parsing pushed payloads. Raw ccusage JSON is not sent, so `normalise_payload`
  improvements remain local-only. See "Accepted losses".
- Authentication for reading. The page is public.

## Architecture

```
subbench/                    existing CLI and estimator, core unchanged
  push.py                    agent: send new evidence on an interval
  server/
    entry.py                 Python Worker: routing, auth, D1 access
    confidence.py            tier rules over RegressionEstimate
    schema.sql               D1 schema
    static/index.html        single page: vanilla JS, inline SVG
wrangler.toml                Worker config, D1 binding, python_workers flag
```

Three parts:

**Agent** (`subbench push`) sends normalised evidence recorded since the last acknowledged
cursor. It runs from the existing watcher loop on an interval, so there is no second
daemon to supervise.

**Worker** (Python Workers on Pyodide) handles `POST /ingest` behind a bearer token and
serves `GET /api/*` by running the real estimator over D1 rows.

**Page** is static, fetches `/api/*`, and renders value cards and charts with no build
step and no external requests.

### Why Python Workers

The estimator must not be reimplemented. Every subtle defect this project has hit lived in
that code — quota-span weighting, unobserved-usage exclusion, reset-boundary clustering,
stale-cost joins. A second implementation in JavaScript would drift silently and there
would be no way to tell which copy was right.

`subbench.regression` and `subbench.timeseries` are pure standard library: `dataclasses`,
`statistics`, `datetime`, `typing`. Cloudflare's Python Workers provide the full standard
library except a documented exclusion list (`curses`, `fcntl`, `tkinter`, `venv` and
similar), plus `threading` and `multiprocessing` which import but do not function. The
estimator uses none of these and is single-threaded, so it runs unmodified.

Python Workers are in open beta and need the `python_workers` compatibility flag.
Cloudflare Containers reached general availability on 2026-04-13 and would run the same
package inside a normal ASGI app. Containers is the fallback if the beta proves
unworkable; because both import the same package, switching is a deployment change rather
than a rewrite. Keep the Worker entry point thin to preserve that.

Pyodide cold starts add seconds to the first request after idle. Acceptable for a
dashboard; it would not be acceptable for a high-frequency API.

## Data model

D1 mirrors the local schema for the two tables the estimator consumes, with an added
agent dimension.

```sql
CREATE TABLE agents (
    agent_id     TEXT PRIMARY KEY,
    label        TEXT,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE entitlement_snapshots (
    agent_id         TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    provider         TEXT NOT NULL,
    account_id       TEXT,
    account_key      TEXT NOT NULL,   -- COALESCE(account_id, '')
    window           TEXT NOT NULL,
    used_percent     REAL NOT NULL,
    resets_at        TEXT,
    duration_minutes INTEGER,
    source           TEXT NOT NULL,
    PRIMARY KEY (agent_id, provider, account_key, window, observed_at)
);

CREATE TABLE usage_rows (
    agent_id         TEXT NOT NULL,
    import_key       TEXT NOT NULL,   -- the agent's local import id, as text
    imported_at      TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    provider         TEXT NOT NULL,
    account_id       TEXT,
    account_key      TEXT NOT NULL,   -- COALESCE(account_id, '')
    period_start     TEXT,
    model            TEXT,
    model_key        TEXT NOT NULL,   -- COALESCE(model, '')
    input_tokens              INTEGER NOT NULL,
    cached_input_tokens       INTEGER NOT NULL,
    cache_write_tokens        INTEGER NOT NULL,
    cache_read_tokens         INTEGER NOT NULL,
    output_tokens             INTEGER NOT NULL,
    reasoning_output_tokens   INTEGER NOT NULL,
    reported_cost_usd         TEXT,
    source_path      TEXT NOT NULL,
    PRIMARY KEY (agent_id, import_key, source_path, model_key)
);

CREATE INDEX usage_by_import ON usage_rows(agent_id, provider, account_key, last_seen_at);
CREATE INDEX entitlement_by_series
    ON entitlement_snapshots(agent_id, provider, account_key, window, observed_at);
```

`account_key` and `model_key` exist because SQLite permits NULL in any PRIMARY KEY that is
not `INTEGER PRIMARY KEY`, and two rows differing only by a NULL are not treated as
conflicting. Claude entitlement rows have no `account_id`, and aggregate usage rows have no
`model`, so keying on the nullable columns directly would make `ON CONFLICT` upserts miss
and re-pushing would insert duplicates instead of being idempotent. The nullable columns
are retained for reads; the `_key` columns are derived on ingest and used only for
identity. The local schema avoids this by using `UNIQUE` with an `INSERT OR IGNORE`, where
the same NULL behaviour causes a duplicate rather than a corrupted upsert.

`last_seen_at` is carried because the estimator's staleness bound depends on it: a
deduplicated ccusage payload stops advancing `imported_at`, and without `last_seen_at` a
current cost reading looks arbitrarily old.

`raw_json` is deliberately not sent. It is 3.37 MB of the local database's 5.96 MB and the
estimator never reads it.

### Growth and retention

Measured over roughly two days of heavy use: 169 imports, 16,421 usage rows, 192
entitlement snapshots. That is about 85 imports, 8,200 usage rows and 96 entitlement
snapshots per day, or roughly 3 million usage rows a year for one agent.

Measured again after building it, by pushing the real database through ingest into the D1
schema: 253 entitlement rows and 21,724 usage rows occupy **9.46 MB**, against about
2.6 MB of normalised rows locally. The difference is denormalisation — `agent_id`,
`account_key`, `model_key`, `imported_at` and `last_seen_at` are stored per row, where
locally the import metadata lives once per import. That is roughly 3.6x, or about
1.7 GB/year for one agent against D1's 10 GB limit.

Retention is therefore load-bearing rather than tidiness, and the 90-day window below
holds one agent at roughly 425 MB. A second agent doubles it. If several agents are ever
added, moving the per-import columns into their own table is the obvious next step.

Entitlement snapshots are small and irreplaceable — an unsampled moment is gone forever —
so they are never pruned. Usage rows are prunable: once a reset window has passed and its
estimate is settled, the underlying rows are only needed to re-derive that window.

Retention: delete usage rows whose `last_seen_at` is older than 90 days. This bounds the
table at roughly 750,000 rows per agent while preserving the ability to re-derive any
window from the last quarter. Implemented as a Worker Cron Trigger, not inline in ingest.

## Agent push protocol

`subbench push` is incremental and idempotent.

- **Cursor.** The agent stores the highest `observed_at` and `last_seen_at` the server has
  acknowledged, in a local `push_state` table. Each push sends rows strictly newer than
  the cursor. The cursor advances only on a `200`.
- **Idempotency.** The server upserts by primary key. Re-pushing an overlapping range is
  harmless, which means a lost acknowledgement costs a duplicate send, not a gap.
- **Batching.** At most 5,000 usage rows per request; the agent loops until drained.
- **Identity.** `agent_id` is a UUID generated on first push and stored locally. It is not
  derived from anything identifying.
- **Interval.** Default hourly, from the watcher loop. `--once` for manual pushes.
- **Failure.** A push failure is logged and the cursor is left alone. Collection never
  depends on the network; the local database remains the source of truth.

Configuration mirrors the existing environment-variable style:

```bash
SUBBENCH_PUSH_URL=https://subbench.example.com/ingest
SUBBENCH_PUSH_TOKEN=...
```

If `SUBBENCH_PUSH_URL` is unset, `subbench push` is a no-op and `subbench watch` never
attempts it.

### Request shape

```json
{
  "agent_id": "…",
  "schema_version": 1,
  "entitlements": [ { "observed_at": "…", "provider": "codex", "window": "weekly",
                      "used_percent": 84.0, "resets_at": "…", "duration_minutes": 10080,
                      "account_id": "…", "source": "codex-app-server" } ],
  "usage": [ { "import_key": "…", "imported_at": "…", "last_seen_at": "…",
               "provider": "codex", "account_id": "…", "period_start": "2026-07-30",
               "model": "gpt-5.6-terra", "input_tokens": 0, "…": 0,
               "reported_cost_usd": "1.23", "source_path": "$.daily[57]" } ]
}
```

Response `200`: `{"accepted": {"entitlements": n, "usage": n}, "cursor": {...}}`.

## Confidence tiers

The page shows every estimate. Prominence is set by tier.

An estimate is scored on evidence the estimator already produces:

- `covered_quota_percent` — quota that advanced with recorded spend beside it
- `coverage_percent` — covered ÷ total quota advanced
- `slope_count` — valid pairs behind the estimate
- relative band width — `(upper_usd - lower_usd) / estimate_usd`
- cross-window corroboration, where a provider exposes two windows

Rules, evaluated top down:

| tier | requirement |
|---|---|
| `confirmed` | meets `likely`, **and** relative band width ≤ 0.7 **or** cross-window corroboration within 15% |
| `likely` | `covered_quota_percent` ≥ 25 **and** `coverage_percent` ≥ 70 **and** `slope_count` ≥ 50 |
| `provisional` | an estimate exists |

The band threshold is 0.7, not 1.0. At 1.0 every current series passes it — 0.77, 0.61 and
0.53 — so the criterion would be decorative, present in the rules but never deciding
anything. At 0.7 it is a real filter on estimates that reached `likely` but are still
unstable, which is the only job it has.

Against the data as of 2026-07-31:

| series | covered | coverage | slopes | band | tier |
|---|---|---|---|---|---|
| codex weekly | 45.0 | 50.5% | 4,842 | $61–$149 | `provisional` — coverage fails |
| claude five_hour | 33.0 | 100% | 110 | $25–$48 | `confirmed` — width 0.61 |
| claude weekly | 4.0 | 100% | 37 | $224–$383 | `provisional` — too little quota |

This spread is the intended behaviour. The Codex series has the most observations by far
and is still only `provisional`, because half its quota movement was never measured —
which is exactly the fact a confidence label exists to convey.

**Cross-window corroboration** applies when one account has both a short and a long
window. The short window's estimate predicts the long one through the observed ratio of
their quota movement. On 2026-07-30 the Claude five-hour window predicted $304.26 against
a direct weekly estimate of $299.72, a 1.5% agreement. Corroboration catches systematic
error that no single series can, so it is sufficient — not necessary — for `confirmed`.

Thresholds live in `confidence.py` as named constants with the reasoning recorded beside
them, in the style of the existing estimator constants.

## HTTP API

| route | method | auth | returns |
|---|---|---|---|
| `/` | GET | public | the page |
| `/api/current` | GET | public | rolling values per provider and window, with tier |
| `/api/history` | GET | public | one estimate per reset window |
| `/api/models` | GET | public | model token share per window |
| `/ingest` | POST | bearer | acceptance counts and cursor |
| `/api/health` | GET | public | schema version, last ingest time, row counts |

`/api/current` returns the `CurrentValue` fields already defined — `estimate_usd`,
`marginal_usd`, `lower_usd`, `upper_usd`, `coverage_percent`, `quota_span_percent`,
`window_count` — plus `tier` and the reason a tier was not reached.

## Page

One screen, no navigation:

- A card per provider and window: value, marginal rate, tier badge, and the one-line
  reason for the tier.
- A convergence chart per series — estimate over observations, the terminal equivalent of
  `subbench chart` — as inline SVG.
- A model-mix bar per window.
- A footer with last ingest time and schema version.

Rendering is vanilla JavaScript against `/api/*`. Charts are hand-rolled SVG: the data is
a few hundred points and a charting library would be the page's only external dependency.
Light and dark are both supported through `prefers-color-scheme`.

## Error handling

**Ingest validation** is the point of server-side derivation, so it is strict and rejects
rather than coerces:

- `used_percent` outside 0–100, negative token counts, or a `reported_cost_usd` that is
  not a decimal → `400`, whole batch rejected, nothing partially written.
- Unknown `provider` → `400`.
- `schema_version` newer than the Worker understands → `409` with the supported version,
  so an upgraded agent fails loudly instead of writing rows the server misreads.
- Missing or wrong token → `401`.
- Batch over 5,000 usage rows → `413`.

A rejected batch leaves the agent's cursor unmoved, so the data is retried after the
underlying problem is fixed. Rejections are counted and exposed on `/api/health`.

**Read paths** degrade rather than fail. A series with no valid pairs is absent from
`/api/current` rather than reported as zero — matching `robust_estimates`, which omits
rather than fabricates. If D1 is unavailable the page shows the failure and the last
successful fetch time; it never renders a stale number as current.

## Testing

The estimator's 46 existing tests already cover derivation and are untouched.

New coverage:

- **Confidence tiers** — table-driven over synthetic estimates, including the three real
  series above as regression cases, so a threshold change that reclassifies your live data
  fails a test.
- **Push protocol** — cursor advances only on success; overlapping re-push is idempotent;
  a failed push leaves the cursor unmoved; batching drains correctly.
- **Ingest validation** — each rejection above returns its status code and writes nothing.
- **Round trip** — evidence pushed from a fixture database, derived server-side, produces
  the same estimates as deriving locally. This is the test that would catch the estimator
  forking, and it is the most important one here.

The round-trip test runs against the Worker's request handler directly, in-process, with
D1 replaced by an in-memory SQLite connection. Deployed-Worker testing is manual against a
staging environment; the beta runtime is not worth mocking.

## Deployment

```bash
wrangler d1 create subbench
wrangler d1 execute subbench --file src/subbench/server/schema.sql
wrangler secret put SUBBENCH_INGEST_TOKEN
wrangler deploy
```

`wrangler.toml` sets the `python_workers` compatibility flag, binds D1, and registers the
retention Cron Trigger. The domain is attached through Cloudflare's dashboard.

## Accepted losses

Stated plainly so they are not rediscovered later:

- **Raw payloads stay local.** A future `normalise_payload` fix cannot be applied to
  server-side history. Mitigation: the local database keeps everything, and a full re-push
  after a local re-parse is possible because ingest is idempotent.
- **Push is best-effort.** Evidence collected while the network is down arrives late.
  Estimates are recomputed from whatever has arrived, so late data is absorbed correctly
  rather than lost — but a gap in the server's view can transiently look like the
  unobserved-usage pattern the estimator excludes. `/api/health` exposing last ingest time
  is how that is distinguished.
- **Python Workers are beta.** Accepted deliberately, with Containers as a
  redeploy-not-rewrite fallback.

## What this enables next

The schema carries `agent_id` from the start, so multi-user aggregation is an additive
change rather than a migration. Server-side derivation is the property that makes it
work: pooled estimates must be computed over combined pairs, which cannot be reconstructed
from per-user summaries.

Per-model quota weights — the prerequisite for the model × subscription Pareto — need many
reset windows with varying model mix. Pushing evidence now means that data accumulates
server-side from day one, whether or not the fit is ever run here.

## Deployment status, 2026-07-31

The Worker code is written and `wrangler deploy --dry-run` validates. It is **not yet
deployed**, because of a toolchain problem rather than a problem with this code.

What `wrangler dev` established, which is the part that mattered:

- The whole 169 KB bundle **loads on Pyodide**. `subbench.regression`, `timeseries` and
  `weights` import and initialise, which was the main risk in choosing Python Workers.
- Requests route to the handler.

Two problems were found and fixed by running it rather than reasoning about it:

1. An entry file inside the package cannot use relative imports. Wrangler loads it as a
   top-level module, so Pyodide raises "attempted relative import with no known parent
   package". The entry now sits at `src/worker.py`, beside the package, so `subbench`
   ships as a real package.
2. `queries` reached `store`, which reaches `entitlement`, which imports `subprocess` --
   unavailable on the runtime. `MAX_COST_AGE_MINUTES` moved to `regression`, and the
   server's import closure is now `regression`, `timeseries`, `weights` and `server.*`
   with only `dataclasses`, `datetime`, `decimal`, `json`, `statistics` and `typing`
   beneath them.

The remaining blocker is the handler entry point. Current Python Workers expect
`class Default(WorkerEntrypoint)` from the `workers` package, which ships in `workers-py`
and is built with `uv run pywrangler` rather than plain `wrangler`. That build fails
before it starts:

```
INFO    Installing packages into python_modules...
INFO    Packages installed in python_modules.
INFO    Installing packages into .venv-workers...
WARNING error: Unexpected '', expected '-c', '-e', '-r' or the start of a requirement
ERROR   Failed to install the requirements defined in your pyproject.toml file.
```

Ruled out by testing: the project's `plotext` dependency (fails identically with
`dependencies = []`), and a `dev` name collision between `[project.optional-dependencies]`
and `[dependency-groups]`. The `python_modules` install succeeds and only the
`.venv-workers` step fails, which points at pywrangler rather than at this project.

The `disable_python_external_sdk` escape hatch was also tried, with module-level
`on_fetch`/`fetch` exports defined both in the entry module and imported into it. The
runtime kept reporting "Handler does not export a fetch() function", so that path appears
to be withdrawn rather than merely undocumented.

Options, in the order worth trying:

1. Pin an older `workers-py`, or check the workers-sdk issue tracker for the
   `.venv-workers` install bug. Cheapest if a fix or working version exists.
2. Deploy on **Containers** instead. Generally available, runs the same package under a
   normal ASGI app, and needs no Pyodide toolchain. `entry.py` was kept thin precisely so
   this is a redeploy rather than a rewrite.
3. Keep derivation on the agent and reduce the Worker to store-and-render in JavaScript.
   Rejected during design because it duplicates the estimator, and that judgement has not
   changed.

Nothing about the agent, ingest, confidence, multi-agent merge or weights work depends on
which of these is chosen.
