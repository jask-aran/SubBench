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

Claude Code does not currently provide an equivalent supported local RPC. SubBench therefore keeps Claude entitlement access replaceable rather than embedding credentials or a private endpoint. Set `SUBBENCH_CLAUDE_USAGE_COMMAND` to a local command that prints the Claude OAuth usage response as JSON:

```bash
export SUBBENCH_CLAUDE_USAGE_COMMAND='your-local-claude-usage-helper --json'
subbench watch --provider claude --once
```

Expected fields are `five_hour` and `seven_day` (or `weekly`), each containing `utilization` and optionally `resets_at`. Utilisation may be represented as either 0–1 or 0–100. Claude token collection continues when this command is absent or fails; only entitlement inference is unavailable.

## Method

For Codex:

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

Between two snapshots in the same reset window:

```text
quota_delta = usage_end - usage_start
api_value_delta = cumulative_api_value_end - cumulative_api_value_start
implied_full_entitlement = api_value_delta / (quota_delta / 100)
```

For example, US$4.80 of API-equivalent usage moving a five-hour meter from 21% to 37% implies:

```text
US$4.80 / 0.16 = US$30.00
```

Run:

```bash
subbench report
subbench report --provider codex
subbench report --json
```

The estimator rejects intervals that cross a reported reset, have decreasing quota utilisation, or have decreasing cumulative cost. It currently uses ccusage's per-model API-cost calculation. Historical first-party pricing tables and robust multi-interval regression remain planned.

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
                 reset-safe interval estimator
                            ▼
              API-equivalent entitlement value
```

Snapshot collection is deliberately recoverable: the coding CLIs retain cumulative usage, so token activity generated during brief SubBench downtime is captured on the next successful sample. Finer entitlement movement during that downtime cannot be reconstructed.

## Commands

```bash
subbench watch                          # continuous usage + entitlement collection
subbench watch --provider codex --once # diagnostic snapshot
subbench report                         # inferred full-window values
subbench collect codex --report daily  # manual/backfill usage snapshot
subbench ingest payload.json --provider claude --report daily
subbench imports                        # audit raw usage imports
```

## Evidence model

SubBench keeps four layers separate:

1. **Usage evidence:** exact provider-reported token classes and model identifiers, normalised from ccusage JSON.
2. **Pricing evidence:** currently ccusage's model pricing and reported USD cost; timestamped independent pricing is planned.
3. **Entitlement evidence:** usage percentage, reset time and window identity sampled from account interfaces.
4. **Inference:** interval joins that never intentionally cross a reset boundary.

Raw ccusage JSON is preserved so parser changes can be audited and historical observations rebuilt.

## Measurement limits

Token valuation can be exact to the retained provider telemetry and applicable public price table. Entitlement inference remains empirical: meters may be rounded, limits may roll, providers may apply model-specific weights, and allowance multipliers may change. Results are workload-specific API-equivalent allowance estimates, not contractual quotas.

## Licence

MIT
