# SubBench

SubBench measures the API-equivalent value of a coding-agent subscription. It joins the
token usage that Codex CLI and Claude Code record on this machine with the quota movement
that the provider reports for the same period.

SubBench does not estimate the internal cost of OpenAI or Anthropic. It answers one
question:

> At public API prices, how much usage did this subscription entitlement deliver under the
> observed model mix and workload?

The documentation uses ASD-STE100 Simplified Technical English.

---

## 1. Data collection

Four collectors write to one SQLite database at
`~/.local/share/subbench/subbench.sqlite3`. The `subbench watch` command runs all four.

| Collector | Source | Result |
|---|---|---|
| File watcher | Agent JSONL logs | A trigger. It reads file metadata only. |
| ccusage | Agent JSONL logs | Token counts and API-equivalent cost, per day and model. |
| Codex app-server | `codex app-server` JSON-RPC | Quota percent, window length and reset time. |
| Claude usage helper | Claude OAuth usage endpoint | The same three values for Claude. |

### The watcher

The watcher scans file metadata every 2 seconds. It does not open or parse the logs. After
the last write, the watcher waits 60 seconds for the burst of writes to stop. Then it
starts ccusage and the entitlement collectors. A full reconciliation runs every 6 hours,
even if no file changed. The reconciliation recovers missed file events. It also recovers
usage that occurred while SubBench was off.

```bash
subbench watch --interval 1       # metadata scan period, in seconds
subbench watch --debounce 10      # wait time after the last log write
subbench watch --reconcile 3600   # maximum time between full reconciliations
```

### What the database keeps

The database keeps three layers apart:

1. **Usage evidence.** The raw ccusage payload, and the token classes and USD cost that
   ccusage calculates from it.
2. **Entitlement evidence.** One quota reading for each window at each poll: the used
   percent, the reset time and the window length.
3. **Inference.** Every estimate. SubBench calculates each estimate again from layers 1
   and 2. It never treats an estimate as source data.

SubBench keeps the raw payloads. Therefore an improvement to the estimator applies to all
history, and not only to new data.

SubBench discards a ccusage payload that is identical to the previous payload. The
`imports.last_seen_at` column records when SubBench last confirmed a payload. This
separates "unchanged" from "not collected".

SubBench rounds each reset time to the nearest minute. A provider reports a stable reset
boundary that moves by a few seconds between reads. SubBench then groups reset times that
are within 5 minutes of each other. Two different windows are hours apart, so this
tolerance cannot merge two real windows.

### Entitlement collection for each provider

Codex has a supported local interface. SubBench starts `codex app-server`, does the
JSON-RPC handshake, and calls `account/rateLimits/read`. The response also gives the plan
name. SubBench uses that name to decide whether two accounts are comparable.

Claude Code has no equivalent local interface. Set `SUBBENCH_CLAUDE_USAGE_COMMAND` to a
command that prints the Claude usage response as JSON. `packaging/claude-usage.py` is such
a command. It reads the access token that Claude Code already keeps in
`~/.claude/.credentials.json`. It sends that token only to the API host that issued it.

```bash
export SUBBENCH_CLAUDE_USAGE_COMMAND="python3 $PWD/packaging/claude-usage.py"
subbench watch --provider claude --once
```

The same command asks Anthropic for the current plan. ccusage does not report one, and
the login file can lag an upgrade by days. A change of plan on the same account is
recorded on the next reading, and that reading starts a new series.

A `401` response means that the stored token has expired. Open Claude Code to refresh it.

If you do not set this variable, SubBench collects Claude cost but no Claude quota. It can
then produce no Claude estimate at all. `subbench doctor` reports this condition as an
error.

---

## 2. Arithmetic

### Step 1 — the value of the usage

SubBench uses the model identification, the token classes and the price table of ccusage.
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
one lifetime total from another. ccusage reports its full history at each run, and that
history is not stable between runs: two identical commands have returned reports that
differ by several days. A difference of two lifetime totals turns that instability into
large false deltas. A window-bounded sum does not depend on any day outside the window.

### Step 2 — the slope of each pair

Inside one reset window, each pair of observations where both the quota and the value
increase implies a value for the full window:

```text
slope(i, j) = (value_j - value_i) / ((quota_j - quota_i) / 100)
```

SubBench rejects a pair in four conditions:

| Condition | Reason |
|---|---|
| The quota moved less than 2 points. | A provider reports quota as a whole percent. A 1-point pair carries approximately 50% error. |
| The value did not move. | This shows an incomplete usage import, and not free quota. |
| The cost total is more than 30 minutes old. | The quota advances while a stopped collector holds the cost still. |
| The pair spans an interval of unobserved usage. | Refer to "Usage that ccusage cannot see". |

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

### Usage that ccusage cannot see

ccusage reads the logs on this machine only. Quota that a web interface, a cloud runner or
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
the accuracy, but it does not do better than daily bucketing. The ccusage row for the
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
   one import share a timestamp. SubBench never sends the raw ccusage payloads.
