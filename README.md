# SubBench

SubBench measures the API-equivalent value of a coding-agent subscription. It joins the
token usage that Codex CLI and Claude Code record on this machine with the quota movement
that the provider reports for the same period.

The collector is one executable. It needs no Python, Node, ccusage or Codex installation:

```bash
curl -fsSL https://github.com/jask-aran/SubBench/releases/latest/download/install.sh | sh
```

SubBench does not estimate the internal cost of OpenAI or Anthropic. It answers one
question:

> At public API prices, how much usage did this subscription entitlement deliver under the
> observed model mix and workload?

The documentation uses ASD-STE100 Simplified Technical English.

---

## 1. Data collection

Four collectors write to one SQLite database at
`~/.local/share/subbench/subbench.sqlite3`. The `subbench watch` command runs all four.
All four are built in.

| Collector | Source | Result |
|---|---|---|
| File watcher | Agent JSONL logs | A trigger. It reads file metadata only. |
| Log reader | Agent JSONL logs | Token counts and API-equivalent cost, per day and model. |
| Codex meter | ChatGPT usage endpoint, with the Codex login | Quota percent, window length, reset time and plan. |
| Claude meter | Claude OAuth usage endpoint, with the Claude Code login | The same values for Claude. |

The log reader replaces ccusage. It is a port of ccusage 20's Claude and Codex readers,
rule for rule, so a database recorded with ccusage continues without a step. It counts
the same things ccusage counts:

- Codex: `.jsonl` and zstd-compressed `.jsonl.zst` rollouts, active over archived copies;
  forks and subagents without the parent history they replay; automatic compactions,
  counted once; headless `codex exec` usage; the service tier each request ran at;
  `codex-auto-review` priced as the model it routed to on that date.
- Claude Code: a reply logged several times (streaming, resumed sessions, subagent
  replays) once, keeping the fullest copy; subagent progress lines; advisor iterations;
  fast mode; a cost the log recorded itself.

`tests/test_parity.py` holds the reader to ccusage's own output for generated logs that
exercise each of these rules (packaging/prices/README.md). `--runner npx` still runs
ccusage instead.

### Optional ccusage backup

ccusage is optional. The backup does not install it. If `ccusage` is on the PATH (for
example, after `npm install -g ccusage`), SubBench checks its reader against ccusage
once an hour, at collection time. It compares daily token counts, not costs. If the
built-in reader fails, SubBench uses ccusage as the usage source.

`SUBBENCH_CCUSAGE=off` turns this backup off. `SUBBENCH_CCUSAGE=/path` names an
executable file. The backup runs with `--offline`. It uses cached prices and does
not fetch a price table.

The systemd unit's PATH is
`~/.local/bin:/home/linuxbrew/.linuxbrew/bin:/usr/local/bin:/usr/bin:/bin`.
If you installed ccusage elsewhere, for example under nvm, set `SUBBENCH_CCUSAGE`
to its full path.

### The watcher

The watcher scans file metadata every 2 seconds. It does not open or parse the logs. After
the last write, the watcher waits 60 seconds for the burst of writes to stop. Then it
reads the new log lines and the quota meters. A full reconciliation runs every 6 hours,
even if no file changed. The reconciliation recovers missed file events. It also recovers
usage that occurred while SubBench was off.

```bash
subbench watch --interval 1       # metadata scan period, in seconds
subbench watch --debounce 10      # wait time after the last log write
subbench watch --reconcile 3600   # maximum time between full reconciliations
```

### What the database keeps

The database keeps four layers apart:

1. **Usage evidence.** The daily usage report, and the token classes and USD cost
   calculated from it. No log text is kept.
2. **Entitlement evidence.** One quota reading for each window at each poll: the used
   percent, the reset time and the window length.
3. **Hourly usage.** Token counts, requests and cost per UTC hour and model, in
   `usage_hourly`, updated in place as an hour fills. A closed hour keeps the account that
   was active when it was spent. The estimator does not read this yet: it still sums the
   daily imports, so every figure stays comparable with the history recorded before. The
   hourly table is the finer evidence for edge-of-window attribution, and it stays local.
