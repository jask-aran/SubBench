# SubBench

SubBench estimates the **API-equivalent entitlement value** delivered by coding-agent subscriptions. It joins exact token usage reported by tools such as Codex CLI and Claude Code with observed changes in their subscription usage windows.

It is not an estimate of OpenAI or Anthropic's internal cost. It answers a narrower and more reproducible question:

> At public API prices, how much usage did this subscription entitlement deliver under the observed model mix and workload?

## Method

For each model and interval, SubBench values the provider-reported token classes at the API prices that applied when the usage occurred.

For Codex:

```text
V = (T_input - T_cached) × P_input
  + T_cached × P_cached
  + T_output × P_output
```

`reasoning_output_tokens`, when exposed separately, is retained for analysis but is not added again: it is a subset of billed output tokens.

For Claude Code:

```text
V = T_input × P_input
  + T_cache_write × P_cache_write
  + T_cache_read × P_cache_read
  + T_output × P_output
```

Claude thinking tokens are billed as output. They do not need a separate price class to calculate API equivalence, although SubBench will retain a separate thinking count where the source exposes one.

Between two entitlement snapshots:

```text
quota_delta = usage_end - usage_start
implied_full_entitlement = api_value_between_snapshots / quota_delta
```

For example, if A$4.80 of API-equivalent usage moves a five-hour meter from 21% to 37%, the implied value of a complete window is:

```text
A$4.80 / 0.16 = A$30.00
```

Actual records are stored in USD because the source API price tables are denominated in USD. Reporting currencies can be added later without changing the underlying observation.

A single interval is noisy because usage meters may be rounded and limits may be rolling or dynamically weighted. SubBench will therefore estimate the slope of cumulative API-equivalent value against cumulative quota utilisation across many intervals, segmented by provider, model, window and workload where necessary.

## Architecture

```text
Codex CLI / Claude Code
        │
        ▼
provider-reported local usage logs
        │
        ▼
ccusage --json
        │
        ▼
SubBench ccusage adapter
  - preserves raw payload
  - normalises token classes
  - records model and time bounds
        │
        ▼
SQLite observation store
        │
        ├── historical API pricing
        ├── entitlement snapshots
        └── interval estimator
                │
                ▼
API-equivalent entitlement value
and subscription yield reports
```

The system deliberately separates four layers:

1. **Usage evidence** — exact token counts and model identifiers produced by the coding CLI and normalised by ccusage.
2. **Pricing evidence** — timestamped prices by provider, model and token class.
3. **Entitlement evidence** — percentage used/remaining, window type and reset timestamp obtained from the provider account interfaces.
4. **Inference** — joins usage value to quota movement and reports estimates with sample size and uncertainty.

Raw ccusage JSON is retained alongside normalised rows. This makes imports auditable and allows future parser versions to rebuild the database when ccusage changes its output schema.

## Current state

The initial implementation imports Claude Code and Codex JSON produced by ccusage into SQLite. It supports a saved file, standard input, or running ccusage directly.

```bash
# Python 3.11+
python -m subbench init

# Run ccusage and ingest its JSON output
python -m subbench collect claude --report daily
python -m subbench collect codex --report daily

# Import an existing payload
npx ccusage@latest claude daily --json > claude.json
python -m subbench ingest claude.json --provider claude --report daily

# Pipe JSON directly
npx ccusage@latest codex daily --json \
  | python -m subbench ingest - --provider codex --report daily

python -m subbench imports
```

By default the database is stored at `~/.local/share/subbench/subbench.sqlite3`. Override it with `--database` or `SUBBENCH_DATABASE`.

## Normalised usage schema

Each imported row records:

```text
provider
report type
period start / end
model
input tokens
cached input tokens
cache-write tokens
cache-read tokens
output tokens
reasoning output tokens
reported API-equivalent cost, when ccusage supplies it
raw import identifier
```

The importer prefers per-model breakdowns. Aggregate rows are retained only when the ccusage payload does not contain a usable model breakdown, because applying model-specific prices to a mixed aggregate would be incorrect.

## Planned work

- Verify and fixture the current ccusage JSON variants for Claude and Codex.
- Add timestamped model pricing and independently recomputed API-equivalent value.
- Poll Codex entitlement through `codex app-server` and Claude entitlement through its account usage interface.
- Match quota snapshots to token events without crossing reset boundaries.
- Estimate full-window value using robust regression rather than isolated divisions.
- Report subscription yield: API-equivalent value consumed divided by subscription price.

## Measurement limits

SubBench can make token valuation exact to the telemetry and public price table, but the entitlement inference remains empirical. Providers may round displayed percentages, use rolling windows, apply model-specific weights, change limits temporarily, or maintain separate pools for different features. Results should therefore be described as workload-specific API-equivalent allowance estimates, not fixed contractual quotas.

## Licence

MIT
