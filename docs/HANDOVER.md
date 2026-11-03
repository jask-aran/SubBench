# Handover: SubBench 0.3, as of 2026-11-03

For the agent taking over development on the user's machine. Read this and CONTRIBUTING.md
before changing anything.

## State

- `main` and `claude/llem-pareto-subbench-integration-50pgv9` are both at `6064020`.
  `pytest` passes 312 tests; `node --test tests/*.mjs` passes 38.
- The machine is upgraded through step 7 of docs/UPGRADE-0.3.md, and stopped at step 8.
  Only derived figures differ; nothing recorded has been lost (see below).
- Backups: `~/.local/share/subbench/subbench.sqlite3.pre-0.3-switch.bak` (36,176 readings,
  taken just before the switch) and the older `subbench.sqlite3.pre-0.3.bak`. Keep both.
- Agent id `ecbbd8ba-0853-478d-a2f2-01c182c8905d`, push URL
  `https://subbench.jask-aran.com/ingest`. The site has readings up to 00:02 UTC on
  2026-11-03, sent by the one 0.3 cycle in step 7.
- The site, the Worker and D1 are deployed and need nothing. The Worker hashes account ids
  on ingest; D1 was migrated to hashed ids. Deploys are the user's (`npx wrangler deploy`).

## Readings are the irreplaceable data; estimates are not

A quota reading (`entitlement_snapshots`) is a moment that cannot be re-read later. Usage can
be re-read from the agents' logs for as long as they keep them, and every estimate is
recomputed from readings plus usage. So:

1. Keep the collector running. A stopped collector is the only way to lose data here.
2. Estimates can be argued about afterwards. Any estimator change recomputes the whole
   history: `window_estimates` is keyed by `ESTIMATOR_POLICY` (timeline.py), so bumping
   the policy recomputes every stored window on the next cycle.

## Open work, most urgent first

1. **Start the collector (UPGRADE step 9).** `~/.config/systemd/user/subbench.service` is a
   symlink into the checkout (`packaging/systemd/subbench.service`), and
   `service.install` writes with `write_text`, which follows the link and would overwrite
   the tracked file. Remove the link first (`rm` the link, not its target), then
   `subbench service install`. Then fix `service.py` to unlink an existing symlink before
   writing, with a test. `subbench.service.0.2` is a copy of the repo file, not the 0.2
   unit. The real 0.2 unit is `git show d307836:packaging/systemd/subbench.service`. Fix
   the rollback section of the guide to match.

2. **`check-estimates` overstates changes (`src/subbench/compare.py`).** `drift` is the
   largest relative move among `estimate_usd`, `lower_usd` and `upper_usd`, but the line
   prints only the estimate. A lower bound near $0 gives figures like +15299888461538.5%.
   Count and print the estimate's change on its own, report bound changes separately, and
   add a test for a case like that one. Then rerun it against the switch backup to get the
   true count of the 56.

3. **Explain the step 8 differences on the real database.** Separate the deliberate v3
   rule (each window's peer reference comes only from windows that closed before it) from
   anything else. `tests/reference_estimator.py` is the old estimator, kept as the test
   oracle. Run it over `regression_points(db)` from the live database and compare with
   0.3's figures window by window. What is left after the rule change is a bug. Look at:
   - the 4 missing windows: Claude pro five_hour, resets 2026-09-05T03:30 and
     2026-09-25T17:30, and the weekly `~via~five_hour` windows derived from them. A window
     with no valid pairs after the reference changed would be dropped; check that first;
   - Claude max_5x five_hour, reset 2026-10-20T16:20: $0.29 to $150.59. The old figure
     looks wrong and the new one plausible, but confirm it;
   - Claude pro, reset 2026-09-14T03:00: $54.34 to $106.57;
   - Codex plus weekly, reset 2026-08-06T14:40: $134.34 to $87.27.
   If the v3 rule is judged wrong, revert it and bump `ESTIMATOR_POLICY`.

4. **"codex unobserved usage" (45 points with almost no local spend) is not the reader.**
   Every Codex day matched ccusage (step 4), so this is quota spent where this machine's
   logs cannot see it: Codex web or cloud tasks, or another machine on the same account.
   The doctor warning is right to show it. The paused 0.3.0 attempt also left one import
   from the old, incomplete reader (`subbench data imports`, command `subbench codex
   logs`, from before `6064020`). It priced at most one cycle of Codex readings low. Leave
   it unless the user asks otherwise.

5. **Not started; each needs the user's go-ahead:**
   - the regime detector should start a new series when the plan changes;
   - using the hourly buckets (`usage_hourly`) in the estimator;
   - tagging v0.3.0 to run `.github/workflows/release.yml`. Tags and releases are
     outward-facing, so ask first.

## Swapping ccusage back in

The ccusage path is kept: `subbench watch --runner npx` runs `npx --yes ccusage@latest
<provider> daily --json` per cycle and stores what it prints, as 0.2 did. To make the
service use it, add `--runner npx` to the unit's `ExecStart` (`service install` has no
runner option yet; adding one is a small change). Costs of running it that way:

- it needs Node on the service's PATH;
- it fetches from npm outside `net.py`, so `subbench privacy log` will not show that
  traffic. Say so if the user cares about the privacy claims;
- the hourly buckets are not recorded: only the built-in reader produces them.

Either source writes the same daily report shape, so switching in either direction leaves
the spend series continuous. `python3 packaging/compare_ccusage.py` (read-only) checks the
two against each other on the real logs at any time.

## Keeping the reader in step with ccusage

`logs.py` (Claude) and `codex_logs.py` (Codex) are ports of ccusage 20.0.47 (commit
`d1b84d9`), rule by rule. `prices.py` ports its cost rules, and `data/prices.json` is
ccusage's own price resolution. `tests/test_parity.py` requires our daily reports to equal
ccusage's recorded output (`tests/parity/golden`) for 16 generated log sets. When ccusage
changes:

1. re-record the golden files with a built ccusage (`tests/parity/record.sh`);
2. regenerate the price table (packaging/prices/README.md);
3. port whatever the parity test then disagrees on.

`compare_ccusage.py` exiting non-zero on a real machine means the logs contain a shape the
generator does not produce. Add that shape to `tests/parity/generate.py` before fixing the
reader.

## Rules that still hold

- Never run `data reset`, `data prune` or `sync reset`. Never delete
  `~/.local/share/subbench/` or the backups.
- Never print or read tokens or credential files. Never commit real account ids or
  emails; tests use fake ones.
- An estimator change is a versioned change (CONTRIBUTING rule 3).
- Do not commit model identifiers into the repository.
