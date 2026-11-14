"""Write the pooling cases that subbench.products.pool_history and worker/pooling.js share.

    uv run python tests/pooling/generate.py

Each case is several collectors' history reports and a moment; the expected output is
what the Python reference gives. Values are picked, never computed, by pooling, so the two
implementations must agree exactly. Regenerate only after a deliberate change to the
reference, and commit the JavaScript change with it.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from subbench.products import pool_history  # noqa: E402

OUT = Path(__file__).resolve().parent / "golden"
NOW = datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc)
PLANS = {"claude": ["pro", "max_5x", "max_20x", None], "codex": ["plus", "pro", "chatgpt_pro", None]}


def window_row(rng, provider, account, plan, window, reset):
    tier = rng.choice(["confirmed", "confirmed", "confirmed", "likely", "provisional"])
    estimate = rng.choice([0.0, round(rng.uniform(5, 2000), 2)]) if rng.random() < 0.1 else round(rng.uniform(5, 2000), 2)
    row = {
        "provider": provider, "account_id": account, "plan": plan, "window": window,
        "reset_key": reset, "tier": tier, "reason": "",
        "estimate_usd": estimate,
        "lower_usd": None if rng.random() < 0.1 else round(estimate * rng.uniform(0.7, 1.0), 2),
        "upper_usd": None if rng.random() < 0.1 else round(estimate * rng.uniform(1.0, 1.4), 2),
        "covered_quota_percent": rng.choice([None, 0.0, round(rng.uniform(1, 100), 1), round(rng.uniform(1, 100), 1)]),
        "latest_observed_at": (NOW - timedelta(minutes=rng.randint(1, 60 * 24 * 40))).isoformat(),
    }
    return row


def case(seed: int, *, scoped: bool = False) -> dict:
    rng = random.Random(seed)
    accounts = [f"acct-{rng.getrandbits(64):016x}" for _ in range(rng.randint(1, 4))] + ([None] if rng.random() < 0.3 else [])
    owner = {a: (rng.choice(["claude", "codex"]), None) for a in accounts}
    owner = {a: (p, rng.choice(PLANS[p])) for a, (p, _) in owner.items()}
    shared = []
    for account, (provider, plan) in owner.items():
        windows = [("five_hour", 300), ("weekly", 10080)]
        if scoped and provider == "claude":
            # A scoped limit (products.product_view): its own product, a weekly figure.
            windows.append(("weekly_fable", 10080))
        for window, minutes in windows:
            for _ in range(rng.randint(0, 8)):
                reset = NOW + timedelta(minutes=rng.randint(-60 * 24 * 45, minutes))
                reset = reset.replace(second=0, microsecond=0)
                key = reset.isoformat()
                if rng.random() < 0.15:
                    key += "~via~five_hour"
                if rng.random() < 0.03:
                    key = "unknown"
                shared.append((account, provider, plan, window, key))
    reports = []
    for _agent in range(rng.randint(1, 3)):
        rows = [window_row(rng, provider, account, plan, window, key)
                for account, provider, plan, window, key in shared if rng.random() < 0.7]
        rng.shuffle(rows)
        reports.append({"windows": rows, "products": [], "generated_at": NOW.isoformat()})
    return {"now": NOW.isoformat(), "reports": reports,
            "expected": json.loads(json.dumps(pool_history(reports, now=NOW)))}


def main() -> None:
    for seed in range(1, 17):
        (OUT / f"case-{seed}.json").write_text(json.dumps(case(seed), sort_keys=True) + "\n")
    # Cases with scoped limits. Separate seeds, so the cases above stay as they were.
    for seed in range(17, 21):
        (OUT / f"case-{seed}.json").write_text(json.dumps(case(seed, scoped=True), sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
