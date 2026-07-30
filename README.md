# SubBench

SubBench continuously estimates the **API-equivalent entitlement value** delivered by coding-agent subscriptions. It joins exact token usage reported by Codex CLI and Claude Code with observed changes in their subscription usage windows.

It does not estimate OpenAI or Anthropic's internal cost. It answers:

> At public API prices, how much usage did this subscription entitlement deliver under the observed model mix and workload?

## Run continuously

```bash
# Python 3.11+
pip install -e .

# Verify collection once
npx ccusage@latest codex daily --json >/dev/null
subbench watch --provider codex --once

# Then leave it running, normally through the included systemd user service
subbench watch
```

The watcher is event-driven. It scans only file metadata every two seconds, waits five seconds for a burst of log writes to settle, then runs ccusage and captures entitlement. A full ccusage reconciliation runs every six hours even if no change was detected. This keeps ccusage as the pricing and schema authority without launching Node processes every minute.

Changed ccusage payloads and entitlement snapshots are retained in `~/.local/share/subbench/subbench.sqlite3`; identical usage snapshots are discarded by hash.

Useful controls:

```bash
subbench watch --interval 1       # filesystem metadata scan cadence
subbench watch --debounce 10      # wait for logs to settle
subbench watch --reconcile 3600   # maximum time between full reconciliations
```

### Automatic startup on Linux or WSL

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/subbench.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now subbench
journalctl --user -u subbench -f
```

The service assumes `subbench` is installed at `~/.local/bin/subbench`. Adjust `ExecStart` where necessary. WSL must have systemd enabled.
Its environment must also include the directories containing `codex` and `npx`; the included unit covers this machine's Homebrew installation.

## Web dashboard

SubBench can publish its estimates to a Cloudflare Worker backed by D1, with a public page
and token-authenticated ingest.

```bash
export SUBBENCH_PUSH_URL=https://subbench.example.com/ingest
export SUBBENCH_PUSH_TOKEN=...
subbench push                    # send evidence recorded since the last acknowledgement
```

`subbench watch` pushes hourly on its own clock once those variables are set, so a slow or
unreachable server never delays a quota reading and a push failure never fails a
collection cycle. The local database stays the source of truth.

Derivation runs **server-side**: the Worker imports the same `subbench.regression` code
the CLI uses. An estimator improvement is a redeploy, and re-derives all stored history
without any agent re-pushing. Nothing re-implements the estimator, because every subtle
defect this project has hit lived in that code and a second copy would drift silently.

Raw ccusage payloads are not sent — they are most of the local database by size and the
estimator never reads them.

Deploying:

```bash
wrangler d1 create subbench
wrangler d1 execute subbench --file src/subbench/server/schema.sql --remote
wrangler secret put SUBBENCH_INGEST_TOKEN
wrangler deploy
```

### Confidence tiers

Every estimate is shown; the tier sets how prominently. Suppressing weak estimates would
leave a blank page for the first hours of collection and would hide the series that most
needs explaining.

| tier | requirement |
|---|---|
| `confirmed` | meets `likely`, and a slope band within 70% of the estimate **or** cross-window agreement within 15% |
| `likely` | at least 25 points of measured quota, 70% coverage, and 50 valid pairs |
| `provisional` | an estimate exists |

The Codex weekly series carries thousands of slopes and still reads `provisional`, because
half its quota movement was never measured locally. That is the label doing its job.

## Terminal charts

SubBench uses `plotext` to draw charts directly in a normal terminal. It remains a CLI rather than becoming an interactive TUI.

```bash
subbench chart
subbench chart --provider codex
subbench chart --provider codex --window weekly
subbench chart --width 120 --height 32
subbench chart --min-quota-span 10
subbench chart --provider codex --slopes
```

The chart is a compact plot of the running median estimate for the latest reset period. Each new entitlement observation adds valid pairwise slopes, so the line shows the estimate converging as evidence arrives. `--slopes` prints every valid pairwise slope contributing to the latest median, including its quota delta and API-value delta. The default plot is bounded to 78×16 terminal cells; `--width` and `--height` override that for larger displays. Use `subbench report --history` for one final estimate per historical reset window and `subbench report --json` as the stable machine-readable interface.

## Entitlement collection

Codex uses the supported local app-server interface. SubBench starts `codex app-server`, performs the JSON-RPC handshake, calls `account/rateLimits/read`, and stores each returned window's used percentage, duration and reset timestamp.
Codex reset timestamps are rounded to the minute because consecutive reads of one reset boundary can differ by a few seconds.

Claude Code does not currently provide an equivalent supported local RPC. Set `SUBBENCH_CLAUDE_USAGE_COMMAND` to a local command that prints the Claude usage response as JSON:

`packaging/claude-usage.py` is such a helper. It reads `claudeAiOauth.accessToken` from `~/.claude/.credentials.json` — the credentials Claude Code already stores — and queries the OAuth usage endpoint, sending the token only to the API host it belongs to.

```bash
export SUBBENCH_CLAUDE_USAGE_COMMAND="python3 $PWD/packaging/claude-usage.py"
subbench watch --provider claude --once
```

A `401` means the stored token expired; opening Claude Code refreshes it. Override `CLAUDE_CREDENTIALS` or `CLAUDE_USAGE_URL` if either moves. The included systemd unit sets this variable already.

Expected fields are `five_hour` and `seven_day` (or `weekly`), each containing `utilization` and optionally `resets_at`. An `account_uuid` (or `organization_uuid`) is used to scope snapshots per account, as Codex snapshots already are.

There is no `claude usage` subcommand — the `claude` CLI does not expose this, so the helper has to call the OAuth usage endpoint itself with your local credentials.

Without the helper, SubBench is **half-instrumented for Claude**: token costs accumulate as the numerator while no quota is ever observed as the denominator, so no Claude estimate can ever be produced. That is silent in the watcher log, so `subbench doctor` now reports it as an `error` rather than a warning:

```text
error  Claude entitlement helper  SUBBENCH_CLAUDE_USAGE_COMMAND is not set;
                                  Claude usage is priced but never valued
