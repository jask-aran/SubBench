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
| `src/subbench/ccusage.py` | The ccusage subprocess and the payload normalisation. |
| `src/subbench/entitlement.py` | The Codex and Claude quota collectors. |
| `src/subbench/store.py` | The SQLite schema, the migrations and the queries. |
| `src/subbench/regression.py` | The pairwise slopes and the robust estimate. |
| `src/subbench/crosssolve.py` | The window ratios, the account pooling and the divergences. |
| `src/subbench/timeline.py` | The settled series, the replay series and the estimator version. |
| `src/subbench/weights.py` | The non-negative least-squares fit for the per-model weights. |
| `src/subbench/push.py` | The batches, the cursor and the computed reports. |
| `worker/index.js` | The Cloudflare Worker. Validation and storage only. |
| `src/subbench/server/static/index.html` | The page. |

`regression.py` and `crosssolve.py` hold the parts that are easy to get wrong. Read the
docstrings before you change either one. Each docstring gives the reason for the design,
and most of those reasons come from a fault that the code had.

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

The local database holds the raw ccusage payloads. Those payloads contain the paths of
your projects and the names of your sessions.

The push sends the quota readings, the token counts, the costs and the computed reports.
It does not send the raw payloads.

Never commit a real credential, an access token or an unredacted payload. If you add a
fixture from a real response, remove the account identifiers and the paths first.