4. **Inference.** Every estimate, derived from layers 1 and 2 and never treated as source
   data. A window's estimate is stored in `window_estimates` once the window has reset,
   with a digest of the readings it came from, the peers its reference rate came from,
   and the estimator version. Each cycle then estimates only the open windows (one per
   series) and reads the rest back. A window is computed again only if its readings
   change (a late usage import repriced them) or the estimator version changes.

A window's reference rate, which detects quota spent off this machine, comes from the
windows in its series that had closed before it closed. That is what makes a closed
window's estimate final: no later window can change it.

SubBench keeps the raw payloads. Therefore an improvement to the estimator applies to all
history, and not only to new data: a new estimator version recomputes every window once.

The two quadratic loops (every pair of readings in a window) run as numpy array
operations in `subbench/kernels.py`, with the same arithmetic in the same order as the
loops they replace. `tests/test_estimator_kernels.py` checks the results are identical to
the original pure-Python estimator, kept in `tests/reference_estimator.py`.

SubBench discards a usage report that is identical to the previous payload. The
`imports.last_seen_at` column records when SubBench last confirmed a payload. This
separates "unchanged" from "not collected".

SubBench rounds each reset time to the nearest minute. A provider reports a stable reset
boundary that moves by a few seconds between reads. SubBench then groups reset times that
are within 5 minutes of each other. Two different windows are hours apart, so this
tolerance cannot merge two real windows.

### Entitlement collection for each provider

**Codex.** SubBench reads the access token in `$CODEX_HOME/auth.json` and asks
`https://chatgpt.com/backend-api/wham/usage`, the endpoint the Codex CLI reads. The
response gives the used percent, length and reset time of each window, and the plan name.
SubBench uses that name to decide whether two accounts are comparable. If the request
fails (usually an expired token) and Codex is installed, SubBench asks
`codex app-server` instead, because Codex refreshes its own login.

**Claude.** SubBench reads the access token that Claude Code keeps in
`~/.claude/.credentials.json` (on macOS, in the Keychain) and asks
`https://api.anthropic.com/api/oauth/usage`. It asks the profile endpoint for the current
plan, because the login file can lag an upgrade by days. A change of plan on the same
account is recorded on the next reading, and that reading starts a new series.

The Claude meter also reports **scoped limits**: a sub-limit for one model family inside
the weekly limit. For example, Fable has its own limit, equal to 50% of the weekly limit.
SubBench records each model-scoped limit as its own window, `weekly_<scope>` (for example
`weekly_fable`), with its share of the weekly limit (`allowance_percent`). A limit scoped
to a surface (an app) and not a model is not recorded, because no model id can price it.
Refer to "Scoped limits".

Claude Code keeps its own copy of the last meter response in `~/.claude.json`
(`cachedUsageUtilization`), with the time it was fetched and the account. When the
endpoint answers 429, or SubBench waits between reads, SubBench uses this copy if it is
newer than its own last reading, less than 1 hour old, and for the account that Claude
Code uses now. The reading keeps the time that Claude Code fetched it. This sends no
request. Such a reading has the source `claude-code-cache`.

SubBench never refreshes or stores a login. A refresh rotates the login, and that would
sign the agent out. An expired token stops the readings until the agent next refreshes it,
and `subbench doctor` reports it. `SUBBENCH_CLAUDE_USAGE_COMMAND`, if set, still replaces
the built-in Claude reader.

### Pricing

Prices are ccusage's: the table inside the executable is generated from ccusage's own price
resolution (LiteLLM and models.dev, with its corrections), so pricing works offline and
matches the history ccusage priced. Each token class has its own rate; a one-hour cache
write costs twice the input rate; a request past a model's long-context threshold pays the
long-context rates; Fast, Flex and Ultrafast requests pay the model's multiplier. A model
the table lacks is looked up in LiteLLM's live table, fetched at most once a day
(`SUBBENCH_OFFLINE=1` never fetches). If LiteLLM cannot be reached, the collector uses
`src/subbench/data/prices_fallback.json`: LiteLLM rows for models newer than the ccusage
table (at present, `claude-fable-5-2`). A model with no price at all costs zero, and the
collector names it in its log.

---

## Privacy

SubBench is built so that you can check what it does, not only read a promise.

