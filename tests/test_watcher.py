from __future__ import annotations

from unittest.mock import patch

from subbench.watcher import WatchTarget, ccusage_command, watch


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


def test_the_first_cycle_derives_rather_than_waiting_half_an_hour(monkeypatch):
    """A restart must not leave the stored measurements stale for the next thirty minutes."""
    from subbench import watcher as module

    derived = []
    monkeypatch.setattr(module, "build_reports", lambda db: derived.append(db) or {})
    monkeypatch.delenv("SUBBENCH_PUSH_URL", raising=False)
    monkeypatch.delenv("SUBBENCH_PUSH_TOKEN", raising=False)

    module.refresh_if_due(None, last_refreshed=0.0, now=1.0, emit=lambda _m: None)
    assert derived == [None]


def test_a_derivation_failure_does_not_stop_collection(monkeypatch):
    from subbench import watcher as module

    def explode(_db):
        raise RuntimeError("estimator fell over")

    monkeypatch.setattr(module, "build_reports", explode)
    messages = []
    assert module.refresh_if_due(None, last_refreshed=0.0, now=1.0, emit=messages.append) == 1.0
    assert "derive failed" in messages[0]