error  claude valuation           usage is recorded but no entitlement snapshot
                                  exists, so no estimate can be produced
```

Wiring this up matters beyond Claude coverage: Claude exposes a **five-hour** window, while a Codex `plus` account returns only a weekly one. Five-hour windows produce roughly 34 reset periods a week instead of one, which is what the multi-window aggregates are starved of.

## Method

SubBench relies on ccusage's model identification, token classes and API-cost calculation. For Codex:

```text
V = (T_input - T_cached) × P_input
  + T_cached × P_cached
  + T_output × P_output
```

For Claude Code:

```text
V = T_input × P_input
  + T_cache_write × P_cache_write
  + T_cache_read × P_cache_read
  + T_output × P_output
```

Reasoning or thinking tokens are retained separately where exposed, but are not added again when already included in billed output tokens.

Within each reset window, every pair of cumulative observations with increasing quota and increasing API value implies a full-window value:

```text
slope(i, j) = (api_value_j - api_value_i)
              / ((usage_j - usage_i) / 100)
```

The API value of a pair is summed over the days **inside that entitlement window**, not differenced from a lifetime running total. ccusage re-reports its whole history on every run and is not stable between runs: identical commands on the same account have returned reports differing by several whole days. Differencing a lifetime total turns that into large phantom deltas; window-bounded sums do not depend on days outside the window at all.

SubBench reports the **quota-weighted** median of all valid pairwise slopes, each slope weighted by the quota it spans. Weighting is what makes the estimate usable. Providers report quota as a whole integer percent, so a pair one point apart carries ±0.5% error in the denominator — roughly ±50% on that slope — and because the error enters through `1/delta` the resulting distribution is heavy-tailed to the right. Short pairs also outnumber long pairs quadratically, so an unweighted median is decided by the least reliable evidence and *degrades* as observations accumulate. Weighting by span makes one 50-point pair count for fifty 1-point pairs, which is the ratio of their information content. Pairs below `--min-pair-delta` (default 2%) are discarded outright.

Pairs where quota moved but recorded API value did not are dropped rather than counted as a zero slope: that pattern means a stale or incomplete usage import, not a stretch of free quota.

A quota reading is only used if the cost total beside it was confirmed within 30 minutes. When collection stops, quota keeps advancing while the last recorded cost stands still, and pairing the two badly understates the value of that quota. Because unchanged ccusage payloads are deduplicated, `imports.last_seen_at` records when a payload was last confirmed — that is what separates "unchanged" from "not collected".

### Model mix

Quota-per-dollar varies several-fold inside a single window, and the likeliest cause is which models the workload used. `subbench models` shows the token share of each model per reset window:

```text
codex  weekly  2026-08-05T04:11  gpt-5.6-terra  24,930,077  49.7%
codex  weekly  2026-08-05T04:11  gpt-5.6-sol    15,775,241  31.5%
codex  weekly  2026-08-05T04:11  gpt-5.5         8,777,584  17.5%
```

Solving for per-model quota weights would explain that variance instead of averaging it away, but it needs many windows with a *varying* mix — comfortably more than the number of distinct models, so more than a dozen. SubBench records the inputs and does not attempt the fit: a weight vector fitted to one window would look authoritative and be noise. Per-model cost is deliberately absent, because ccusage reports cost on the aggregate row and leaves model breakdowns null.

### Usage that ccusage cannot see

ccusage reads this machine's logs. Quota spent through a provider's web or cloud runner, or from another machine on the same account, burns entitlement while leaving no local tokens.

Such a stretch is not cheap usage, it is unmeasured usage: the numerator is missing rather than small. Worse, it spans a lot of quota, so under span weighting it would be the most heavily weighted evidence of all. SubBench therefore drops **any pair spanning an interval where quota moved at least 5 points while recorded value did not move** — including pairs whose own endpoints sit far outside it, since they inherit the same missing value. What remains is an estimate over observed evidence only, which implicitly assumes the unobserved quota was worth about what the observed quota was worth. That is a far better assumption than the alternative of treating it as worthless, but it is still an assumption.

`subbench doctor` reports how much quota this affected:

```text
warn  codex unobserved usage  45 percentage points of quota moved with almost no
                              locally recorded spend; estimates for that window
                              understate the entitlement
