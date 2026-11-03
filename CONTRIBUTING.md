# Contributing to SubBench

Read the README first. It explains what SubBench measures and how the arithmetic works.
This file explains how to work on the code.

This project uses ASD-STE100 Simplified Technical English for all prose. That includes
this file, the README, commit messages and issues. It does not include the code.

---

## Development setup

SubBench needs Python 3.11 or later. It has one runtime dependency, `plotext`.

```bash
git clone https://github.com/jask-aran/SubBench
cd SubBench
python -m pip install -e '.[dev]'
pytest -q
```

The test suite must pass in less than 5 seconds. If it takes longer, a test does work that
it must not do.

To run the collectors on your own machine, refer to the "Installation" section of the
README. You do not need a working collector to change the estimator. The tests use
synthetic evidence.

---

## The five rules

Each rule exists because a break of it caused a real fault in this project. The code does
not show the rule, so a new contributor cannot find it without this file.

### 1. All estimator logic stays in Python

Quota-span weighting, the exclusion of unobserved usage, the reset clustering, the
staleness limit and the confidence tiers stay in `src/subbench/`. The Cloudflare Worker in
`worker/index.js` must contain none of them.

The Worker validates a batch and serves a stored report. That is all.

A second implementation of the estimator would move away from the first one silently. Two
different numbers would then appear, and nothing could show which one was correct.

### 2. The tests stay deterministic

A test must not call `codex`, `npx`, `wrangler` or the network. It must not read
`~/.local/share/subbench/` or `~/.claude/`.

Give the functions synthetic evidence, as `tests/test_timeline.py` does. Every estimator
function takes a list of mappings, so a test can build its input directly.

CI runs the suite on Python 3.11, 3.12 and 3.13. A test that needs a machine cannot pass
there.

### 3. An estimator constant must be versioned

`src/subbench/timeline.py` contains `VERSIONED_CONSTANTS`. This list holds every constant
that decides what an estimate is. `estimator_version()` hashes the list.

If you add a constant that changes a value, add it to that list. If you add a constant
that changes only what SubBench reports beside a value, add it to `UNVERSIONED_CONSTANTS`
with a comment that gives the reason.

`test_every_decision_constant_is_versioned` fails if you do neither.

Each point of the value-over-time chart carries this version. A step in a line has two
possible causes: the provider changed something, or we changed something. Without the
version, nobody can tell the two apart, and the chart will one day give a confident wrong
date for a change of a plan.

### 4. Restart the service after a code change

```bash
systemctl --user restart subbench
```

An editable installation points at the repository. It does not update a process that
already runs. The watcher keeps the old modules in memory, and the page then shows old
numbers with no warning.

### 5. Delete the asset cache before you deploy a changed page

```bash
rm -rf .wrangler
npx wrangler deploy
```

The Wrangler asset cache can report "No updated asset files to upload" and continue to
serve the old page. The deploy reports success.

---

## Where the code is

| Path | Contents |
|---|---|
| `src/subbench/watcher.py` | The file watcher and the collection loop. |
| `src/subbench/logs.py` | Reads Claude Code's logs (a port of ccusage 20's reader): the daily report, in ccusage's shape. |
| `src/subbench/codex_logs.py` | Reads Codex rollouts (a port of ccusage 20's reader): forks, compactions, zstd, tiers. |
| `src/subbench/prices.py` | ccusage's resolved price table (`data/prices.json`) and its Claude and Codex cost rules. |
| `tests/parity/` | Generated logs and ccusage's own reports for them; `test_parity.py` requires equality. See `packaging/prices/README.md`. |
| `src/subbench/compare.py` | `subbench data check-estimates`: window estimates in a backup against the live ones. |
| `src/subbench/endpoints.py` | The Claude and Codex quota meters, read with each agent's stored login. |
| `src/subbench/net.py` | The only network client: allowed hosts, credential binding, the egress record. |
| `src/subbench/privacy.py` | Account-id hashing, `subbench privacy` and the push preview. |
| `src/subbench/service.py` | `subbench service`: the systemd unit and the launchd agent. |
| `src/subbench/ccusage.py` | Normalisation of a daily usage report (ours, or ccusage's). |
| `src/subbench/entitlement.py` | The Codex and Claude quota collectors. |
| `src/subbench/store.py` | The SQLite schema, the migrations and the queries. |
| `src/subbench/regression.py` | The pairwise slopes and the robust estimate. |
| `src/subbench/crosssolve.py` | The window ratios, the account pooling and the divergences. |
| `src/subbench/timeline.py` | The settled series, the replay series and the estimator version. |
| `src/subbench/weights.py` | The non-negative least-squares fit for the per-model weights. |
| `src/subbench/push.py` | The batches, the cursor and the computed reports. |
| `worker/index.js` | The Cloudflare Worker. Validation and storage only. |
| `src/subbench/frontier.py` | `subbench frontier`: weekly value against benchmark task cost. |
| `web/` | The site: Vite, SolidJS, Conduit components v2 and Plotly. Refer to the README, section 4. |
| `scripts/dev-server.mjs` | Preview of the built site with demo, saved or live reports. |
| `LLM-Pareto/` | The benchmark scrapers and the bundle export. Refer to its README. |

`regression.py` and `crosssolve.py` hold the parts that are easy to get wrong. Read the
docstrings before you change either one. Each docstring gives the reason for the design,
and most of those reasons come from a fault that the code had.

---

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

---

## The Worker

To run the Worker on your own machine:

```bash
npx wrangler dev
```

This uses a local D1 database. It does not touch the production data.

To deploy, refer to the "Worker and the page" section of the README. You need access to the
Cloudflare account.

---

## How to send a change

1. Make a branch. Do not commit to `main`.
2. Write a test that fails before your change and passes after it.
3. Run `pytest -q`.
4. Write the commit message in Simplified Technical English. Say what the change does and
   why. A reader must not need the pull request to understand the commit.
5. Open a pull request.

---

## How to write a comment

Most comments in this project give a reason, and not a description. The code shows what it
does. A comment must show why it does that, and what breaks if somebody changes it.

This is useful:

```python
# Providers report a stable reset boundary that wanders by a few seconds between reads,
# so rounding alone still splits one window whenever the boundary straddles a minute.
```

This is not:

```python
# Round the reset time.
```

---

## Data and privacy

The local database holds daily usage reports: token counts and costs per model and day.
It holds no log text, project paths or session names.

The push sends the quota readings and the computed reports, with every account id hashed
(`subbench/privacy.py`). Every request goes through `subbench/net.py`; do not open a
connection anywhere else, and `tests/test_privacy.py` fails if you do. Run
`subbench privacy preview` to see a push in full before you change what it carries.

Never commit a real credential, an access token or an unredacted payload. If you add a
fixture from a real response, remove the account identifiers and the paths first.
