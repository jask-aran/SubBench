# LLM-Pareto

Tools for comparing model capability, task cost, token use, response time, and coding-agent configurations.

## Web explorer

[`index.html`](./index.html) is the unified static explorer. Model mode combines
Artificial Analysis indexes and individual benchmarks with DeepSWE Pass@1/Pass@4.
Coding-agent mode keeps harness–model configurations as their own entity type.
Both modes share filtering, family colours, resource axes, the Pareto frontier,
the efficiency solver, and quick comparison.

### Data refresh

| Dataset | Script | Auto-update |
|---------|--------|-------------|
| `leaderboard.json` | `./update.sh` | GitHub Actions (daily) |
| `data/` | `uv run archive.py collect` | Manual |
| `frontier-data.json` | `uv run export-web-data.py` | Manual after collection |

The app only fetches `frontier-data.json`; it makes no runtime request to either
source. AA benchmark observations retain only benchmark-specific resource data.
Where no such cost or time is published, the UI switches to a capability ranking
instead of borrowing a composite index value.

## Running the CLI with uv

`artificial_analysis_v2.py` uses only the Python standard library. There are no packages to install.

Run it directly through `uv`:

```bash
uv run artificial_analysis_v2.py --help
```

Optionally select a Python version:

```bash
uv run --python 3.12 artificial_analysis_v2.py --help
```

The pre-v2 commands remain under `legacy/`:

```bash
uv run legacy/artificial_analysis_single_model.py gpt-5-6-sol
uv run legacy/artificial_analysis_models.py gpt-5-6-sol,gpt-5-6-terra-low
```

## CLI modes

The command has three mutually exclusive modes:

1. model details from one or more positional slugs;
2. the model catalogue through `--list-models`;
3. Coding Agent Index configurations through `--coding-agents` or `--coding-agent`.

Every successful response contains `schema_version`, `status`, and a UTC `collected_at` system timestamp. Each detailed model or coding-agent record uses the same timestamp as its containing response.

### Model details

One model returns `data.model`:

```bash
uv run artificial_analysis_v2.py gpt-5-6-sol
```

Multiple comma-separated or space-separated slugs return `data.models` in requested order:

```bash
uv run artificial_analysis_v2.py \
  gpt-5-6-terra-low,gpt-5-6-sol-high gpt-5-6-sol-xhigh
```

Duplicate slugs are removed. The request fails rather than returning a partial collection when a required record is unavailable for any requested model.

#### Default model output

The default is a compact comparison record close to the original cost-per-task output:

- `collected_at`, `slug`, `name`, `short_name`
- `intelligence_index`
- `cost_per_task` and `cost_per_task_breakdown`
- input, answer-output and reasoning-output tokens per task
- `time_per_task`
- `output_speed_tokens_per_second`
- `end_to_end_response_time`
- `time_to_first_answer_token`
- `coding_index`
- `coding_cost_per_task` and `coding_cost_per_task_breakdown`
- `coding_time_per_task`
- `model_metadata.release_date`

The response metadata also includes requested and returned counts, source URLs, the evaluation reference model, the active evaluation field set, and the selected output fields.

#### Verbose model output

`--verbose` returns every normalized field plus the canonical source records:

```bash
uv run artificial_analysis_v2.py gpt-5-6-sol --verbose
```

This adds:

- complete Intelligence and Coding Index cost totals and breakdowns;
- evaluation scores and each evaluation's weighted cost contribution;
- canonical token totals and answer/reasoning token splits;
- output-speed and first-token percentile distributions;
- first-answer and end-to-end timing components;
- performance by prompt type and available time-series data;
- nested benchmark details and result-status fields;
- release, lineage, modality, context, licensing and pricing metadata;
- `source_record.general` and `source_record.coding`.

### Model catalogue

The catalogue discovers dated candidates without requesting each model separately:

```bash
uv run artificial_analysis_v2.py --list-models
```

#### Default catalogue output

The default catalogue is deliberately minimal:

- `slug`
- `short_name`
- `release_date`

It excludes deprecated models by default and sorts newest releases first.

Filter by data availability:

```bash
uv run artificial_analysis_v2.py --list-models --eligibility full
```

Eligibility modes are:

- `all`: every canonical dated model allowed by the deprecation filter;
- `active`: non-deprecated models, the default;
- `general`: models with complete general cost data;
- `coding`: models with complete Coding Index cost data;
- `full`: models with complete general and Coding Index cost data.

Filter by release date or include historical models:

```bash
uv run artificial_analysis_v2.py --list-models --since 2026-07-01
uv run artificial_analysis_v2.py --list-models --include-deprecated
```

#### Verbose catalogue output

```bash
uv run artificial_analysis_v2.py --list-models --verbose
```

Verbose catalogue entries add full identity, creator, reasoning/open-weight/deprecation status, general and coding availability flags, and the canonical general and coding records.

Typed canonical-record rules keep chart-specific representations out of the catalogue. Conflicting equally complete canonical records fail instead of being selected silently.

### Coding Agent Index

Coding-agent results are harness-model configurations rather than ordinary model slugs. Their stable primary identity is the configuration `id`; harness and host model are separate dimensions.

Return all configurations:

```bash
uv run artificial_analysis_v2.py --coding-agents
```

`--list-coding-agents` is an alias for the same command.

Select one or more configurations by stable ID or exact display label:

```bash
uv run artificial_analysis_v2.py \
  --coding-agent 6eb6667a6c986c2afc40c779a1666e5a
```

Repeat `--coding-agent` to select several configurations.

#### Default coding-agent output

