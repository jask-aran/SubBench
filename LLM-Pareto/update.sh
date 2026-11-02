#!/usr/bin/env bash
set -euo pipefail

# Update leaderboard.json from DeepSWE Datacurve's live leaderboard.
# Usage: ./update.sh
# Output: overwrites leaderboard.json with transformed data.

DEEPSWE_URL="https://deepswe.datacurve.ai/artifacts/v1.1/leaderboard-live.json"
DEEPSWE_OUT="leaderboard.json"

FRONTIERCODE_OUT="frontiercode-data.json"

curl -sfL "$DEEPSWE_URL" | python3 -c "
import json, sys

data = json.load(sys.stdin)
rows = data['rows']

# Transform to the schema expected by index.html
out = []
for r in rows:
    out.append({
        'model': r['model'],
        'effort': r['reasoning_effort'] or 'default',
        'p1': r['pass_at_1'],
        'p4': r['pass_at_4'],
        'cost': r['mean_cost_usd'],
        'dur': r['mean_duration_seconds'],
        'outTok': r['mean_output_tokens'],
        'inTok': r['mean_input_tokens'],
        'steps': r['mean_agent_steps'],
    })

# Sort by model then by effort order (same as JS)
effort_order = {'low': 0, 'medium': 1, 'high': 2, 'xhigh': 3, 'max': 4, 'default': 2}
out.sort(key=lambda r: (r['model'], effort_order.get(r['effort'], 5)))

with open('$DEEPSWE_OUT', 'w') as f:
    json.dump(out, f, indent=2)

print(f'Wrote {len(out)} configs to $DEEPSWE_OUT')
print(f'Generated at: {data.get(\"generated_at\", \"unknown\")}')
"

# Collect FrontierCode leaderboard
uv run python frontier_code.py --compact > "$FRONTIERCODE_OUT"
echo "Wrote FrontierCode leaderboard to $FRONTIERCODE_OUT"
