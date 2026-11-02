# Rollout evidence: findings and a proposed change of source

Status: analysis complete, no code written. This document records what one session
measured on 2026-09-05 and 2026-09-06, and the design that follows from it. Another
agent can continue from here.

The documentation uses ASD-STE100 Simplified Technical English.

---

## 1. Summary

Three results matter.

1. **ccusage adds no evidence.** For Codex, ccusage is a re-aggregation of the same
   rollout files that SubBench already watches. This session rebuilt ccusage's daily
   per-model output from the rollouts to between 0.02% and 0.4%.
2. **The rollouts also hold the quota readings**, and they hold 20 times more of them
   than SubBench's own database, with six more months of history.
3. **Two dimensions that change the result are invisible to the current pipeline**:
   reasoning effort and service tier. Fast mode (`service_tier: priority`) is not
   priced by ccusage or by SubBench.

The result of 1 and 2 is that SubBench can become a parser over the rollouts. The
result of 3 is that it should.

---

## 2. What the meter measures

### 2.1 The observable

The only quantity the provider reports is `used_percent`. Codex writes it into every
`token_count` event, inside `rate_limits.primary` and `rate_limits.secondary`. Every
credit or dollar figure in this document is a reconstruction: logged tokens multiplied
by a price table. The reconstruction is a hypothesis about the meter, not a reading of
it.

### 2.2 The price anchor

The Codex pricing page publishes credit prices per 1M tokens. One credit is $0.05.
GPT-5.6 Terra is 50 credits input and 300 credits output. Multiplied by $0.05 this is
$2.50 and $15.00, which is the API list price exactly. Sol matches on input and is
close on output. Luna disagrees by a factor of 4 and is the least certain.

### 2.3 The meter is not a token meter

Measured over single-model, single-session 5-hour windows:

| Model / effort / tier | credits per 1% | tokens per 1% |
|---|---|---|
| Astra low, default | 2.28 | 64,371 |
| Terra high, default | 2.58 | 311,102 |
| Sol high, default | 2.60 | 179,967 |
| Sol low, default | 2.75 | 215,944 |
| Sol medium, default | 3.45 | 269,062 |
| Luna max, priority | 0.92 | 1,148,660 |

Tokens per 1% change by a factor of 19. The meter does not count tokens.

Credits per 1% stay in a band of 2.28 to 2.75 for four configurations whose prices
differ by 5 times. For those, the meter is dollar-denominated to within 10%. This is
evidence and not a circular result: the price table was not fitted to the data.

### 2.4 The one exception, and its likely cause

Luna sits at 0.92 credits per 1%, which is three times below the band.

Luna is also the only model that was run on the priority tier. Of 633 priority calls in
the logs, 624 are Luna at max effort. If the meter charges a premium for priority
processing, and ccusage prices priority at the standard rate, then the Luna credit
figure is too low by that premium. A premium near 3 times would move Luna into the
band with the other four.

This is a hypothesis. Section 8 gives the experiment that tests it.

### 2.5 The published message table agrees

The pricing page gives message ranges per model. Against Astra, the low ends are Sol
2.0, Terra 5.0 and Luna 50.0. The input price ratios are 2.5, 5.0 and 50.0. Terra and
Luna are price-proportional to two significant figures. A message meter would not track
price this closely. The table is the same dollar budget, shown per model at an assumed
message size.

---

## 3. Practical rates

### 3.1 Burn rate

Quota consumed for each minute of continuous work. Single-session windows only.

| Config | %/min | Flat-out minutes to 100% | Observed minutes |
|---|---|---|---|
| Astra low, default | 1.552 | 64 | 102 |
| Terra high, default | 1.147 | 87 | 85 |
| Sol low, default | 1.082 | 92 | 734 |
| Sol high, default | 0.978 | 102 | 351 |
| Sol medium, default | 0.909 | 110 | 145 |
| Luna max, priority | 0.321 | 311 | 143 |
| Luna max, default | 0.145 | 691 | 118 |

