"""The built-in log reader reports what ccusage 20 reports for the same logs.

tests/parity/generate.py writes Claude Code and Codex logs exercising every rule ccusage
applies: forks and subagents replaying their parent, compactions in either order, zstd
rollouts and duplicate copies, service tiers, codex-auto-review, headless exec lines,
long-context requests, streamed and resumed Claude messages, sidechain replays, advisor
iterations, fast mode, recorded costs, malformed lines. The golden files are ccusage's own
`daily --json --offline` output for each seed (tests/parity/record.sh).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from subbench import logs
from subbench.prices import Prices, _bundled

HERE = Path(__file__).parent / "parity"
sys.path.insert(0, str(HERE))
import generate  # noqa: E402

pytest.importorskip("zstandard")
SEEDS = sorted(int(p.stem.split("-")[1]) for p in (HERE / "golden").glob("seed-*.json"))


def _rows(report: dict) -> dict[tuple[str, str], tuple]:
    rows = {}
    for day in report["daily"]:
        cost = day.get("totalCost", day.get("costUSD"))
        rows[(day["date"], "*")] = (round(cost, 9), day["inputTokens"], day["outputTokens"],
                                    day["cacheCreationTokens"], day["cacheReadTokens"])
        models = day.get("modelBreakdowns") or [dict(modelName=k, **v) for k, v in day.get("models", {}).items()]
        for model in models:
            rows[(day["date"], model["modelName"])] = (
                round(model["cost"], 9) if "cost" in model else None, model["inputTokens"], model["outputTokens"],
                model["cacheCreationTokens"], model["cacheReadTokens"])
    return rows


def _same(expected: dict, actual: dict) -> list[str]:
    problems = []
    for key in sorted(set(expected) | set(actual)):
        a, b = expected.get(key), actual.get(key)
        if a is None or b is None:
            problems.append(f"{key}: ccusage {a}, subbench {b}")
            continue
        for x, y in zip(a, b):
            if x is not None and y is not None and abs(x - y) > 1e-6 * max(1.0, abs(x)):
                problems.append(f"{key}: ccusage {a}, subbench {b}")
                break
    return problems


@pytest.fixture
def utc(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("SUBBENCH_OFFLINE", "1")
    time.tzset()
    yield
    time.tzset()


@pytest.mark.parametrize("seed", SEEDS)
def test_daily_reports_match_ccusage(seed, tmp_path, monkeypatch, utc):
    home = tmp_path / "home"
    generate.generate(seed, home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    golden = json.loads((HERE / "golden" / f"seed-{seed}.json").read_text())
    for provider in ("claude", "codex"):
        report = logs.LogReader(provider, Prices(_bundled())).daily()
        assert _same(_rows(golden[provider]), _rows(report)) == [], provider
