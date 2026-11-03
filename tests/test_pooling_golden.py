"""The server's pooling reference reproduces the shared cases (tests/pooling/golden).

worker/pooling.js must reproduce the same files (tests/test_pooling.mjs). If this test
fails, the Python reference changed: regenerate the cases on purpose and change the
JavaScript with them, or the server and the collector pool differently.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from subbench.products import merge_agent_windows, pool_history, pool_input

CASES = sorted((Path(__file__).parent / "pooling" / "golden").glob("case-*.json"))


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_reference_reproduces_the_shared_case(path):
    case = json.loads(path.read_text())
    now = datetime.fromisoformat(case["now"])
    got = json.loads(json.dumps(pool_history(case["reports"], now=now)))
    assert got == case["expected"]
    # The server pools the compact copies it stores, and must get the same answer.
    assert json.loads(json.dumps(pool_history([pool_input(r) for r in case["reports"]], now=now))) == case["expected"]


def test_the_pooled_report_carries_no_account_and_stays_small():
    report = {"windows": [{"provider": "codex", "account_id": f"acct-{i:016x}", "plan": "plus", "window": "weekly",
                           "reset_key": f"2026-10-{1 + i % 28:02d}T00:00:00+00:00", "tier": "confirmed",
                           "estimate_usd": 90.0 + i, "covered_quota_percent": 50.0,
                           "latest_observed_at": "2026-10-01T00:00:00+00:00"} for i in range(500)]}
    pooled = pool_history([report, report], now=datetime.fromisoformat("2026-11-01T00:00:00+00:00"))
    text = json.dumps(pooled)
    assert "acct-" not in text and len(pooled["windows"]) == 1


def test_one_account_on_two_machines_counts_once():
    window = {"provider": "claude", "account_id": "acct-1", "plan": "pro", "window": "weekly",
              "reset_key": "2026-10-01T00:00:00+00:00", "tier": "confirmed", "estimate_usd": 300.0,
              "covered_quota_percent": 40.0, "latest_observed_at": "2026-09-30T00:00:00+00:00"}
    fuller = {**window, "estimate_usd": 320.0, "covered_quota_percent": 70.0}
    merged = merge_agent_windows([{"windows": [window]}, {"windows": [fuller]}])
    assert merged == [fuller]
    pooled = pool_history([{"windows": [window]}, {"windows": [fuller]}], now=datetime.fromisoformat("2026-10-02T00:00:00+00:00"))
    assert pooled["products"][0]["account_count"] == 1 and pooled["products"][0]["estimate_usd"] == 320.0