```bash
subbench privacy            # what is read, kept and sent, and the only hosts it can reach
subbench privacy log        # every request this collector has made
subbench privacy preview    # the exact payload of the next push, not sent
```

- **Reads only usage.** The log reader skips every line that cannot carry usage without
  decoding it. From the remaining lines it keeps the timestamp, the model, the message id
  (to count a message once) and the token counts. Prompts, replies, code and paths are
  never stored, printed or sent.
- **A closed list of hosts.** Every request goes through `subbench/net.py`. It refuses any
  host that is not `api.anthropic.com`, `chatgpt.com`, `raw.githubusercontent.com` (the
  public price table), `subbench.jask-aran.com` (the public benchmark bundle, for
  `subbench frontier` only) or the push endpoint you configured. It refuses plain HTTP. A
  test fails if any other module imports a network client.
- **Each login goes only to its own provider.** `net.py` refuses to send the Claude token
  anywhere but Anthropic, or the Codex token anywhere but ChatGPT.
- **Every request is recorded** in `egress.jsonl` beside the database: time, host, path,
  purpose, bytes sent and received, status. Bodies and headers are not recorded.
- **Nothing is sent unless you configure a push.** With a push, the server receives quota
  readings, derived values and the model mix. Account ids are replaced by a one-way hash
  before the payload is built (the same account hashes the same on every machine, so two
  collectors still pool). Emails and names are never sent. Raw daily usage rows are sent
  only with `SUBBENCH_PUSH_RAW_USAGE=1`.
- **The collector hashes; the server checks what it can read.** A collector from this
  version sends its reports gzipped, and the Worker stores and serves them without opening
  them, to keep its CPU time low. For those reports the collector's hashing is the only
  guard, and `tests/test_privacy.py` opens every gzipped report a push carries to check
  it. The Worker still hashes any account id that arrives unhashed in an uncompressed
  report or reading, so a collector from before 0.3.0 cannot store a raw id. With one
  collector, the site serves its report, where every account id is hashed. With several,
  it serves pooled figures and product names only, and no account id at all.
  For ids a server already holds, `subbench privacy migration` prints SQL that rewrites them in
  place (idempotent); run it with `npx wrangler d1 execute subbench --remote --file`.
- **The binary is the source.** Each release is built from a tag by
  `.github/workflows/release.yml`, with a SHA-256 file and a signed build-provenance
  attestation. `gh attestation verify subbench-linux-x64 --repo jask-aran/SubBench` proves
  the file you run came from this source.

---

## 2. Arithmetic

### Step 1 — the value of the usage

SubBench prices each request with the token classes in the log and the price table above.
For Codex:

```text
V = (T_input - T_cached) x P_input
  + T_cached x P_cached
  + T_output x P_output
```

For Claude Code:

```text
V = T_input x P_input
  + T_cache_write x P_cache_write
  + T_cache_read x P_cache_read
  + T_output x P_output
```

SubBench sums this value over the days **inside the reset window**. It does not subtract
one lifetime total from another. ccusage reported its full history at each run, and that
history was not stable between runs: two identical commands returned reports that
differed by several days. A difference of two lifetime totals turns that instability into
large false deltas. A window-bounded sum does not depend on any day outside the window.

### Step 2 — the slope of each pair

Inside one reset window, each pair of observations where both the quota and the value
increase implies a value for the full window:

```text
slope(i, j) = (value_j - value_i) / ((quota_j - quota_i) / 100)
```

SubBench rejects a pair in five conditions:

| Condition | Reason |
|---|---|
| The quota moved less than 2 points. | A provider reports quota as a whole percent. A 1-point pair carries approximately 50% error. |
| The value did not move. | This shows an incomplete usage import, and not free quota. |
| The cost total is more than 30 minutes old. | The quota advances while a stopped collector holds the cost still. |
| The pair spans an interval of unobserved usage. | Refer to "Usage that local logs cannot see". |
| The pair spans spend of a scoped model that this meter cannot take out. | Refer to "Scoped limits". |

### Step 3 — the estimate for the window

SubBench reports the **quota-weighted median** of all valid slopes. Each slope has the
weight of the quota that it spans.

