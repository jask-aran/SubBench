# Environment validation

Most of SubBench is covered by deterministic unit and synthetic database tests. The remaining risk is integration with live Codex CLI, Claude Code, ccusage, terminal capabilities and platform-specific log locations.

## Local diagnostics

Run:

```bash
subbench doctor
subbench doctor --provider codex
subbench doctor --json
```

`doctor` checks executable discovery, detected JSONL logs, database integrity, the latest usage and entitlement observations, and Claude entitlement-helper configuration. Warnings indicate missing optional evidence; errors produce a non-zero exit status.

## Codex smoke test

```bash
pytest
subbench doctor --provider codex
subbench watch --provider codex --once
subbench report --provider codex --history
subbench chart --provider codex
```

Then leave the watcher running, complete several Codex turns and verify:

1. appending a Codex JSONL file triggers exactly one debounced ccusage collection;
2. unchanged logs do not repeatedly launch ccusage;
3. the usage import precedes or closely matches the entitlement timestamp;
4. restarting the watcher does not duplicate evidence;
5. a quota reset creates a distinct reset-window estimate;
6. `codex app-server` exits cleanly without orphaned processes.

## Claude smoke test

Configure `SUBBENCH_CLAUDE_USAGE_COMMAND`, then repeat the same flow with `--provider claude`. Capture an anonymised ccusage JSON payload and entitlement payload for committed fixtures.

## Platform matrix

Validate at least:

- Linux with default paths;
- WSL with systemd enabled;
- custom `CODEX_HOME`;
- large session archive;
- log append, replacement, truncation and deletion;
- narrow and wide terminals for `plotext` rendering.

## Long-run acceptance

Run the service for at least one complete weekly quota window. Record:

- idle CPU and resident memory;
- ccusage launches per active session and per idle day;
- SQLite growth;
- missed or duplicated collections;
- agreement between SubBench imports and a manual ccusage report;
- stability of reset timestamps and regression output.

Open GitHub issues track the live-environment work that cannot be completed through a repository-only environment.