2. **The computed reports** — `current`, `history`, `models`, `weights` and `series`.
3. **The cursor** that the server returns to acknowledge the batch.

**The agent calculates the reports. The server keeps them.** The Worker contains no
estimator logic. The quota-span weighting, the exclusion of unobserved usage, the reset
clustering and the staleness limit stay in `subbench/regression.py`. A second
implementation would move away from the first one silently, and nothing could then show
which copy was correct.

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
| `/` Overview | The weekly value of each product, then one large chart with three views: weekly value, score against cost, and tasks per weekly limit. |
| `/subscriptions/` | One block per provider (its plans side by side, then its model mix), the value over time, the consistency checks and the completed windows. |
| `/models/` | Every public benchmark in one chart the size of the window. Pick a benchmark, then any two axes (its score, its cost, time or tokens, or another benchmark's score); a third axis makes the chart 3D, or colours the 2D chart. The side panel holds the best trade-offs (non-dominated on the chosen axes), the cheapest model at a target score, and in 3D the camera and a budget. Every measured plan (pooled across the accounts that report it) covers its provider's models (Claude plans Anthropic's, ChatGPT plans OpenAI's): each plan is an axis ("Tasks / week · Claude Max 5X"), "Plan-covered" filters to the models some plan covers, and every hover lists tasks per week on each covering plan. The site presents aggregates only: no page names or identifies an account. The **Plans** view is the same join as a matrix: the chosen benchmark's models down the side, measured plans across, each cell the tasks one weekly limit buys.

**How a plan and a benchmark meet.** Both sides are priced in API dollars: SubBench measures what a plan's weekly limit delivers at public API prices, and each benchmark measures what one task costs at the same prices. Tasks per week is their ratio. Token counts are not divided directly, because the two sides spend different token classes (a week of agent use is mostly cache reads; a benchmark's tokens per task are mostly output), and the price table is what makes the classes comparable. A per-class allowance reported by the collector would allow a token-based conversion as well. `/frontier/` and `/surface/` redirect here. |

The rules for what the pages show are unchanged. A figure appears only for a completed,
confirmed window. A window that passed every check except the width of its interval shows
a range only. A product that is measured but has nothing settled still gets a card. A
stale report or an unreachable API is said in the sidebar and the footer.

**What one week buys.** The overview divides the measured weekly value of a product by the
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
| `web/src/app/` | The shell, the data loading and the Plotly charts. |
| `web/src/conduit/` | Conduit components v2, copied from the Conduit repository. AGPL-3.0; refer to its README. |
| `web/public/data/frontier.json` | The benchmark bundle. `LLM-Pareto/export-web-data.py` writes it. |

The chrome uses Conduit's tokens only. The chart series use one validated categorical
palette, because Conduit's category palette fails the colour-vision checks as scatter
series. The Models chart is ECharts, with echarts-gl for 3D, so 2D and 3D are one engine on one canvas and switching
redraws in place. The overview and subscription trend charts use Plotly's basic bundle. Both load on demand.

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
# Python 3.11 or later
pip install -e .

npx ccusage@latest codex daily --json >/dev/null
subbench watch --provider codex --once

subbench watch
```

### Automatic start on Linux or WSL

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/subbench.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now subbench
journalctl --user -u subbench -f
```

The unit expects `subbench` at `~/.local/bin/subbench`. Change `ExecStart` if necessary.
The environment of the unit must contain the directories of `codex` and `npx`. WSL must
have systemd enabled.

> Restart the service after you change the code. An editable installation does not update
> a process that already runs. The page then shows old numbers with no warning.

---

## Commands

```bash
subbench status                          # what each product is worth, and is collection healthy
subbench frontier                        # what one weekly limit buys on the benchmark frontier
subbench frontier --metric list          # the benchmark lenses in the bundle
subbench values                          # every window measurement
subbench values --tier confirmed         # only the settled ones
subbench values --product Claude         # one product
subbench values --account 9cb152f0       # one account
subbench values --window weekly --json   # machine-readable
subbench chart                           # the same pooled series the site plots
subbench chart --window weekly --product ChatGPT

subbench watch                           # continuous collection
subbench watch --provider codex --once   # one diagnostic snapshot
subbench collect codex --report daily    # manual or backfill collection
subbench doctor                          # check the dependencies and the freshness

subbench sync push                       # send the evidence to the site
subbench sync status                     # endpoint, agent, cursor, last error
subbench sync reset --yes                # new agent identity, send everything again

subbench data path                       # where the database is, and how big
subbench data imports                    # audit the raw usage imports
subbench data import payload.json --provider claude
subbench data backup ~/subbench-backup.sqlite3
subbench data prune --days 90 --yes      # drop raw usage older than the cutoff
subbench data reset --yes                # delete every observation

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

The valuation is only as accurate as the retained telemetry and the ccusage price table.
The inference stays empirical. A meter can round, a limit can roll, a provider can apply a
model-specific weight, and a multiplier can change. The results are workload-specific
estimates of an API-equivalent allowance. They are not contractual quotas.

---

## Licence

MIT