- `id`, `display_label`, `agent_name`, `host_model_slug`
- aggregate Coding Agent Index score
- DeepSWE, Terminal-Bench v2 and SWE-Atlas-QnA component scores and token means
- cost per task
- active agent wall time per task
- output and total tokens per task

#### Verbose coding-agent output

```bash
uv run artificial_analysis_v2.py --coding-agents --verbose
```

Verbose records additionally contain provider and display metadata, variant relationship, component/evaluation counts, steps, input/cache/output token fields, cache-hit rate, total evaluation cost, cost/token percentiles, harness versions per benchmark, and the canonical source record.

## Selecting output fields

`--fields` accepts comma-separated dotted paths, repeated flags, named groups, or a mixture:

```bash
uv run artificial_analysis_v2.py gpt-5-6-sol \
  --fields slug,name,intelligence_index,cost_per_task,coding_index
```

```bash
uv run artificial_analysis_v2.py gpt-5-6-sol \
  --fields identity,cost,timing,evaluations
```

```bash
uv run artificial_analysis_v2.py --coding-agents \
  --fields id,display_label,index_score,percentiles
```

Named model groups are:

- `identity`
- `summary`
- `cost`
- `tokens`
- `timing`
- `evaluations`
- `coding`
- `metadata`
- `performance`
- `source`

Unknown paths fail explicitly. `--fields` and `--verbose` are mutually exclusive.

Entity identity is never removed by a projection. Model details and catalogue
entries always include `slug`. Coding-agent configurations always include `id`
and `host_model_slug`, even when they are not named in `--fields`. Response-level
timestamps, counts, sources and collection metadata also remain present.

## Flags

| Flag | Use |
|---|---|
| positional `slugs` | Select one or more model slugs. |
| `--list-models` | Return the dated model catalogue. |
| `--eligibility all\|active\|general\|coding\|full` | Filter catalogue candidates by available data. |
| `--include-deprecated` | Allow deprecated models in catalogue results. |
| `--since YYYY-MM-DD` | Include catalogue entries released on or after the date. |
| `--coding-agents`, `--list-coding-agents` | Return all Coding Agent Index configurations. |
| `--coding-agent VALUE` | Select a configuration by ID or exact display label; repeatable. |
| `--evaluation-reference-model SLUG` | Change the model whose non-null evaluations define the detailed-model evaluation schema. Defaults to `gpt-5-6-sol`. |
| `--fields PATHS` | Return explicit dotted fields or named groups; repeatable. |
| `--verbose` | Return all normalized and canonical fields. |
| `--compact` | Emit JSON without indentation. |
| `--timeout SECONDS` | Set the HTTP timeout; defaults to 30 seconds. |
| `-h`, `--help` | Show CLI help. |

## Local archive

`archive.py` creates a coherent frontier snapshot using verbose records. A bare
collection gets the complete catalogue, deep details for every active model with
both general and coding data, and every coding-agent configuration. Models are
batched to keep request URLs bounded; deprecated catalogue entries remain locally
queryable but are not deeply collected unless requested.

```bash
uv run archive.py collect
```

`--since` limits deep model records by source `release_date` without truncating
the saved catalogue. `--models` overrides automatic deep-model selection.

```bash
uv run archive.py collect --since 2026-01-01
uv run archive.py collect --models gpt-5-6-sol,gpt-5-6-terra-low
```

Optional exclusions support narrower collections:

```bash
uv run archive.py collect --no-model-details
uv run archive.py collect --no-coding-agents
uv run archive.py collect --no-catalogue --models gpt-5-6-sol
```

Each job is stored as an individual JSON record and indexed in SQLite. A final
manifest records its catalogue, model batches, coding-agent run, selected and
skipped slugs, filters, timing and completion status. Queries use only the latest
completed manifest and never access the network.

```bash
uv run archive.py query gpt-5-6-sol
uv run archive.py query --list-models
uv run archive.py query --list-models --eligibility full --since 2026-01-01
uv run archive.py query --coding-agents
uv run archive.py query --coding-agent CONFIGURATION_ID
```

Cached queries retain the main CLI's default fields, mandatory identities,
`--fields`, `--verbose`, `--compact`, model slug parsing and coding-agent
selectors. Query an earlier completed snapshot with `--at UTC_TIMESTAMP`:

```bash
uv run archive.py query gpt-5-6-sol --fields intelligence_index,cost_per_task
uv run archive.py query --list-models --verbose
uv run archive.py query --coding-agents --at 2026-07-22T12:00:00Z
```

The default data directory is `data/`. Put `--data-dir PATH` before the subcommand
to use persistent storage elsewhere. Generated data and reset backups are ignored
by Git.

### Recovery

JSON is written and flushed before its SQLite transaction, making JSON the
recovery source of truth. `verify` checks JSON hashes, SQLite integrity and exact
run/snapshot parity. `rebuild` atomically reconstructs SQLite only after every JSON
record validates.

```bash
uv run archive.py verify
uv run archive.py rebuild
uv run archive.py rebuild --replace
```

Cold collection creates the directories and schema automatically. Reset requires
confirmation and moves the data directory to a timestamped backup instead of
deleting it:

```bash
uv run archive.py reset --yes
```

## Preview the web app

```bash
cd ~/LLM-Pareto
python3 -m http.server 8899 --bind 0.0.0.0
```

Open http://127.0.0.1:8899/ (or http://localhost:8899/).
Do not use `file://`; the browser must fetch `frontier-data.json` over HTTP.

## DeepSWE leaderboard

`leaderboard.json` is a local snapshot of DeepSWE Datacurve. Refresh it with:

```bash
./update.sh
```

The daily workflow refreshes this snapshot at 06:00 UTC. Run
`uv run export-web-data.py` afterwards to fold it into the browser bundle.