Luna needs more than 300 minutes of continuous work to empty a 300-minute window. You
cannot exhaust it. This agrees with the operator's experience.

### 3.2 Turns

| Config | Median turn | % per turn | Turns per window |
|---|---|---|---|
| Luna max, default | 2.5 min | 0.5 | 200 |
| Luna max, priority | 3.1 min | 1.5 | 67 |
| Sol low | 2.1 min | 3.0 | 33 |
| Sol high | 3.1 min | 3.0 | 33 |
| Terra high | 1.2 min | 3.5 | 29 |
| Astra low | 1.3 min | 5.0 | 20 |
| Sol medium | 2.5 min | 5.0 | 20 |

`used_percent` is an integer. Per-turn deltas are therefore quantised, and short turns
show zero. Use the aggregate rates in section 3.1 as the reliable layer. Treat the
per-turn column as indicative.

### 3.3 Value of one exhausted window

| Dominant config | Credits per 100% | Dollars at $0.05 |
|---|---|---|
| Sol medium | 366 | $18.31 |
| Sol high | 355 | $17.73 |
| Sol low | 335 | $16.77 |
| Astra low | 283 | $14.14 |
| Terra high | 259 | $12.97 |

SubBench reports $14.24 for the median ChatGPT Plus 5-hour window. The independent
reconstruction agrees.

### 3.4 Effort

Reasoning tokens for each call: Astra low 52, Sol low 83, Sol medium 111, Terra high
200, Sol high 252, Luna max 377. Effort triples reasoning between low and high on Sol.

Cost for each call moves much less, because reasoning is 8% to 12% of a call whose
other 90% is a context of 130k tokens. Benchmarks see a 10 times change because a
benchmark prompt is short and reasoning is most of its bill. In an agent loop, context
hides the effect. The logs contain no xhigh window with enough quota movement to
measure, so the high-effort regime is untested.

---

## 4. Evidence sources compared

### 4.1 ccusage is derived from the rollouts

ccusage's Codex JSON has this schema and nothing else:

```
/daily[]/date, costUSD, inputTokens, cacheReadTokens, cacheCreationTokens,
         outputTokens, reasoningOutputTokens, totalTokens
/daily[]/models/<model>/{same fields, plus isFallback}
/totals/{same fields}
```

Model and day. No effort, no tier, no window, no session.

Rebuilding the same totals from `last_token_usage` in the rollouts:

| Day and model | From rollouts (fresh in / cached in / out / reasoning) | ccusage |
|---|---|---|
| 09-04 luna | 197,113 / 1,814,784 / 9,451 / 6,696 | identical |
| 09-04 sol | 1,652,626 / 50,161,920 / 219,129 / 123,640 | 1,652,251 / 50,070,912 / 219,092 / 123,640 |
| 09-05 astra | 918,802 / 19,390,848 / 51,349 / 12,609 | 914,770 / 19,049,088 / 50,590 / 12,084 |

The difference is 0.02% to 0.4%, from edge events and the timezone boundary.

ccusage adds two things of its own:

1. A price table, which gives `costUSD`.
2. Model-name resolution. It reports the auto-review turns as `gpt-5.5`, where
   `turn_context` says only `codex-auto-review`.

### 4.2 The rollouts hold more quota history than SubBench

| Source | 5-hour readings | Distinct windows | Earliest |
|---|---|---|---|
| Rollout JSONL, `primary` only | 26,583 | 251 | February 2026 |
| `entitlement_snapshots` | 1,280 | 76 | 2026-08-25 |

The rollout count is a lower bound. In early August, `primary` held the weekly window
and the 5-hour figure was in `secondary`. A parser that reads both fields will find
more.

All 6,260 Codex entitlement rows in the database come from `codex-app-server`.

### 4.3 What the poll adds

One thing only: a reading when no session runs. A rollout writes `rate_limits` as a
by-product of a turn, so the file record stops when work stops.

For a window worked to exhaustion this changes nothing, because quota does not move
while idle, so the last in-session reading is already final. It matters for a window
abandoned early, which is most of them, and for the `measured %` coverage figure.