The weight is necessary. The error enters through `1/delta`, so the distribution of the
slopes has a long tail to the right. Short pairs also outnumber long pairs quadratically.
An unweighted median therefore follows the least reliable evidence, and it becomes worse
as more observations arrive. The weight makes one 50-point pair equal to fifty 1-point
pairs, which is the ratio of the information in them.

SubBench also reports a **marginal** value. This is the same calculation over the pairs
inside the most recent 20 quota points. The window average shows what the entitlement has
delivered until now. The marginal value shows what one quota point is worth now. The two
values move apart when the model mix changes inside a window.

The 10th to 90th percentile range of the slopes is an empirical stability range. It is not
a formal confidence interval. A wide range usually means that the value for each quota
point changed inside the window.

### Step 4 — combination across windows and accounts

A provider that reports both a five-hour window and a weekly window meters one
subscription twice. Use of the five-hour window also uses part of the weekly window. The
observations give this ratio directly:

```text
claude: 7.98 five_hour entitlements fill one weekly entitlement
```

SubBench multiplies each five-hour estimate by this ratio. The result joins the weekly
pool. The five-hour window turns over approximately 34 times each week, and the weekly
window turns over one time. Therefore this step gives the weekly figure much more
evidence.

SubBench converts a short window into a long window only. It never converts a long window
into a short one. A weekly meter is not evidence of a five-hour limit. A conversion in that
direction would invent the limit that the measurement must find.

Two accounts on one plan are two separate entitlements. SubBench therefore never pools
their **pairs**: one percent of each account is a different physical allowance. It does
pool their **estimates**, because those describe the same product. The plan name must be
equal, and that name comes from the meter itself.

### Scoped limits

Claude meters Fable two times. Fable usage moves the weekly limit, as all models do. It
also moves a Fable meter, which shows 0–100% of the Fable allowance (50% of the weekly
limit). Per API dollar, Fable uses the weekly limit approximately 2.5 times as fast as
Opus. If SubBench measured the two together, the weekly value would follow the share of
Fable in the model mix, and not the plan.

Thus SubBench measures each scoped limit separately (`src/subbench/scopes.py`):

| Window | Quota | Value |
|---|---|---|
| `weekly_fable` | The Fable meter. | Fable spend only. The estimate is the value of one full Fable allowance. |
| `weekly` | The weekly meter minus the Fable meter × its share (50%). | Spend on all other models. |
| `five_hour` | The five-hour meter. No Fable meter is available to subtract. | Spend on all other models. A pair that spans Fable spend is rejected. |

A weekly reading with no Fable reading beside it (for example, before SubBench recorded
the Fable meter) is treated as the five-hour window is: a pair that spans Fable spend is
rejected. A model belongs to a scope when its id contains the scope as a word
(`claude-fable-5-2` is in `fable`).

The site shows each scoped limit as its own product, for example "Claude Max 5X · Fable".
Its weekly figure is the value of one full Fable allowance. "Claude Max 5X" is then the
value of the weekly limit on the other models. On the Models page, the Fable product covers
Fable models only, and the plan covers the other models of its provider.

**A Fable reset.** A subscriber can use a Fable reset. The provider then gives back the
Fable share of the weekly limit, and the weekly meter drops (on 2026-11-10, from 60% to
11%) with no change of reset time. With the Fable meter recorded, this drop does not
change the non-Fable weekly series, because the weekly meter and the Fable share drop by
the same amount. Without it, SubBench adds the drop back to every later reading in the
window (`PARTIAL_REFILL_MIN_DROP`, 5 points). A drop that follows a rise of at least half
its size is a spike that reversed, and SubBench does not add it back.

### Step 5 — divergence

Two independent measurements of one quantity must agree. If they do not agree, either an
assumption here is wrong, or something changed at the provider. A single series shows
neither condition.

```text
scope     provider  subject                  difference  detail
window    claude    five_hour vs weekly         -42.1%   five_hour implies US$300.00 per
                                                         weekly entitlement, measured
                                                         directly it is US$173.70
```

The threshold is 35%. This value is well above the usual noise of the estimator, which is
a few percent on real data.

### Step 6 — value over time

Each reset window gives one estimate. SubBench treats these estimates as a time series. It
does not fit one permanent regression across all history.

Two series answer two different questions:

