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

## Terminal charts

SubBench uses `plotext` to draw charts directly in a normal terminal. It remains a CLI rather than becoming an interactive TUI.

```bash
subbench chart
subbench chart --provider codex
subbench chart --provider codex --window weekly
subbench chart --width 120 --height 32
subbench chart --min-quota-span 10
```

The chart plots one robust API-equivalent value estimate per informative reset window. Separate series are drawn for each provider and quota window. It is intended for quick on-demand inspection; `subbench report --json` remains the stable machine-readable interface.

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

SubBench reports the median of all valid pairwise slopes. This Theil–Sen-style estimate is resistant to rounded quota percentages and isolated anomalous intervals. The 10th–90th percentile slope range is an empirical stability range, not a formal confidence interval.

A regression is never combined across different reported reset timestamps. Points with unchanged rounded quota do not create slopes, but remain useful once a later snapshot moves the meter.

## Current value over time

Each reset timestamp produces a separate robust estimate. SubBench then treats those reset-window estimates as a time series rather than fitting one permanent regression across all history.

The default report calculates a rolling current value using recent informative windows:

- up to 10 weekly windows;
- up to 30 five-hour windows;
- only windows with at least 5 percentage points of observed quota movement by default;
- weighted by observed quota span, capped at one full entitlement.

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