---

## 5. Fast mode is invisible to the current pipeline

Codex writes `service_tier` into `thread_settings_applied`. Across all sessions there
are 4,356 `default` calls and 633 `priority` calls.

The priority calls are 624 Luna max, 8 Terra high and 1 Sol high. Tier and model are
therefore almost fully confounded in the present data. No conclusion can separate
"Luna is metered cheaply" from "priority is metered differently".

Neither tool models the tier. SubBench has no `service_tier` concept; the only "tier"
in its source is the confidence tier in `cli.py`. ccusage reads the same files but its
schema drops the field. The tokens are counted. The price premium is not.

---

## 6. Is the approach viable for Claude Code?

Partly. The token side is better than Codex. The quota side is absent.

### 6.1 What Claude Code rollouts contain

Each `assistant` record carries a full usage object:

```json
{"model":"claude-opus-5","effort":"medium","apiBlockIndex":0,
 "usage":{"input_tokens":2,"cache_creation_input_tokens":2186,
          "cache_read_input_tokens":138284,"output_tokens":374,
          "output_tokens_details":{"thinking_tokens":147},
          "cache_creation":{"ephemeral_1h_input_tokens":2186,
                            "ephemeral_5m_input_tokens":0},
          "service_tier":"standard","speed":"standard",
          "server_tool_use":{"web_search_requests":0,"web_fetch_requests":0}}}
```

This is richer than the Codex equivalent. It splits cache creation by time to live,
which prices differently. It gives thinking tokens for each message. It carries
`effort`, `service_tier` and `speed`. The `speed` field is where fast mode will appear,
so the same parser design applies to both providers.

Present local history: 4 projects, 23 JSONL files, 5,299 assistant records. All are
`claude-opus-5`, `speed: standard`, `service_tier: standard`, with 4,798 medium effort
and 501 high.

### 6.2 What they do not contain

No quota field of any kind. A search for rate, limit, quota, percent, reset and overage
returns nothing usable.

Claude entitlement must keep coming from the OAuth usage endpoint. SubBench calls it
today through `SUBBENCH_CLAUDE_USAGE_COMMAND` in `entitlement.py`, which runs an
external command and parses `five_hour` and `seven_day` utilisation.

### 6.3 Consequence for the design

The two providers are asymmetric.

- Codex: rollouts give tokens **and** quota. The poll adds idle observation only.
- Claude: rollouts give tokens. Quota comes only from the endpoint, so the poll is
  mandatory, not an improvement.

`apiBlockIndex` was investigated and is not a window key. See section 6.4.

### 6.4 apiBlockIndex is a content-block index, and a trap

`apiBlockIndex` does not segment the transcript by rate-limit block. It is the index of
the content block inside one API response. Values observed are 0 to 3.

The proof is that the index oscillates between 0 and 1 within the same second, in the
same session, and that records which share a `requestId` and a `message.id` carry
**identical** usage objects with different indices:

```
req_011Cejw26Hsbtchhgow5NLgg  (0, out=326, ['thinking'])
                              (1, out=326, ['tool_use'])
                              (2, out=326, ['tool_use'])
```

Claude Code writes one `assistant` record for each content block, and repeats the whole
usage object on each one. In the local history there are 5,485 assistant records but
only 2,815 real API calls. The distribution is 1,048 requests with one record, 973 with
two, 699 with three, 90 with four, 4 with six and 1 with ten.

**A parser that sums usage over assistant records over-counts by about 1.95 times.**
Deduplicate on the pair `(requestId, message.id)` before summing.

ccusage does this correctly. Its Claude daily totals match the deduplicated
reconstruction exactly:

| Day | Deduplicated (in / cache read / cache write / out) | ccusage |
|---|---|---|
| 09-02 | 1,098 / 149,337,463 / 1,755,700 / 468,591 | identical |
| 09-03 | 646 / 71,637,710 / 742,739 / 280,760 | identical |
| 09-04 | 496 / 33,766,974 / 348,405 / 152,429 | identical |
| 09-05 | 248 / 27,335,677 / 255,151 / 145,378 | identical |