- **Settled windows.** One point for each window that has passed its reset time. A step
  between two settled points is a change in what the entitlement delivered. This series
  detects a change of allowance at the provider, and gives its date.
- **Replay.** What the present estimator says at each moment inside a window. This series
  shows convergence. SubBench calculates it again from the retained evidence, so one
  estimator produces the whole curve. A stored series would mix estimator versions, and a
  later correction would then look like a change of the plan.

Each point carries an `estimator_version`. This value is a hash of the constants that
decide what an estimate is. A step in a line has two possible causes: the provider changed
something, or SubBench changed something. The version separates the two causes.

### Confidence tiers

SubBench shows every estimate. The tier controls how prominent the estimate is. To hide a
weak estimate would give an empty page for the first hours. It would also hide the series
that most needs an explanation.

| Tier | Requirement |
|---|---|
| `confirmed` | Meets `likely`, and has a slope band within 70% of the estimate **or** cross-window agreement within 15%. |
| `likely` | Has at least 25 points of measured quota, 70% coverage and 50 valid pairs. |
| `provisional` | An estimate exists. |

### Usage that local logs cannot see

The log reader reads the logs on this machine only. Quota that a web interface, a cloud runner or
a second machine uses leaves no local tokens.

Such an interval is not cheap usage. It is unmeasured usage: the numerator is absent, and
not small. It also spans much quota, so the weight would make it the strongest evidence of
all. SubBench therefore rejects **every pair that spans an interval where the quota moved
at least 5 points and the recorded value did not move**. This includes a pair whose own
ends are far outside that interval, because it inherits the same absent value.

The result is an estimate over the observed evidence only. This assumes that the
unobserved quota was worth approximately as much as the observed quota. That is an
assumption, but it is much better than an assumption that the unobserved quota was worth
nothing.

Reports therefore give a **coverage** value: the share of the observed quota movement that
had recorded spend beside it. A window can show an 89% quota span and 49% coverage. Only
the second number describes the evidence.

### Why SubBench does not attribute cost below one day

`ccusage <provider> session --json` gives the time of each session. Against 93 dense
observations, this does not improve on daily buckets:

| Attribution | Linearity of cost against quota (R²) |
|---|---|
| Daily buckets | **0.977** |
| Session, at `lastActivity` | 0.054 |
| Session, spread over its span | 0.974 |

A session has a median span of 30 minutes and a 90th percentile of 6 hours. To put its
full cost at its last activity is therefore very late. To spread the cost equally recovers
the accuracy, but it does not do better than daily bucketing. The usage row for the
current day is a live running total, so it already grows at each import. Day boundaries
are important only at the edges of a reset window.

---

## 3. How the data reaches subbench.jask-aran.com

Put the endpoint and the token in `~/.config/subbench/push.env`, which the service reads
through systemd and the command line reads directly:

```
SUBBENCH_PUSH_URL=https://subbench.jask-aran.com/ingest
SUBBENCH_PUSH_TOKEN=...
```

```bash
subbench sync push      # send now
subbench sync status    # endpoint, agent identity, cursor, last error
```

`subbench watch` pushes every 30 minutes on its own clock when these two variables exist.
It sends nothing when there is no new evidence. A variable exported in the shell overrides
the file, so a one-off endpoint needs no edit.

Normal pushes are measurement-only. They send quota snapshots and the derived reports;
raw usage rows stay in the local SQLite database. To send normalised usage rows for a
deliberate multi-machine aggregation test, set the exact opt-in before pushing:

```bash
export SUBBENCH_PUSH_RAW_USAGE=1
subbench sync push
```

The usage cursor, whole-import batching, retry behavior, and server idempotency apply only
to this opt-in path. A normal push does not advance the raw-usage cursor. The local raw
imports and rows are not deleted.

The push runs separately from the collection. Therefore a slow server cannot delay a quota
reading, and a failed push cannot fail a collection cycle. The local database stays the
source of truth. The cursor advances only after the server accepts the batch. Rejected
evidence is thus sent again after you correct the cause.

Each batch contains three parts:

1. **The new measurements** — quota readings since the last acknowledgement. Normal pushes
   contain no raw usage rows. The opt-in path sends complete imports, because the rows of
   one import share a timestamp. SubBench never sends the raw usage reports, and every account id is hashed.
