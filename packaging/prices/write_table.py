"""Write src/subbench/data/prices.json from dump_pricing's output."""
import json
import sys
from pathlib import Path

resolved, revision = json.load(open(sys.argv[1])), sys.argv[2]
models = {name: {k: v for k, v in rate.items() if v is not None} for name, rate in sorted(resolved.items())}
target = Path(__file__).resolve().parents[2] / "src" / "subbench" / "data" / "prices.json"
target.write_text(json.dumps({"source": f"ccusage {revision[:12]} PricingMap::load_embedded()", "models": models},
                             sort_keys=True, separators=(",", ":")))
print(f"{len(models)} models -> {target}")