```

A window carrying that warning rests on less evidence than its quota span suggests, because the unobserved stretch and everything spanning it has been discarded. Reports therefore carry a **coverage** column: the share of observed quota movement that had recorded spend beside it. A window can show an 89% quota span and 49% coverage, and only the second number describes the evidence.

### Why not sub-day cost attribution

`ccusage <provider> session --json` exposes per-session timestamps, which looks like it should beat day-granularity cost. Measured against 93 dense observations, it does not:

| attribution | linearity of cost vs quota (R²) |
|---|---|
| daily buckets | **0.977** |
| session, at `lastActivity` | 0.054 |
| session, spread over its span | 0.974 |

Sessions have a median span of 30 minutes and a p90 of six hours, so attributing a session's whole cost at its last activity is severely late-biased. Spreading it uniformly recovers the accuracy but does not exceed daily bucketing, because ccusage's row for the current day is a **live running total** — it grows on every import, so intra-day resolution already matches the polling interval. Day boundaries only matter at the edges of a reset window.

The 10th–90th percentile slope range is an empirical stability range, not a formal confidence interval. A wide range is meaningful — it usually means quota-per-dollar genuinely varied inside the window because the model mix changed.

### Window average and marginal rate

Reports show two numbers, because there are two different questions:

- **window avg** — the quota-weighted median over every valid pair in the reset window. What the entitlement has averaged so far.
- **marginal** — the same calculation over pairs lying entirely within the most recent 20 quota points. What a quota point is worth *now*.

They diverge whenever the workload mix shifts mid-window, and the divergence is not noise. The window average is an average, so it keeps carrying the old rate long after the rate has changed; on one observed window the marginal estimate reported the new rate roughly four hours before the window average caught up:

```text
quota   window avg   marginal
  69%       $14.87    $14.47     rates still agree
  74%       $38.26    $80.50     marginal has switched, average is lagging
  84%       $96.32   $102.67     average has caught up
