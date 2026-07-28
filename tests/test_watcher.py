from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from subbench.store import connect
from subbench.watcher import WatchTarget, ccusage_command, collect_target, watch

FIXTURES = Path(__file__).parent / "fixtures" / "ccusage"


def test_ccusage_command() -> None:
    assert ccusage_command(
        runner="npx", provider="codex", report="daily"
    ) == ["npx", "--yes", "ccusage@latest", "codex", "daily", "--json"]


def test_watch_once_collects_all_targets() -> None:
    emitted: list[str] = []
    targets = [WatchTarget("claude"), WatchTarget("codex")]

    with patch(
        "subbench.watcher.collect_target",
        side_effect=[(True, "claude ok"), (True, "codex ok")],
    ) as collect:
        result = watch(
            object(),
            targets=targets,
            runner="npx",
            interval_seconds=60,
            once=True,
            emit=emitted.append,
        )

    assert result == 0
    assert collect.call_count == 2
    assert emitted[0].endswith("claude ok")
    assert emitted[1].endswith("codex ok")


def test_watch_once_reports_failure() -> None:
    with patch(
        "subbench.watcher.collect_target",
        return_value=(False, "failed"),
    ):
        result = watch(
            object(),
            targets=[WatchTarget("claude")],
            runner="npx",
            interval_seconds=60,
            once=True,
            emit=lambda _: None,
        )

    assert result == 1


def test_collect_target_fails_diagnostic_run_when_entitlement_is_unavailable() -> None:
    completed = type("Completed", (), {"returncode": 0, "stdout": b"{}", "stderr": b""})()
    with (
        patch("subbench.watcher.subprocess.run", return_value=completed),
        patch("subbench.watcher.normalise_payload", return_value=[]),
        patch("subbench.watcher.save_import", return_value=(1, 0, True)),
        patch("subbench.watcher.collect_entitlements", side_effect=RuntimeError("authentication failed")),
    ):
        ok, message = collect_target(object(), target=WatchTarget("codex"), runner="npx")

    assert not ok
    assert "entitlement unavailable: authentication failed" in message


def test_collect_target_consumes_current_ccusage_contract(tmp_path: Path) -> None:
    raw = (FIXTURES / "codex-daily-v20.0.19.json").read_bytes()
    completed = type(
        "Completed",
        (),
        {"returncode": 0, "stdout": raw, "stderr": b""},
    )()
    db = connect(tmp_path / "subbench.sqlite3")

    with (
        patch("subbench.watcher.subprocess.run", return_value=completed),
        patch("subbench.watcher.collect_entitlements", return_value=[]),
    ):
        ok, message = collect_target(db, target=WatchTarget("codex"), runner="npx")

    stored = db.execute(
        """SELECT model, input_tokens, cache_read_tokens, reported_cost_usd
           FROM usage_rows ORDER BY id"""
    ).fetchall()
    cost = db.execute(
        """SELECT SUM(CAST(COALESCE(reported_cost_usd, '0') AS REAL))
           FROM usage_rows"""
    ).fetchone()[0]

    assert ok
    assert "usage recorded import 1 (3 rows)" in message
    assert len(stored) == 3
    assert round(cost, 9) == round(0.006365, 9)
