"""Compare SubBench's log reader with ccusage on this machine's own logs. Reads logs only.

    python3 packaging/compare_ccusage.py            # both providers
    python3 packaging/compare_ccusage.py codex

Runs `npx -y ccusage@latest <provider> daily --json` and SubBench's reader over the same
logs and prints every day whose tokens or cost differ. Tokens must agree exactly. Costs may
differ by a fraction of a percent when ccusage's live price table is newer than the one
SubBench ships. Exit status 1 when any day's tokens differ or a cost differs by more than
1%. Nothing is written to the SubBench database.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from subbench import logs  # noqa: E402
from subbench.prices import default_prices  # noqa: E402

CLASSES = ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens")


def days(report: dict) -> dict[str, dict]:
    out = {}
    for day in report.get("daily", []):
        out[day["date"]] = {"cost": float(day.get("totalCost", day.get("costUSD", 0.0))),
                            **{name: int(day.get(name, 0)) for name in CLASSES}}
    return out


def main() -> int:
    providers = sys.argv[1:] or ["claude", "codex"]
    if not shutil.which("npx"):
        print("npx not found: install Node.js to compare against ccusage")
        return 2
    prices = default_prices()
    failed = False
    for provider in providers:
        result = subprocess.run(["npx", "-y", "ccusage@latest", provider, "daily", "--json"],
                                capture_output=True, text=True)
        if result.returncode:
            print(f"{provider}: ccusage failed: {result.stderr.strip()[-400:]}")
            failed = True
            continue
        theirs = days(json.loads(result.stdout))
        ours = days(logs.LogReader(provider, prices).daily())
        token_days, cost_days = [], []
        for date in sorted(set(theirs) | set(ours)):
            a, b = theirs.get(date), ours.get(date)
            if a is None or b is None or any(a[n] != b[n] for n in CLASSES):
                token_days.append((date, a, b))
            elif abs(a["cost"] - b["cost"]) > 0.01 * max(abs(a["cost"]), 0.01):
                cost_days.append((date, a["cost"], b["cost"]))
        total_a = sum(d["cost"] for d in theirs.values())
        total_b = sum(d["cost"] for d in ours.values())
        print(f"{provider}: {len(theirs)} day(s) from ccusage, {len(ours)} from SubBench; "
              f"total ${total_a:,.2f} vs ${total_b:,.2f}")
        for date, a, b in token_days:
            print(f"  tokens differ {date}: ccusage {a} subbench {b}")
        for date, a, b in cost_days:
            print(f"  cost differs  {date}: ccusage ${a:.4f} subbench ${b:.4f}")
        if token_days or cost_days:
            failed = True
        else:
            print("  every day matches")
    unknown = sorted(prices.unknown)
    if unknown:
        print(f"models with no price: {', '.join(unknown)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
