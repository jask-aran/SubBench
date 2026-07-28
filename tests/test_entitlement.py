from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from subbench.entitlement import _normalise_claude, _normalise_codex, collect_codex


class FakeProcess:
    def __init__(self, messages: list[dict], stderr: str = "") -> None:
        self.stdin = io.StringIO()
        self.stdout = io.StringIO("".join(json.dumps(message) + "\n" for message in messages))
        self.stderr = io.StringIO(stderr)
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_normalise_codex_windows():
    rows = _normalise_codex({
        "rateLimits": {
            "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1780000000},
            "secondary": {"usedPercent": 40, "windowDurationMins": 10080, "resetsAt": 1780500000},
        }
    })
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 25.0), ("weekly", 40.0)]


def test_normalise_codex_falls_back_to_named_bucket():
    rows = _normalise_codex({
        "rateLimits": {},
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"usedPercent": 15, "windowDurationMins": 300, "resetsAt": 1780000000},
            }
        },
    })
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 15.0)]


def test_normalise_claude_fractional_utilisation():
    rows = _normalise_claude({
        "five_hour": {"utilization": 0.12, "resets_at": "2026-07-27T10:00:00Z"},
        "seven_day": {"utilization": 0.34, "resets_at": "2026-08-01T10:00:00Z"},
    })
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 12.0), ("weekly", 34.0)]


def test_collect_codex_waits_for_initialize_and_returns_windows():
    process = FakeProcess([
        {"id": 1, "result": {"userAgent": "codex-test"}},
        {
            "id": 2,
            "result": {
                "rateLimits": {
                    "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1780000000},
                }
            },
        },
    ])
    with patch("subbench.entitlement.subprocess.Popen", return_value=process):
        rows = collect_codex()

    requests = [json.loads(line) for line in process.stdin.getvalue().splitlines()]
    assert [request["method"] for request in requests] == [
        "initialize",
        "initialized",
        "account/rateLimits/read",
    ]
    assert [(row.window, row.used_percent) for row in rows] == [("five_hour", 25.0)]
    assert process.returncode is not None


def test_collect_codex_surfaces_json_rpc_error():
    process = FakeProcess([
        {"id": 1, "result": {"userAgent": "codex-test"}},
        {"id": 2, "error": {"code": -32603, "message": "authentication failed"}},
    ])
    with (
        patch("subbench.entitlement.subprocess.Popen", return_value=process),
        pytest.raises(RuntimeError, match=r"rate-limit request failed \(-32603\): authentication failed"),
    ):
        collect_codex()
