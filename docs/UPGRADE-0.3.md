# Upgrade a machine to SubBench 0.3 (native collector)

A guide for an agent doing the upgrade on the user's machine. It replaces the old collector
(ccusage + `claude-usage.py`) with the native one, keeps every recorded observation, and
checks the result. Work through the steps in order. Stop and report at any **STOP**.

## Rules

- Never run `subbench data reset`, `subbench data prune`, `subbench sync reset` or delete
  `~/.local/share/subbench/`. The database is the only copy of the history.
- Never print tokens or credentials. `subbench config show` masks secrets; do not `cat`
  `~/.config/subbench/push.env`, `~/.claude/.credentials.json` or `~/.codex/auth.json`.
- Do not touch the Cloudflare database. The Worker is already deployed and the server-side
  migration is done.

## 1. Update the checkout

```bash
cd ~/SubBench            # or wherever the repository is
git status --short       # STOP if there are uncommitted changes you did not make
git pull
```

## 2. Back up the database

```bash
DB=${SUBBENCH_DATABASE:-$HOME/.local/share/subbench/subbench.sqlite3}
cp -p "$DB" "$DB.pre-0.3.bak"
ls -la "$DB" "$DB.pre-0.3.bak"   # STOP if the copy is missing or smaller
```

## 3. Find how the old `subbench` is installed

```bash
which -a subbench
ls -la "$(which subbench)"
systemctl --user cat subbench.service 2>/dev/null | grep -E 'ExecStart|CLAUDE_USAGE'
```

- `~/.local/bin/subbench` from **uv** (`uv tool list` shows `subbench`): use 4a.
- From **pipx** (`pipx list` shows it): use 4b.
- Otherwise: use 4c.

## 4. Install 0.3 over it

4a. `uv tool install --force --editable .`

4b. `pipx install --force --editable .`

4c. `python3 -m pip install --user -e .` (if the system Python refuses with
"externally-managed-environment", install uv and use 4a instead of overriding it).

Then:

```bash
hash -r
subbench version         # must print "subbench 0.3.0"; STOP if it errors or prints anything else
which -a subbench        # STOP if an older subbench comes first on PATH
```

## 5. Remove the old Claude helper setting

```bash
subbench config show
```

If `SUBBENCH_CLAUDE_USAGE_COMMAND` is listed:

```bash
subbench config unset SUBBENCH_CLAUDE_USAGE_COMMAND
```

Also confirm `SUBBENCH_PUSH_URL` and `SUBBENCH_PUSH_TOKEN` are listed (the token masked).
If they are missing, STOP: the old unit may have had them inline; report what
`systemctl --user cat subbench.service` shows (without the token value).

## 6. Check the logins and logs before switching

```bash
subbench doctor
```

- `claude login` and `codex login` should be `ok`. If one says expired, open that agent
  (`claude` or `codex`) once so it refreshes its login, then run `subbench doctor` again.
- `error ... no ... login` for a provider the user does not use is fine; note it.
- STOP on any other error.

## 7. One collection by hand

```bash
subbench watch --once
```

Expect, per provider: `usage recorded import N (...)` or `usage unchanged`,
`N hour(s) updated`, and `entitlement N window(s)` with N > 0. STOP if a line says
`usage error` or `entitlement unavailable`, and report the message.

## 8. Replace the service

```bash
subbench service install
systemctl --user cat subbench.service | grep -E 'ExecStart|CLAUDE_USAGE'
```

`ExecStart` must point at the 0.3 `subbench` with `watch --provider all`, and there must
be no `SUBBENCH_CLAUDE_USAGE_COMMAND` line. If `service install` says the unit stops at
logout, run `loginctl enable-linger` only if the user wants it running while logged out.

Stop any `subbench watch` started by hand (check `pgrep -af "subbench watch"`: only the
service's process should remain).

## 9. Verify

Wait two minutes, then:

```bash
subbench service status          # active (running)
subbench service logs | tail -20 # cycles with "usage" and "entitlement" lines, no ccusage, no tracebacks
subbench privacy log             # hosts: only api.anthropic.com, chatgpt.com, raw.githubusercontent.com, the push host
subbench detail hourly --hours 6 # recent hours listed
subbench sync push               # "pushed N entitlement ..." or "nothing to push"; STOP on "push failed"
subbench status                  # products and values as before
```

Then check the site: `curl -s https://subbench.jask-aran.com/api/health` shows a
`last_ingest_at` within the last few minutes after the push.

## 10. Report

Report to the user: the install method used, `subbench version`, the `doctor` summary,
the hosts in `privacy log`, whether the push succeeded, and the backup path. Keep the
backup until the user confirms the site looks right; do not delete it yourself.

## Rollback

```bash
subbench service uninstall
cp -p "$DB.pre-0.3.bak" "$DB"
git checkout <previous commit> && reinstall with the same method as step 4
```
