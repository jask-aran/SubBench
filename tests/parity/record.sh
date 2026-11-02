#!/bin/bash
# Record ccusage's daily reports for the generated logs, as the parity tests' expected values.
#   CCUSAGE=/path/to/ccusage tests/parity/record.sh
# Built from ryoppippi/ccusage (rust/): CCUSAGE_PRICING_JSON_PATH=<pinned litellm json> cargo build --release -p ccusage
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
ccusage=${CCUSAGE:?set CCUSAGE to a ccusage 20 binary}
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
for seed in $(seq 1 16); do
  rm -rf "$work/home"
  python3 "$here/generate.py" "$seed" "$work/home"
  for provider in claude codex; do
    HOME="$work/home" CODEX_HOME="$work/home/.codex" XDG_CONFIG_HOME="$work/home/.config" TZ=UTC \
      "$ccusage" "$provider" daily --json --offline --timezone UTC > "$work/$provider.json"
  done
  python3 - "$work" "$here/golden/seed-$seed.json" "$("$ccusage" --version)" <<'PY'
import json, sys
work, out, version = sys.argv[1:4]
json.dump({"ccusage": version, "claude": json.load(open(f"{work}/claude.json")),
           "codex": json.load(open(f"{work}/codex.json"))}, open(out, "w"), sort_keys=True, separators=(",", ":"))
PY
done
