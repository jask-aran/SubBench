# Upgrade a machine to SubBench 0.3

A procedure for an agent on the user's machine. It moves the collector from 0.2 (ccusage
plus `claude-usage.py`) to 0.3 (the built-in reader and meters) and keeps everything the
machine has recorded:

- every quota reading, usage import and account in `~/.local/share/subbench/subbench.sqlite3`;
- every window estimate, recomputed from those readings by the same estimator and checked
  against the copy 0.2 stored;
- the machine's identity and position on subbench.jask-aran.com, so the site carries on
  from the last reading it received rather than seeing a new machine.

0.3 opens the 0.2 database in place: it adds tables (`usage_hourly`, `window_estimates`)
and changes no existing row. Work through the steps in order. At a **STOP**, stop and
report what you saw.

## Rules

- Never run `subbench data reset`, `subbench data prune` or `subbench sync reset`, and never
  delete or overwrite anything under `~/.local/share/subbench/`, backups included. The
  database is the only copy of the history.
- Never print tokens or credentials. `subbench config show` masks secrets. Do not read
  `~/.config/subbench/push.env`, `~/.claude/.credentials.json` or `~/.codex/auth.json`.
- Do not touch Cloudflare or the Worker. The site side is already done.
- Commands that write the database need access to `~/.local/share/subbench/`. If a sandbox
  makes it read-only, ask the user for that access rather than working around it.

## 1. Update the code

```bash
cd ~/SubBench                  # wherever the checkout is
git status --short             # STOP if there are changes you did not make
git pull
uv tool install --force --editable .
hash -r
subbench version               # must print "subbench 0.3.0"
"$(dirname "$(readlink -f "$(command -v subbench)")")/python" -c "import zstandard; print('zstandard ok')"
```

STOP if `subbench version` prints anything else or zstandard does not import (Codex
compresses old sessions with zstd, and 0.3 reads them through it).

## 2. Back up the database, as it is now

The old collector has kept recording since any earlier backup, so take a new one. This
uses SQLite's backup API on a read-only connection: a consistent copy even while the old
collector is writing, and nothing in the live database changes.

```bash
DB=${SUBBENCH_DATABASE:-$HOME/.local/share/subbench/subbench.sqlite3}
BAK="$DB.pre-0.3-switch.bak"
python3 - "$DB" "$BAK" <<'PY'
import sqlite3, sys
source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
target = sqlite3.connect(sys.argv[2])
source.backup(target)
print(target.execute("SELECT COUNT(*) FROM entitlement_snapshots").fetchone()[0], "readings backed up")
PY
ls -la "$DB" "$BAK"
```

STOP if the backup is missing, much smaller than the database, or the reading count is 0.

## 3. Record where the machine stands on the site

```bash
subbench config show           # SUBBENCH_PUSH_URL and SUBBENCH_PUSH_TOKEN (masked)
subbench sync status           # endpoint, agent id, cursor, last error
curl -s https://subbench.jask-aran.com/api/health
```

Note the agent id and the endpoint. The endpoint must be on `subbench.jask-aran.com`. STOP
if the push URL or token is missing, or the URL is on another host. If
`SUBBENCH_CLAUDE_USAGE_COMMAND` is listed, remove it (0.3 reads the Claude meter itself):

```bash
subbench config unset SUBBENCH_CLAUDE_USAGE_COMMAND
```

## 4. Check the new reader against ccusage on this machine's logs

Read-only: it runs ccusage and the new reader over the same logs and compares every day.

```bash
python3 packaging/compare_ccusage.py
```

Expect "every day matches" for both providers. Small cost differences (under 1%) can come
from ccusage's live price table being newer; token differences cannot. STOP on any
"tokens differ" line and report the lines.

## 5. Check the logins

```bash
subbench doctor
```

`claude login` and `codex login` should be `ok`. An expired login: open that agent (`claude`
or `codex`) once so it refreshes, then run `doctor` again. A provider the user does not use
may show an error; note it. STOP on any other error.

## 6. Stop the old collector

```bash
UNIT=~/.config/systemd/user/subbench.service
grep -E 'ExecStart|Environment' "$UNIT"      # note it; the token lives in push.env, not here
cp -p "$UNIT" "$UNIT.0.2"                    # step 9 replaces this file; keep the old one for rollback
systemctl --user stop subbench.service
pgrep -af "subbench watch" || echo "no collector running"
```

On macOS, copy the old plist aside the same way and use `launchctl bootout gui/$(id -u)
<the plist>` instead.
Kill any `subbench watch` started by hand, so nothing else writes while you switch.

## 7. One collection with 0.3

```bash
time subbench watch --once
```

The first run computes the estimate of every window in the history once and stores it, so
it takes longer than later runs (seconds to about a minute). Expect, for each provider,
`usage recorded import N` or `usage unchanged`, `N hour(s) updated`, and `entitlement N
window(s)` with N above 0; then a push line. STOP on `usage error`, `entitlement
unavailable`, `derive failed` or `push failed`, and report the line.

## 8. Check that the estimates survived

```bash
subbench data check-estimates "$BAK"
```

It compares every window that had closed when the backup was taken: the estimate 0.2
stored against the one 0.3 just computed from the same readings. Expect `0 missing` and
nearly every window unchanged. A few windows changing by a few percent is expected:
0.3 derives each window's reference only from windows that closed before it, where 0.2
also used later ones. STOP if any window is missing, or more than about 2% of windows
changed, and report the output.

## 9. Install the new collector

```bash
subbench service install
systemctl --user cat subbench.service | grep ExecStart
```

`ExecStart` must run the 0.3 `subbench` with `watch --provider all`. If `service install`
says the collector stops at logout, run `loginctl enable-linger` only if the user wants it
running while logged out.

## 10. Verify

Wait two minutes, then:

```bash
subbench service status
subbench service logs | tail -20     # usage and entitlement lines; no ccusage, no tracebacks
subbench sync status                 # the same agent id and endpoint as in step 3, no error
subbench privacy log                 # hosts: api.anthropic.com, chatgpt.com, raw.githubusercontent.com, subbench.jask-aran.com
subbench status
curl -s https://subbench.jask-aran.com/api/health    # last_ingest_at within the last few minutes
```

STOP if the agent id changed, `last_error` is set, or the health check shows no recent ingest.

## 11. Report

Tell the user: `subbench version`; the `compare_ccusage.py` summary; the `doctor` summary;
the `watch --once` time; the `check-estimates` summary line and any changed windows; the
agent id before and after; the hosts in `privacy log`; the health check; and both backup
paths. Do not delete any backup.

## Rollback

```bash
subbench service uninstall
cp -p "$BAK" "$DB"
git checkout <the commit before the pull>
uv tool install --force --editable .
cp -p ~/.config/systemd/user/subbench.service.0.2 ~/.config/systemd/user/subbench.service
systemctl --user daemon-reload && systemctl --user enable --now subbench.service
```

Restoring the backup discards whatever 0.3 recorded after step 2, so roll back only if the
user asks.