```

Use the window average to value the entitlement as a whole, and the marginal rate to see the current regime early. A large, sustained gap between them is the within-window analogue of what `detect_regime_changes` reports across windows.

Reset boundaries are clustered within five minutes before grouping. Providers report a stable boundary that wanders by a few seconds between reads, and rounding alone still splits a window whenever the boundary straddles a minute. Genuinely different windows are hours apart, so the tolerance cannot merge two real ones.

A regression is never combined across different reported reset timestamps. Points with unchanged rounded quota do not create slopes, but remain useful once a later snapshot moves the meter.

## Current value over time

Each reset timestamp produces a separate robust estimate. SubBench then treats those reset-window estimates as a time series rather than fitting one permanent regression across all history.

The default report calculates a rolling current value using recent informative windows:

- up to 10 weekly windows;
- up to 30 five-hour windows;
- only windows with at least 5 percentage points of observed quota movement by default;
- weighted by observed quota span, capped at one full entitlement.

### How quickly it converges

A single reset window becomes usable as soon as it has spanned a decent fraction of its quota — hours to a day or two of ordinary use, not weeks. The within-window estimate is what `subbench chart` plots.

The **multi-window** aggregates are the slow ones, and only because they are sized in whole reset periods: the rolling value wants up to 10 windows and regime detection needs at least 6 before it will report anything. For a five-hour window that is a day or two; for a weekly window that is genuinely weeks to months. If a provider exposes only a weekly window, expect one estimate per week feeding those aggregates no matter how densely you sample.

Sampling density stops helping quickly. Once observations are dense enough that the quota meter advances between them, extra snapshots at the same integer percent add pairs with either zero quota delta (discarded) or a 1-point delta (below the floor). What is scarce is quota **span**, not sample count.
```bash
subbench report
subbench report --provider codex
subbench report --json
subbench report --history
subbench report --intervals
subbench chart
```

`--history` shows one robust estimate per reset window. `--intervals` retains raw adjacent-snapshot calculations for debugging. `chart` renders the same reset-window history visually.

## Detecting changed limit regimes

SubBench compares the latest three informative reset windows with the preceding baseline. It reports a possible regime change only when:

- the recent median differs from the baseline median by at least 20%;
- all three recent estimates lie on the same side of the baseline;
- at least three earlier informative windows exist.

A change is labelled `developing` with three or four baseline windows and `likely` once at least five baseline windows support the old regime.

The date means the first reset window in which SubBench observed the new level. It is not necessarily the exact backend-change date, particularly if the harness was unused around the transition.

A sustained jump is evidence of a changed **effective API-equivalent entitlement under the observed workload**. It does not by itself prove that every subscriber received a fixed token increase. A provider may change model weights, temporary multipliers or account segmentation, and a major change in model/workload mix can also move the estimate.

## Architecture

```text
Codex CLI / Claude Code
        │ append local JSONL logs
        ▼
metadata-only file watcher
        │ change burst + debounce
        ▼
      ccusage ──────────────┐
                            │
Codex app-server ───────────┤ on change or reconciliation
Claude usage helper ────────┘
                            ▼
                    SQLite evidence store
                            ▼
              reset-separated robust regression
                            ▼
               time series + terminal charts
                            ▼
              rolling value + regime detection
```

The filesystem watcher does not parse or modify agent logs. It only notices new, appended, replaced or removed JSONL files. ccusage remains responsible for interpreting those logs and calculating API-equivalent cost. Periodic reconciliation recovers missed filesystem events and usage generated while SubBench was offline.

## Commands

```bash
subbench watch                          # incremental continuous collection
subbench watch --provider codex --once # diagnostic snapshot
subbench chart                          # terminal value history
subbench chart --window weekly          # one quota-window type
subbench report                         # rolling current value and regime changes
subbench report --history               # one estimate per reset window
subbench report --intervals             # raw adjacent-interval diagnostics
subbench chart --slopes                  # audit every current-period slope
subbench collect codex --report daily  # manual/backfill usage snapshot
subbench ingest payload.json --provider claude --report daily
subbench imports                        # audit raw usage imports
```

## Overhead

While idle, SubBench performs recursive directory scans of file names and metadata only; it does not open every JSONL file. CPU use should remain near zero and the Python process should remain in the tens-of-megabytes memory range. The heavier Node/ccusage and entitlement subprocesses now run only after relevant log changes or at the six-hour reconciliation boundary.

A very large archive containing hundreds of thousands of session files would still make metadata scans non-trivial. A future platform-native filesystem notification backend could remove even that scan, but the current approach stays dependency-light and works consistently across Linux, WSL and macOS-style filesystems.

## Evidence model

SubBench keeps three layers separate:

1. **Usage and pricing evidence:** provider-reported token classes and ccusage's API-equivalent USD calculation.
2. **Entitlement evidence:** usage percentage, reset time and window identity sampled from account interfaces.
3. **Inference:** reset-separated robust regression, rolling current value and conservative structural-break detection.

Raw ccusage JSON is preserved so parser changes can be audited and historical observations rebuilt. Derived estimates are recomputed from that evidence rather than treated as irreversible source data.

## Measurement limits

Token valuation is only as accurate as the retained provider telemetry and ccusage pricing. Entitlement inference remains empirical: meters may be rounded, limits may roll, providers may apply model-specific weights, and allowance multipliers may change. Results are workload-specific API-equivalent allowance estimates, not contractual quotas.

## Licence

MIT