2. **The computed reports** — `current`, `history`, `models`, `weights` and `series`.
3. **The cursor** that the server returns to acknowledge the batch.

**The agent calculates the reports. The server keeps them.** The Worker contains no
estimator logic. The quota-span weighting, the exclusion of unobserved usage, the reset
clustering and the staleness limit stay in `subbench/regression.py`. A second
implementation would move away from the first one silently, and nothing could then show
which copy was correct.

Estimation runs only on the collector. With several collectors, the server merges their
windows and pools their finished estimates with weighted medians, at most once per
10 minutes. With one collector, the site serves its report verbatim.
The pooled report carries the pooled figures and one row per measured product, never a
collector's per-account windows.
`worker/pooling.js` must reproduce `tests/pooling/golden`. Regenerate those cases with
`tests/pooling/generate.py` only after a deliberate change to `subbench.products.pool_history`.

The server validates the batch. Validation is different from derivation, and it is the
reason for the server. The Worker rejects a batch with a quota outside 0-100, with a
negative token count, with a cost that is not a decimal, or with a schema version that it
does not know.

Raw usage remains available locally for future aggregation. The server stores the quota
measurements and reports by default; enabling the opt-in usage path is a separate
operational decision and does not delete existing local or D1 rows.

---

## 4. The Worker and the site

The Worker is JavaScript. It runs on Cloudflare Workers with a D1 database and a static
asset binding. The site is a Vite build of `web/`, which Wrangler runs before
`wrangler deploy` and `wrangler dev`.

| Route | Method | Function |
|---|---|---|
| `/ingest` | POST | Validates a batch, then stores the evidence and the reports. |
| `/api/current` | GET | The rolling value, the divergences and the window ratios. |
| `/api/history` | GET | One estimate for each reset window. |
| `/api/models` | GET | The token share of each model, for each window. |
| `/api/weights` | GET | The per-model quota weights, if a fit exists. |
| `/api/series` | GET | The settled series, the replay series and the ratios. |
| `/api/health` | GET | The quota row count, last ingest time and schema version. |
| `/data/frontier.json` | GET | The benchmark bundle from LLM-Pareto. A static file. |
| everything else | GET | The site. |

Each report route returns the newest report of that kind. The pages read health every 5
minutes while the tab is visible.

### The pages