Exact agreement on four consecutive days. The existing Claude figures in SubBench are
therefore sound, and the naive-summation hazard is real but has not affected them.

There is no window join key anywhere in the Claude Code logs. The poller stays
authoritative for Claude entitlement.

---

## 7. Proposed architecture

SubBench takes ownership of parsing and pricing. ccusage is kept only as a price
reference and as an independent check.

1. **Rollout parser, the primary evidence layer.** Read the Codex and Claude JSONL
   directly. Write one row for each model call, with timestamp, session, model, effort,
   service tier, every token class, and, for Codex, the `rate_limits` block attached to
   that call. This replaces ccusage as the source of usage evidence.
2. **Entitlement poller, owned in house.** Call the provider endpoints directly, in the
   way `codex-auth` does, rather than driving `codex app-server` as a subprocess. Its
   purpose narrows to detecting window closes and resets live, and to sampling while
   idle.
3. **Price table with history.** See section 8.1.
4. **Reporting.** The rates in section 3 become a first-class report: for a chosen
   lookback, give the burn rate, the flat-out minutes, the duty cycle, the tokens for
   each 1% and the dollars for each window, split by model, effort and tier.

The operator's judgement, recorded here: for nearly the whole history only one session
ran at a time, so double counting is rare. Where sessions did overlap, their token
counts overlap in the same way as their quota readings, so the error is self-consistent
rather than additive. The measured concurrency supports this. Of 77 windows, 62 had one
session, 13 had two and 2 had three.

The parser should still record the session file for each row, so that a window with
concurrent sessions can be identified and excluded from rate estimates. Every rate in
section 3 was computed this way. The first version of the burn-rate table in this
session was wrong by up to a factor of 14 because it did not do this.

---

### 7.1 One dense poller for both providers

The poller serves a different purpose for each provider, but the same implementation
covers both.

- **Claude: authoritative.** The rollouts hold no quota field, so every entitlement
  reading comes from the endpoint.
- **Codex: a reference point.** The rollouts already carry `rate_limits` on every
  `token_count` event. The poller adds readings while no session runs, which closes the
  tail of a window that was abandoned early, confirms the reset boundary and detects
  usage from another machine on the same account.

**Resolution floor.** Both providers report `used_percent` as an integer. Of 6,260
Codex rows and 2,238 Claude rows in the database, not one holds a fractional value. A
poll faster than the time to move 1% therefore returns a duplicate.

| Provider | Fastest observed rise | Time to move 1% |
|---|---|---|
| Codex, Astra low | 1.55 %/min | 39 s |
| Codex, Sol low | 1.08 %/min | 56 s |
| Claude, mean while rising | 1.4 %/min | 43 s |
| Codex, Luna max default | 0.145 %/min | 6.9 min |

A poll interval of 20 to 30 seconds is the useful floor. Below that the extra readings
carry no information. Present median spacing is 292 s for Claude and 908 s for Codex,
so a dense poll is 10 to 30 times denser than today.

One Claude interval showed a rise of 91 %/min between consecutive polls. This is either
a reset artefact or a parallel burst from subagents. It needs investigation before the
poller's output is trusted for rate work.

**Cost of density.** Owning the endpoint call removes the reason to keep the interval
long. Today the Codex reading needs a `codex app-server` subprocess for each poll,
which is why it runs at 908 s. A direct HTTPS call, in the way `codex-auth` does it, is
cheap enough to run every 30 s.

---

## 8. Open problems

### 8.1 Historical prices

This is the hardest problem and it is not solved by any current design.

SubBench keeps raw payloads so that a better estimator applies to all history. A moving
price table breaks that promise. If Luna's price falls by 80%, re-pricing old evidence
at today's table understates what the entitlement delivered at the time.

The fix is to store the price table, dated, on every collection, and to price each
rollout event with the table that was current when the event happened. ccusage remains
useful as the upstream that maintains the table, so SubBench does not have to. What
SubBench must own is the dated snapshot.

