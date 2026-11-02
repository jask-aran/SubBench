# Prices and ccusage parity

SubBench's log reader replaces ccusage, and the history recorded before the switch was
priced by ccusage, so the reader has to count and price exactly as ccusage does or every
machine's spend series would step when it upgraded.

## The price table

`src/subbench/data/prices.json` is ccusage's own price resolution, not a copy of LiteLLM:
for every Anthropic and OpenAI model name in ccusage's pricing snapshots, the rates
`PricingMap::find` returns, the long-context threshold, the Codex long-context split, and
the Fast, Flex and Ultrafast multipliers. `dump_pricing.rs` produces it.

To regenerate, from a checkout of ryoppippi/ccusage at the version being matched:

```bash
cd rust
# The LiteLLM snapshot ccusage pins (flake.lock, node "litellm"):
curl -sSfLo /tmp/litellm.json "$(python3 -c "import json;print(json.load(open('../flake.lock'))['nodes']['litellm']['locked']['url'])")"
cp /path/to/SubBench/packaging/prices/dump_pricing.rs crates/ccusage/examples/
```

`dump_pricing.rs` reads two fields ccusage keeps crate-private. Add these accessors to
`impl Pricing` in `crates/ccusage-core/src/pricing.rs` (a local change, not for ccusage):

```rust
pub fn long_context_threshold_tokens(&self) -> Option<u64> { self.long_context_threshold }
pub fn cache_create_rate_above(&self) -> Option<f64> { self.cache_create_above_200k }
```

Then list the model names and dump their resolved prices:

```bash
python3 /path/to/SubBench/packaging/prices/model_names.py /tmp/litellm.json \
  crates/ccusage-pricing-snapshots/src/models-dev-pricing.json > /tmp/models.txt
CCUSAGE_PRICING_JSON_PATH=/tmp/litellm.json cargo run --release -q -p ccusage --example dump_pricing \
  < /tmp/models.txt > /tmp/resolved.json
python3 /path/to/SubBench/packaging/prices/write_table.py /tmp/resolved.json "$(git rev-parse HEAD)"
```

A model missing from the table is priced from LiteLLM's live table, fetched at most daily
and never with `SUBBENCH_OFFLINE=1`.

## The parity tests

`tests/parity/generate.py` writes logs that exercise every rule ccusage applies (forks,
compactions, zstd rollouts, service tiers, sidechain replays, advisor iterations, fast
mode, malformed lines and more) from a seed. `tests/parity/golden/` holds ccusage's own
`daily --json --offline` output for seeds 1 to 16, and `tests/test_parity.py` requires
SubBench's report to equal it, per day and model, in tokens and cost.

To re-record against a newer ccusage, build it (`CCUSAGE_PRICING_JSON_PATH=/tmp/litellm.json
cargo build --release -p ccusage`) and run:

```bash
CCUSAGE=/path/to/ccusage/rust/target/release/ccusage tests/parity/record.sh
```

Then regenerate the price table from the same ccusage version, and port any rule the new
golden files disagree on.
