# SubBench

SubBench continuously estimates the **API-equivalent entitlement value** delivered by coding-agent subscriptions. It joins exact token usage reported by Codex CLI and Claude Code with observed changes in their subscription usage windows.

It does not estimate OpenAI or Anthropic's internal cost. It answers:

> At public API prices, how much usage did this subscription entitlement deliver under the observed model mix and workload?

## Continuous collection

SubBench is intended to run in the background while Codex CLI and Claude Code are used. It periodically asks ccusage for the current cumulative usage records, preserves changed payloads, and normalises token counts into SQLite. Identical snapshots are discarded by content hash.

```bash
# Python 3.11+
pip install -e .

# Foreground test: collect both providers once
subbench watch --once

# Continuous collection of Claude Code and Codex
subbench watch

# Or one provider only
subbench watch --provider codex --interval 60
```

The default database is `~/.local/share/subbench/subbench.sqlite3`. Override it with `--database` or `SUBBENCH_DATABASE`.

### Start automatically on Linux or WSL

The repository includes a systemd user service:

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/subbench.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now subbench
journalctl --user -u subbench -f
```

The service assumes the `subbench` executable is at `~/.local/bin/subbench`. Adjust `ExecStart` when installed elsewhere. On WSL, systemd must be enabled. Other service managers can run the same long-lived command:

```bash
subbench watch --provider all --interval 60
```

The manual `collect` and `ingest` commands remain available for debugging and backfills; they are not the normal operating mode.

## Method

For each model and interval, SubBench values provider-reported token classes at the public API prices that applied when the usage occurred.

For Codex:

```text
V = (T_input - T_cached) × P_input
  + T_cached × P_cached
  + T_output × P_output
```

`reasoning_output_tokens`, where separately exposed, is retained for analysis but not added again because it is a subset of billed output tokens.

For Claude Code:

```text
V = T_input × P_input
  + T_cache_write × P_cache_write
  + T_cache_read × P_cache_read
  + T_output × P_output
```

Claude thinking tokens are billed as output. A separate thinking count is useful analytically but is unnecessary for API-equivalent pricing when already included in output tokens.

Between two entitlement snapshots:

```text
quota_delta = usage_end - usage_start
implied_full_entitlement = api_value_between_snapshots / quota_delta
```

If US$4.80 of API-equivalent usage moves a five-hour meter from 21% to 37%, the implied value of a complete window is:

```text
US$4.80 / 0.16 = US$30.00
```

A single interval is noisy because meters may be rounded and limits may be rolling or dynamically weighted. SubBench should estimate the slope of cumulative API-equivalent value against cumulative quota utilisation across many intervals, segmented by provider, model and limit window where necessary.

## Architecture

```text
Codex CLI / Claude Code write local session logs
                    │
                    ▼
          ccusage reads cumulative logs
                    │
          every minute while SubBench runs
                    ▼
          SubBench background watcher
          - preserves changed raw JSON
          - normalises exact token classes
          - records model and time bounds
                    │
                    ▼
             SQLite evidence store
                    │
                    ├── historical API prices
                    ├── entitlement snapshots
                    └── interval estimator
                            │
                            ▼
          API-equivalent entitlement value
             and subscription yield
```

Collection is snapshot-based rather than request-proxy-based. Codex and Claude Code already persist provider-reported token usage locally, so SubBench does not need to sit between the agent and provider. Polling cumulative logs also lets it recover usage generated while the watcher was briefly offline.

The system separates four evidence layers:

1. **Usage evidence** — exact token counts and model identifiers produced by the coding CLI and normalised by ccusage.
2. **Pricing evidence** — timestamped prices by provider, model and token class.
3. **Entitlement evidence** — percentage used or remaining, window type and reset timestamp from provider account interfaces.
4. **Inference** — joins usage value to quota movement and reports estimates with sample size and uncertainty.

Raw ccusage JSON is retained alongside normalised rows so imports remain auditable and future parser versions can rebuild the database after schema changes.

## Current commands

```bash
# Continuous default
subbench watch

# One snapshot
subbench collect claude --report daily
subbench collect codex --report daily

# Import existing JSON
subbench ingest claude.json --provider claude --report daily

# Inspect stored snapshots
subbench imports
```

## Normalised usage schema

Each usage row records provider, report type, period bounds, model, uncached and cached input, cache-write and cache-read tokens, output tokens, reasoning output tokens when available, ccusage-reported cost, and the raw import identifier.

The importer prefers per-model breakdowns. Aggregate rows are retained only when the payload has no usable model breakdown, because applying model-specific prices to a mixed aggregate would be incorrect.

## Next work

- Fixture current Claude and Codex ccusage JSON variants from real histories.
- Replace periodic full ccusage scans with incremental adapters where useful, while retaining backfill recovery.
- Add timestamped model pricing and independently recomputed API-equivalent value.
- Snapshot Codex entitlement through `codex app-server` and Claude entitlement through its account usage interface.
- Match quota snapshots to usage without crossing reset boundaries.
- Estimate full-window value using robust regression.
- Report subscription yield: API-equivalent value consumed divided by subscription price.

## Measurement limits

Token valuation can be exact to provider telemetry and the public price table. Entitlement inference remains empirical: providers may round displayed percentages, use rolling windows, apply model-specific weights, temporarily change limits, or maintain separate pools. Results are workload-specific API-equivalent allowance estimates, not contractual quotas.

## Licence

MIT
