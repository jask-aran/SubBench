from __future__ import annotations

import json
import os
import queue
import shlex
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TextIO


APP_SERVER_RESPONSE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class EntitlementWindow:
    provider: str
    window: str
    used_percent: float
    resets_at: str | None
    duration_minutes: int | None
    source: str


def collect_entitlements(provider: str) -> list[EntitlementWindow]:
    if provider == "codex":
        return collect_codex()
    if provider == "claude":
        return collect_claude()
    raise ValueError(f"unsupported provider: {provider}")


def collect_codex() -> list[EntitlementWindow]:
    process = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    messages: queue.Queue[str | None] = queue.Queue()
    stderr_lines: deque[str] = deque(maxlen=20)
    stdout_thread = threading.Thread(target=_read_stdout, args=(process.stdout, messages), daemon=True)
    stderr_thread = threading.Thread(target=_read_stderr, args=(process.stderr, stderr_lines), daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        _send_request(
            process.stdin,
            {
                "method": "initialize",
                "id": 1,
                "params": {
                    "clientInfo": {
                        "name": "subbench",
                        "title": "SubBench",
                        "version": "0.2.0",
                    },
                    "capabilities": {},
                },
            },
        )
        _read_response(messages, request_id=1, operation="initialization", stderr_lines=stderr_lines)
        _send_request(process.stdin, {"method": "initialized", "params": {}})
        _send_request(process.stdin, {"method": "account/rateLimits/read", "id": 2})
        result = _read_response(
            messages,
            request_id=2,
            operation="rate-limit request",
            stderr_lines=stderr_lines,
        )
        windows = _normalise_codex(result)
        if not windows:
            raise RuntimeError("codex app-server returned no usable rate-limit windows")
        return windows
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        stdout_thread.join(timeout=0.2)
        stderr_thread.join(timeout=0.2)


def collect_claude() -> list[EntitlementWindow]:
    command = os.environ.get("SUBBENCH_CLAUDE_USAGE_COMMAND")
    if not command:
        raise RuntimeError(
            "Claude entitlement collection requires SUBBENCH_CLAUDE_USAGE_COMMAND; "
            "set it to a local command that prints the OAuth usage response as JSON"
        )
    result = subprocess.run(shlex.split(command), check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"Claude usage command exited {result.returncode}")
    return _normalise_claude(json.loads(result.stdout))


def _normalise_codex(payload: dict[str, Any]) -> list[EntitlementWindow]:
    limits = payload.get("rateLimits")
    if not isinstance(limits, dict) or not any(limits.get(key) for key in ("primary", "secondary")):
        buckets = payload.get("rateLimitsByLimitId")
        if isinstance(buckets, dict):
            preferred = buckets.get("codex")
            if isinstance(preferred, dict):
                limits = preferred
    if not isinstance(limits, dict):
        limits = payload
    rows: list[EntitlementWindow] = []
    for name, key in (("primary", "primary"), ("secondary", "secondary")):
        window = limits.get(key)
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            continue
        duration = _int_or_none(window.get("windowDurationMins"))
        label = "five_hour" if duration and 240 <= duration <= 360 else "weekly" if duration and duration >= 6 * 24 * 60 else name
        rows.append(EntitlementWindow("codex", label, float(window["usedPercent"]), _epoch_iso(window.get("resetsAt")), duration, "codex-app-server"))
    return rows


def _normalise_claude(payload: dict[str, Any]) -> list[EntitlementWindow]:
    aliases = {
        "five_hour": "five_hour",
        "fiveHour": "five_hour",
        "seven_day": "weekly",
        "sevenDay": "weekly",
        "weekly": "weekly",
    }
    rows: list[EntitlementWindow] = []
    for key, label in aliases.items():
        value = payload.get(key)
        if not isinstance(value, dict):
            continue
        utilisation = value.get("utilization", value.get("usedPercent"))
        if utilisation is None:
            continue
        used = float(utilisation)
        if 0 <= used <= 1:
            used *= 100
        reset = value.get("resets_at", value.get("resetsAt"))
        rows.append(EntitlementWindow("claude", label, used, _time_iso(reset), None, "claude-oauth-usage"))
    return rows


def _epoch_iso(value: Any) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat()


def _time_iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return _epoch_iso(value)
    return str(value)


def _int_or_none(value: Any) -> int | None:
    return None if value is None else int(value)


def _send_request(stdin: TextIO, request: dict[str, Any]) -> None:
    stdin.write(json.dumps(request) + "\n")
    stdin.flush()


def _read_stdout(stdout: TextIO, messages: queue.Queue[str | None]) -> None:
    try:
        for line in stdout:
            messages.put(line)
    finally:
        messages.put(None)


def _read_stderr(stderr: TextIO, lines: deque[str]) -> None:
    for line in stderr:
        stripped = line.strip()
        if stripped:
            lines.append(stripped)


def _read_response(
    messages: queue.Queue[str | None],
    *,
    request_id: int,
    operation: str,
    stderr_lines: deque[str],
) -> dict[str, Any]:
    deadline = time.monotonic() + APP_SERVER_RESPONSE_TIMEOUT_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            detail = _stderr_detail(stderr_lines)
            raise RuntimeError(f"codex app-server timed out during {operation}{detail}")
        try:
            line = messages.get(timeout=remaining)
        except queue.Empty as error:
            detail = _stderr_detail(stderr_lines)
            raise RuntimeError(f"codex app-server timed out during {operation}{detail}") from error
        if line is None:
            detail = _stderr_detail(stderr_lines)
            raise RuntimeError(f"codex app-server ended during {operation}{detail}")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError(f"codex app-server returned invalid JSON during {operation}") from error
        if message.get("id") != request_id:
            continue
        rpc_error = message.get("error")
        if isinstance(rpc_error, dict):
            code = rpc_error.get("code")
            text = rpc_error.get("message") or "unknown JSON-RPC error"
            raise RuntimeError(f"codex app-server {operation} failed ({code}): {text}")
        result = message.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"codex app-server returned an invalid {operation} response")
        return result


def _stderr_detail(lines: deque[str]) -> str:
    return f": {' | '.join(lines)}" if lines else ""