Note also that credits and API list prices are not the same series. Terra agrees
exactly, Sol agrees on input, Luna disagrees by 4 times. Any price history must record
which basis it used.

### 8.2 The Luna anomaly

Unresolved. Either priority processing carries a meter premium, or Luna carries a
weight heavier than its price. See section 8.4.

### 8.3 Quantisation

`used_percent` is an integer. Per-call and per-turn attribution is therefore coarse.
Any estimator built on per-turn deltas must aggregate before it divides.

### 8.4 The experiment that settles the meter

Run Luna at max effort on the **default** tier, in one session with nothing else
running, long enough to move more than 25% of a 5-hour window.

- If Luna default gives about 2.6 credits for each 1%, the meter is dollar-denominated
  throughout, and priority is a meter premium that SubBench must model.
- If Luna default also gives about 0.9, then Luna carries a heavier weight than its
  price, and the meter is dollar-shaped with a correction for each model.

A second experiment: run Sol or Terra at xhigh long enough to move 25% of a window, and
test whether high effort breaks the 10% dollar band.

---

## 9. Corrections made during this session

Recorded so they are not repeated.

1. **Burn rates were contaminated by concurrency.** The first pass walked one file at a
   time but credited all quota movement in the window to that file. Restricting to
   single-session windows changed Luna max priority from 4.69 %/min to 0.321 %/min.
2. **The August windows were weekly, not 5-hour.** In early August `primary` held the
   weekly window. Windows that appeared to consume 1,400 to 2,250 credits were weekly
   windows read as 5-hour ones.
3. **The direction of the Sol deviation was reversed.** More credits for each 1% means
   more value extracted, so Sol is the best value in our data, not the worst. The
   published table predicts the opposite, and that disagreement is unexplained.
4. **Effort was described as costing 5% more.** That figure was cost for each call. It
   is correct, but it hid a tripling of reasoning tokens, which matters in other
   regimes.

---

## 10. Reference figures

SubBench headline at the time of writing, 8,462 quota readings:

```
ChatGPT Plus   weekly $152   ($107-$160, 4 windows, 2 accounts)
               5-hour $14.24 ($13.79-$15.50, 16 windows, 2 accounts)
Claude Pro     weekly $530   ($469-$592, 1 window, 1 account)
               5-hour $64.27 ($52.66-$67.16, 4 windows, 1 account)
```

Both plans cost $20 a month. Claude Pro delivers 4.5 times the API-equivalent value on
the 5-hour window and 3.5 times on the weekly window. Because the meter is
dollar-denominated, this figure measures the entitlement and not the model mix. The
Claude side of that claim is unverified: all Claude usage in the data is Opus 5, so
there is no contrast to test whether Anthropic's meter is also dollar-neutral.

### Fields the parser needs

Codex, in `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`:

| Record | Field | Use |
|---|---|---|
| `turn_context` | `model`, `effort`, `turn_id`, `cwd` | Configuration for each turn |
| `event_msg` / `thread_settings_applied` | `thread_settings.service_tier` | Fast mode |
| `event_msg` / `task_started`, `task_complete` | `turn_id`, timestamp | Turn boundaries and duration |
| `event_msg` / `token_count` | `info.last_token_usage` | Tokens for each call |
| `event_msg` / `token_count` | `rate_limits.primary`, `.secondary` | Quota, window length, reset, plan |

Claude Code, in `~/.claude/projects/<project>/<session>.jsonl`:

| Record | Field | Use |
|---|---|---|
| `assistant` | `message.model`, `effort`, `timestamp`, `requestId` | Configuration for each call |
| `assistant` | `message.usage` | Tokens, cache time to live, thinking tokens |
| `assistant` | `message.usage.service_tier`, `.speed` | Tier and fast mode |
| `assistant` | `requestId` + `message.id` | Deduplication key. Mandatory, see section 6.4 |
| `assistant` | `apiBlockIndex` | Content-block index. Do not use as a window key |

Quota for Claude comes only from the OAuth usage endpoint.
