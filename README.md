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

SubBench samples cumulative local usage every 60 seconds, so it does not proxy or modify Codex/Claude requests. Changed ccusage payloads and entitlement snapshots are retained in `~/.local/share/subbench/subbench.sqlite3`; identical usage snapshots are discarded by hash.

### Automatic startup on Linux or WSL

```bash
mkdir -p ~/.config/systemd/user
cp packaging/systemd/subbench.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now subbench
journalctl --user -u subbench -f
```

The service assumes `subbench` is installed at `~/.local/bin/subbench`. Adjust `ExecStart` where necessary. WSL must have systemd enabled.

## Entitlement collection

Codex uses the supported local app-server interface. SubBench starts `codex app-server`, performs the JSON-RPC handshake, calls `account/rateLimits/read`, and stores each returned window's used percentage, duration and reset timestamp.

Claude Code does not currently provide an equivalent supported local RPC. Set `SUBBENCH_CLAUDE_USAGE_COMMAND` to a local command that prints the Claude usage response as JSON:

```bash
export SUBBENCH_CLAUDE_USAGE_COMMAND='your-local-claude-usage-helper --json'
subbench watch --provider claude --once
```

Expected fields are `five_hour` and `seven_day` (or `weekly`), each containing `utilization` and optionally `resets_at`. Claude token collection continues when this command is unavailable; only entitlement inference is skipped.

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

Within each reset window, every pair of cumulative observations with increasing quota and non-decreasing API value implies a full-window value:

```text
slope(i, j) = (api_value_j - api_value_i)
              / ((usage_j - usage_i) / 100)
```

SubBench reports the median of all valid pairwise slopes. This Theil–Sen-style estimate is resistant to rounded quota percentages and isolated anomalous intervals. As more observations and a wider quota span accumulate, the number of usable slopes grows and the estimate generally stabilises.

The report also shows the 10th–90th percentile range of observed slopes. This is an empirical stability range, not a formal confidence interval.

```bash
subbench report
subbench report --provider codex
subbench report --json
subbench report --intervals  # raw adjacent-interval diagnostics
```

A regression is never combined across different reported reset timestamps. Points with unchanged rounded quota do not create slopes, but remain useful once a later snapshot moves the meter.

## Architecture

```text
Codex CLI / Claude Code
        │ write provider-reported local usage
        ▼
      ccusage ──────────────┐
                            │
Codex app-server ───────────┤ every minute
Claude usage helper ────────┘
                            ▼
                    SubBench watcher
                    - raw usage evidence
                    - normalised token classes
                    - quota/reset snapshots
                            ▼
                          SQLite
                            ▼
              reset-separated robust regression
                            ▼
              API-equivalent entitlement value
```

Snapshot collection is recoverable: the coding CLIs retain cumulative usage, so token activity generated during brief SubBench downtime is captured on the next successful sample. Finer entitlement movement during that downtime cannot be reconstructed.

## Commands

```bash
subbench watch                          # continuous usage + entitlement collection
subbench watch --provider codex --once # diagnostic snapshot
subbench report                         # robust full-window estimates
subbench report --intervals             # raw interval estimates
subbench collect codex --report daily  # manual/backfill usage snapshot
subbench ingest payload.json --provider claude --report daily
subbench imports                        # audit raw usage imports
```

## Evidence model

SubBench keeps three layers separate:

1. **Usage and pricing evidence:** provider-reported token classes and ccusage's API-equivalent USD calculation.
2. **Entitlement evidence:** usage percentage, reset time and window identity sampled from account interfaces.
3. **Inference:** reset-separated robust regression over cumulative observations.

Raw ccusage JSON is preserved so parser changes can be audited and historical observations rebuilt.

## Measurement limits

Token valuation is only as accurate as the retained provider telemetry and ccusage pricing. Entitlement inference remains empirical: meters may be rounded, limits may roll, providers may apply model-specific weights, and allowance multipliers may change. Results are workload-specific API-equivalent allowance estimates, not contractual quotas.

## Licence

MIT
