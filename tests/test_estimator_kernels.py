"""The vectorised estimator equals the original, exactly, on awkward generated histories."""
from __future__ import annotations

import random
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pytest

import reference_estimator as reference
from subbench import regression


def _history(seed: int, *, windows: int = 4, per_window: int = 120) -> list[dict]:
    rng = random.Random(seed)
    rows = []
    start = datetime(2026, 8, 3, tzinfo=timezone.utc)
    for provider, account, plan, window, minutes, rate in (
        ("claude", None, "pro", "weekly", 10080, 290.0),
        ("claude", None, "pro", "five_hour", 300, 40.0),
        ("codex", "acct-a", "plus", "weekly", 10080, 50.0),
    ):
        cost = 0.0
        for w in range(windows):
            reset = start + timedelta(minutes=minutes * (w + 1))
            used = 0.0
            count = rng.randint(per_window // 2, per_window)
            for i in range(count):
                moment = reset - timedelta(minutes=minutes) + timedelta(minutes=minutes * (i + 1) / (count + 1))
                roll = rng.random()
                if roll < 0.35 and used < 100:
                    step = rng.choice([1, 1, 2, 3, 5])
                    used = min(100.0, used + step)
                    cost += step / 100 * rate * rng.uniform(0.6, 1.4)
                elif roll < 0.38:
                    used = min(100.0, used + 9)  # quota moved with no spend: off-machine
                elif roll < 0.40 and used >= 95:
                    used = 100.0
                    cost += rng.uniform(0.3, 2.0)  # spend at the ceiling: extra credit
                else:
                    cost += rng.uniform(0, 0.02)
                rows.append({
                    "provider": provider, "account_id": account, "plan": plan, "window": window,
                    # An occasional undated reading, which the loops treat as an unbounded gap.
                    "observed_at": moment.isoformat() if rng.random() > 0.01 else None,
                    "used_percent": used, "cost_usd": round(cost, 6),
                    "resets_at": reset.isoformat(), "duration_minutes": minutes,
                })
    # Undated rows sort first by their text; keep them dated for grouping, undated for timing.
    for row in rows:
        if row["observed_at"] is None:
            row["observed_at"] = "2026-08-03T00:00:00+00:00x"
    return rows


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)  # some windows closed, one open per series


def _oracle(rows, now):
    """The original estimator, applied window by window to the window and its closed-before peers."""
    groups = list(regression._ordered_groups(rows))
    out = []
    for key, _ in groups:
        own_closed = regression._closed(key[4], now)
        subset = [(k, r) for k, r in groups if k[:4] == key[:4] and (k == key or (
            regression._closed(k[4], now) and (not own_closed or k[4] < key[4])))]
        out.extend(e for e in reference.robust_estimates([], groups=subset)
                   if (e.provider, e.account_id, e.plan, e.window, e.reset_key) == key)
    return sorted(out, key=lambda e: e.latest_observed_at, reverse=True)


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_estimates_equal_the_original_estimator_on_closed_peers(seed):
    rows = _history(seed)
    fast = [asdict(e) for e in regression.robust_estimates(rows, now=NOW)]
    slow = [asdict(e) for e in _oracle(rows, NOW)]
    assert fast == slow


def test_pooled_references_equal_the_original(seed=9):
    groups = list(regression._ordered_groups(_history(seed)))
    assert regression._peer_reference_rates(groups, min_quota_delta=2.0) == \
        reference._peer_reference_rates(groups, min_quota_delta=2.0)


def test_stored_windows_are_read_back_not_recomputed(tmp_path, monkeypatch):
    from subbench.store import WindowEstimateStore, connect

    db = connect(tmp_path / "db.sqlite3")
    now = datetime(2026, 8, 20, tzinfo=timezone.utc)  # mid-history: each series has an open window
    rows = [row for row in _history(10) if row["observed_at"] < now.isoformat()]
    first = regression.robust_estimates(rows, now=now, store=WindowEstimateStore(db))
    closed = db.execute("SELECT COUNT(*) FROM window_estimates").fetchone()[0]
    assert closed > 0
    computed = []
    original = regression._compute_window
    monkeypatch.setattr(regression, "_compute_window", lambda *a, **k: computed.append(a[0]) or original(*a, **k))
    regression._ESTIMATES.clear()
    again = regression.robust_estimates(rows, now=now, store=WindowEstimateStore(db))
    assert [asdict(e) for e in again] == [asdict(e) for e in first]
    # Only the open windows (one per series) were estimated again.
    assert computed and all(not regression._closed(key[4], now) for key in computed)
    assert len(computed) <= 3


def test_changed_evidence_recomputes_that_window(tmp_path):
    from subbench.store import WindowEstimateStore, connect

    db = connect(tmp_path / "db.sqlite3")
    rows = _history(11)
    regression.robust_estimates(rows, now=NOW, store=WindowEstimateStore(db))
    late = [dict(row) for row in rows]
    target = next(row for row in late if row["provider"] == "codex" and row["window"] == "weekly")
    target["cost_usd"] += 0.5  # a late usage import repriced an old reading
    fresh = [asdict(e) for e in regression.robust_estimates(late, now=NOW)]
    stored = [asdict(e) for e in regression.robust_estimates(late, now=NOW, store=WindowEstimateStore(db))]
    assert stored == fresh


@pytest.mark.parametrize("seed", [6, 7])
def test_replay_answer_is_the_newest_estimate(seed):
    rows = sorted(_history(seed, windows=3, per_window=60), key=lambda row: row["observed_at"])
    series = [row for row in rows if row["provider"] == "claude" and row["window"] == "weekly"]
    for end in range(5, len(series), 17):
        prefix = series[: end + 1]
        moment = regression._moment(prefix[-1]["observed_at"])
        expected = [e for e in regression.robust_estimates(prefix, now=moment)
                    if e.provider == "claude" and e.account_id is None and e.window == "weekly"]
        expected = max(expected, key=lambda e: e.latest_observed_at) if expected else None
        got = regression.newest_estimate(prefix, provider="claude", account_id=None, window="weekly", now=moment)
        assert (asdict(got) if got else None) == (asdict(expected) if expected else None)


def test_valid_pairs_keep_the_loop_order(seed=8):
    rows = [row for row in _history(seed, windows=1) if row["window"] == "weekly" and row["provider"] == "claude"]
    rows.sort(key=lambda row: row["observed_at"])
    assert regression._valid_pairs(rows, min_quota_delta=2.0, reference_rate=150.0) == \
        reference._valid_pairs(rows, min_quota_delta=2.0, reference_rate=150.0)
    assert regression._unobserved_intervals(rows, min_value_per_quota_point=0.01, reference_rate=150.0) == \
        reference._unobserved_intervals(rows, min_value_per_quota_point=0.01, reference_rate=150.0)
