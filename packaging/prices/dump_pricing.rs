use ccusage_core::pricing::{self, PricingMap};
use std::io::Read;

fn main() {
    pricing::set_embedded_snapshots(pricing::EmbeddedSnapshots {
        litellm_pricing: ccusage_pricing_snapshots::LITELLM_PRICING,
        models_dev_pricing: ccusage_pricing_snapshots::MODELS_DEV_PRICING,
        models_dev_catalog_rules: ccusage_pricing_snapshots::MODELS_DEV_CATALOG_RULES,
    });
    let map = PricingMap::load_embedded();
    let mut input = String::new();
    std::io::stdin().read_to_string(&mut input).unwrap();
    let mut out = serde_json::Map::new();
    for model in input.lines().map(str::trim).filter(|l| !l.is_empty()) {
        let Some(p) = map.find(model) else { continue };
        out.insert(model.to_string(), serde_json::json!({
            "input": p.input, "output": p.output,
            "cache_create": p.cache_creation_input_token_cost(),
            "cache_read": p.cache_read, "cache_read_explicit": p.cache_read_explicit,
            "input_above": p.input_above_200k, "output_above": p.output_above_200k,
            "cache_create_above": p.cache_create_rate_above(),
            "cache_read_above": p.cache_read_above_200k,
            "threshold": p.long_context_threshold_tokens(),
            "split_threshold": pricing::long_context_split_threshold(model),
            "fast": p.fast_multiplier, "flex": p.flex_multiplier, "ultrafast": p.ultrafast_multiplier,
        }));
    }
    println!("{}", serde_json::to_string(&out).unwrap());
}