| Page | Contents |
|---|---|
| `/` Subscriptions | The figure strip (each plan's weekly value, ≈ monthly value and multiple of its price, and the most-used model). Below, one column holds the plans' limits table (weekly and 5-hour value, windows, the week in progress), the model mix per provider and the checks. The other column holds views composed from the Models page and seen through the plans: tasks a week and tasks per dollar on a chosen benchmark, and value over time. Pointing at a plan anywhere brings its line forward; **Open in Models** opens the view in the Models workbench. The completed windows table is last. `/subscriptions/` redirects here. |
| `/models/` | Every public benchmark in one chart the size of the window. A row of questions opens ready-made views. Otherwise the reader composes one: drag a measure from the Measures pane (this benchmark's score, its cost, time or tokens, a plan's tasks a week, or another benchmark's score) onto Up, Across or the third place, or click it. Each measure shows how many models it shares with the plotted pair. A third measure colours the 2D chart or adds depth (3D, with Angle, Front, Side and Top views). **Your limits** (cost, time, a score floor) fade the models out of reach and ring the best fit; a limit on a plotted measure is drawn on the chart. Two scores against each other get a trend line and Pearson's r. Every measured plan (pooled across the accounts that report it) covers its provider's models (Claude plans Anthropic's, ChatGPT plans OpenAI's), and every hover lists tasks per week on each covering plan. The site presents aggregates only: no page names or identifies an account. The **Plans** view plots the same join: x is tasks per weekly limit, y the benchmark score, and each measured plan's best trade-offs form its own curve, so a plan's leverage shows as its curve's shift to the right. **Per $** divides by the plan's list price (`web/src/lib/plans.js`, a dated table: Claude Pro $20, Max 5x $100, Max 20x $200, ChatGPT Go $8, Plus $20, Pro $200 a month), so plans at different prices compare on tasks per dollar; the Subscriptions page shows each plan's weekly value as a multiple of its price. **× Price 3D** plots tasks per week × the plan's weekly price × the score: each plan's curve sits at its price, and the surface is the best score reachable for a workload at or under a given weekly spend.

**How a plan and a benchmark meet.** Both sides are priced in API dollars: SubBench measures what a plan's weekly limit delivers at public API prices, and each benchmark measures what one task costs at the same prices. Tasks per week is their ratio. Token counts are not divided directly, because the two sides spend different token classes (a week of agent use is mostly cache reads; a benchmark's tokens per task are mostly output), and the price table is what makes the classes comparable. A per-class allowance reported by the collector would allow a token-based conversion as well. `/frontier/` and `/surface/` redirect here. |

The rules for what the pages show are unchanged. A figure appears only for a completed,
confirmed window. A window that passed every check except the width of its interval shows
a range only. A product that is measured but has nothing settled still gets a card. A
stale report or an unreachable API is said in the sidebar and the footer.

**What one week buys.** Tasks a week divides the measured weekly value of a product by the
measured cost of one benchmark task. The result is a throughput and not a promise: a real
workload uses different tokens for each task than a benchmark does. `subbench frontier`
prints the same table in the terminal.

**The attainment surface.** For a cost budget *c* and a time budget *t*, the surface height
is the best score of any model whose cost per task is at most *c* and whose time per task
is at most *t*. The surface is a staircase by construction. A point is on the 3D Pareto set
when no other point is cheaper, faster and better at once. A point can be on that set and
look dominated in a two-axis chart, because it wins on the axis that the chart hides.

### The code

| Path | Contents |
|---|---|
| `web/src/lib/` | The display rules and the frontier arithmetic. Plain modules; `node --test` covers them. |
| `web/src/pages/` | One SolidJS module for each page. |
| `web/src/app/` | The shell, the data loading, the charts, and `plans.ts` (the measured plans and the models each covers, shared by both pages). |
| `web/src/conduit/` | Conduit components v2, copied from the Conduit repository. AGPL-3.0; refer to its README. |
| `web/public/data/frontier.json` | The benchmark bundle. `LLM-Pareto/export-web-data.py` writes it. |

The chrome uses Conduit's tokens only. The chart series use one validated categorical
palette, because Conduit's category palette fails the colour-vision checks as scatter
series. The Models chart is ECharts, with echarts-gl for 3D, so 2D and 3D are one engine on one canvas and switching
redraws in place. The value-over-time chart uses Plotly's basic bundle. Both load on demand.

### Local preview

```bash
npm install
npm run dev                                           # live reload, demo reports
SUBBENCH_API=https://subbench.jask-aran.com npm run dev   # live reload, live reports
npm run build && npm run preview                      # the built site, demo reports
```

The demo reports are synthetic. Every page then shows a "Demo data" note, so a screenshot
of the development server cannot pass for a measurement.

### Deploy

```bash
wrangler d1 create subbench
wrangler d1 execute subbench --file src/subbench/server/schema.sql --remote
wrangler secret put SUBBENCH_INGEST_TOKEN
wrangler deploy
```

`wrangler deploy` builds the site first. The workflow `update-frontier.yml` refreshes the
benchmark bundle each day and commits it. The site shows the new bundle after the next
deploy.

---

## Installation

```bash
curl -fsSL https://github.com/jask-aran/SubBench/releases/latest/download/install.sh | sh
```

The script downloads one file for your OS and architecture (Linux or macOS, x64 or arm64),
checks it against the release's `SHA256SUMS`, puts it in `~/.local/bin/subbench` and runs
`subbench service install`. That writes a systemd user unit on Linux or a launchd agent on
macOS which runs `subbench watch` at login. `SUBBENCH_NO_SERVICE=1` skips the last step.

```bash
subbench doctor              # are both logins and the logs readable
subbench service status
subbench service logs
subbench config set SUBBENCH_PUSH_URL https://subbench.jask-aran.com/ingest
subbench config set SUBBENCH_PUSH_TOKEN <token>
subbench privacy preview     # what that push would send
subbench service install     # restart, to apply the configuration
```

On Linux a user unit stops at logout. `loginctl enable-linger` keeps it running. WSL must
have systemd enabled.

### From source

```bash
pip install -e .             # Python 3.11 or later
sh packaging/binary/build.sh # or build the single executable: dist/bin/subbench-<os>-<arch>
```

---

## Commands

```bash
subbench status                          # what each product is worth, and is collection healthy
subbench frontier                        # what one weekly limit buys on the benchmark frontier
subbench frontier --metric list          # the benchmark lenses in the bundle
subbench values                          # every window measurement
subbench values --tier confirmed         # only the settled ones
subbench values --product Claude         # one product
subbench values --account 1a2b3c4d       # one account
subbench values --window weekly --json   # machine-readable
subbench chart                           # the same pooled series the site plots
subbench chart --window weekly --product ChatGPT

subbench watch                           # continuous collection
subbench watch --provider codex --once   # one diagnostic snapshot
subbench collect codex                   # one usage snapshot now
subbench collect claude --print          # print the daily report instead of storing it
subbench doctor                          # check the logins, the logs and the freshness

subbench service install                 # run the collector at login (systemd or launchd)
subbench service status | logs | uninstall
subbench config set KEY VALUE            # the configuration file (push endpoint and token)
subbench config show                     # secrets masked
subbench privacy                         # what is read, kept and sent
subbench privacy log                     # every request made
subbench privacy preview                 # the next push, in full, not sent
subbench version

subbench sync push                       # send the evidence to the site
subbench sync status                     # endpoint, agent, cursor, last error
subbench sync reset --yes                # new agent identity, send everything again

subbench data path                       # where the database is, and how big
subbench data imports                    # audit the raw usage imports
subbench data import payload.json --provider claude
subbench data backup ~/subbench-backup.sqlite3
subbench data prune --days 90 --yes      # drop raw usage older than the cutoff
subbench data reset --yes                # delete every observation

subbench detail hourly                   # spend per UTC hour, the last 24 hours
subbench detail hourly --hours 168 --by-model
subbench detail models                   # token share of each model
subbench detail weights                  # per-model quota weights
subbench detail accounts                 # known accounts and plans
subbench detail estimator --history      # raw estimator output
subbench detail convergence --provider claude   # how one estimate settled
```

The former names (`push`, `report`, `chart`, `models`, `weights`, `accounts`, `ingest`,
`imports`, `init`) still work and map onto the commands above.

`subbench chart` draws in a normal terminal with `plotext`. SubBench stays a CLI. It does
not become an interactive TUI.

---

## Detection of a changed limit

SubBench compares the three most recent informative windows with the earlier baseline. It
reports a possible change only when all three conditions are true:

- The recent median differs from the baseline median by at least 20%.
- All three recent estimates are on the same side of the baseline.
- At least three earlier informative windows exist.

A change is `developing` with three or four baseline windows. It is `likely` with five or
more.

The date is the first reset window in which SubBench observed the new level. It is not
necessarily the date of the change at the provider. This is especially true if nobody used
the harness near the transition.

A step that continues is evidence of a changed **effective API-equivalent entitlement
under the observed workload**. It does not prove that every subscriber received a fixed
increase. A provider can change a model weight, a temporary multiplier or an account
segment. A large change in the workload mix can also move the estimate.

---

## How quickly SubBench converges

One reset window becomes usable when it has spanned a good part of its quota. That takes
hours to one day of ordinary use, and not weeks.

The aggregates across windows are the slow part, because whole reset periods size them.
The rolling value wants up to 10 windows, and the detection of a changed limit needs at
least 6. For a five-hour window that is one or two days. For a weekly window that is many
weeks.

More samples stop helping quickly. When the samples are dense enough for the meter to
advance between them, an extra sample adds a pair with either no quota delta or a 1-point
delta. SubBench rejects both. The scarce quantity is quota **span**, and not the number of
samples.

---

## Limits of the measurement

The valuation is only as accurate as the retained telemetry and the LiteLLM price table.
The inference stays empirical. A meter can round, a limit can roll, a provider can apply a
model-specific weight, and a multiplier can change. The results are workload-specific
estimates of an API-equivalent allowance. They are not contractual quotas.

---

## Licence

MIT
