"""Scoped limits (Fable's own sub-limit inside the weekly limit) and partial refills."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from subbench.entitlement import _normalise_claude
from subbench.products import product_label, product_view
from subbench.regression import robust_estimates
from subbench.scopes import model_in_scope, scope_of, separate
from subbench.store import connect, regression_points

START = datetime(2026, 11, 9, 19, 0, tzinfo=timezone.utc)
RESET = "2026-11-16T19:00:00+00:00"
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)

# What the meter looked like on 2026-11-14, trimmed to the fields that matter.
PAYLOAD = {
    "five_hour": {"utilization": 2.0, "resets_at": "2026-11-14T04:39:59.598245+00:00"},
    "seven_day": {"utilization": 77.0, "resets_at": "2026-11-16T18:59:59.598261+00:00"},
    "limits": [
        {"kind": "session", "percent": 2, "resets_at": "2026-11-14T04:39:59.598245+00:00", "scope": None},
        {"kind": "weekly_all", "percent": 77, "resets_at": "2026-11-16T18:59:59.598261+00:00", "scope": None},
        {"kind": "weekly_scoped", "percent": 93, "resets_at": "2026-11-16T18:59:59.598374+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        {"kind": "weekly_scoped", "percent": 40, "resets_at": "2026-11-16T18:59:59+00:00",
         "scope": {"model": None, "surface": "cowork"}},
    ],
    "weekly_scoped_shares": [{"limit_index": 2, "used_percent_of_weekly": 47, "allowance_percent_of_weekly": 50}],
    "organizationRateLimitTier": "default_claude_max_5x",
    "account_uuid": "acct",
}


def test_the_fable_sub_limit_is_recorded_with_its_share_of_the_weekly_limit():
    rows = {row.window: row for row in _normalise_claude(PAYLOAD)}
    assert set(rows) == {"five_hour", "weekly", "weekly_fable"}, "a surface-scoped limit has no model to price"
    fable = rows["weekly_fable"]
    assert (fable.used_percent, fable.allowance_percent, fable.plan) == (93.0, 50.0, "max_5x")
    assert fable.resets_at == "2026-11-16T19:00:00+00:00" == rows["weekly"].resets_at
    assert rows["weekly"].allowance_percent is None


def test_a_scope_matches_model_ids_as_a_whole_word():
    assert scope_of("weekly_fable") == "fable" and scope_of("weekly") is None and scope_of("five_hour") is None
    assert model_in_scope("claude-fable-5-2", "fable")
    assert not model_in_scope("claude-fableish-1", "fable")


def _row(window, minutes, used, cost, scoped=0.0, allowance=None, account="acct"):
    return {"provider": "claude", "account_id": account, "window": window, "plan": "max_5x",
            "observed_at": (START + timedelta(minutes=minutes)).isoformat(), "resets_at": RESET,
            "duration_minutes": 300 if window == "five_hour" else 10080, "used_percent": float(used),
            "cost_usd": float(cost), "cost_age_minutes": 0.0, "scoped_cost_usd": float(scoped),
            "allowance_percent": allowance}


def test_separate_takes_the_scoped_quota_and_spend_out_of_the_weekly_limit():
    out = separate([
        _row("weekly", 0, 77, 900.0, scoped=272.0),
        _row("weekly_fable", 0, 93, 272.0, allowance=50.0),
        _row("five_hour", 0, 20, 300.0, scoped=100.0),
        _row("weekly", 60, 10, 400.0, scoped=50.0, account="other"),  # no Fable reading beside it
    ])
    weekly, fable, five_hour, unmatched = out
    assert (weekly["used_percent"], weekly["cost_usd"], weekly["excluded_cost_usd"]) == (30.5, 628.0, 0.0)
    assert (fable["used_percent"], fable["cost_usd"], fable["excluded_cost_usd"]) == (93.0, 272.0, 0.0)
    assert (five_hour["used_percent"], five_hour["cost_usd"], five_hour["excluded_cost_usd"]) == (20.0, 200.0, 100.0)
    assert (unmatched["used_percent"], unmatched["cost_usd"], unmatched["excluded_cost_usd"]) == (10.0, 350.0, 50.0)


OPUS_PER_POINT = 14.0   # $1,400 per weekly limit
FABLE_PER_POINT = 5.8   # per weekly point: a full Fable allowance (50 points) is $290


def _week(*, record_fable: bool):
    """A week of alternating Opus and Fable use, with a Fable reset half way.

    Each hour spends 2 weekly points on one family. The Fable meter reads Fable's weekly
    points over its 50-point allowance. The reset gives the Fable share back.
    """
    rows, opus, fable, fable_meter_base = [], 0.0, 0.0, 0.0
    cost = fable_cost = 0.0
    for hour in range(48):
        if hour == 24:
            fable_meter_base = fable  # the reset: Fable's share of the weekly limit comes back
        family = "fable" if (hour // 4) % 2 else "opus"
        if hour:
            if family == "opus":
                opus += 2.0
                cost += 2.0 * OPUS_PER_POINT
            else:
                fable += 2.0
                cost += 2.0 * FABLE_PER_POINT
                fable_cost += 2.0 * FABLE_PER_POINT
        given_back = fable_meter_base
        weekly = opus + fable - given_back
        rows.append(_row("weekly", hour * 60, weekly, cost, scoped=fable_cost))
        if record_fable:
            rows.append(_row("weekly_fable", hour * 60, (fable - given_back) / 0.5, fable_cost, allowance=50.0))
    return rows


def _estimates(rows):
    return {e.window: e for e in robust_estimates(separate(rows), now=NOW)}


def test_a_week_with_fable_values_each_limit_on_its_own_models():
    estimates = _estimates(_week(record_fable=True))
    assert estimates["weekly"].estimate_usd == pytest.approx(100 * OPUS_PER_POINT, rel=0.01)
    assert estimates["weekly_fable"].estimate_usd == pytest.approx(50 * FABLE_PER_POINT, rel=0.01)


def test_without_the_fable_meter_only_the_stretches_without_fable_count():
    weekly = _estimates(_week(record_fable=False))["weekly"]
    # Every pair across Fable use is rejected, so the estimate rests on the Opus stretches.
    assert weekly.estimate_usd == pytest.approx(100 * OPUS_PER_POINT, rel=0.01)
    assert weekly.unobserved_quota_percent > 0


def test_a_partial_refill_is_added_back_and_not_read_as_a_new_window():
    # Quota really used, at $2 a point. Between readings 6 and 7 nothing is used and the
    # provider gives back 49 points (the 2026-11-10 Fable reset: 60% to 11%).
    true = [0, 10, 20, 30, 40, 50, 60, 60, 70, 80, 90, 100, 110, 120]
    shown = [u - (49 if i >= 7 else 0) for i, u in enumerate(true)]
    rows = [_row("weekly", i * 60, shown[i], true[i] * 2.0) for i in range(len(true))]
    estimates = robust_estimates(rows, now=NOW)
    assert len(estimates) == 1
    assert estimates[0].estimate_usd == pytest.approx(200.0, rel=0.01)
    assert estimates[0].quota_span_percent == pytest.approx(120.0)


def test_a_spike_that_reverses_is_not_a_refill():
    used = [0, 10, 20, 30, 54, 31, 40, 50, 60]
    rows = [_row("weekly", i * 60, u, [0, 10, 20, 30, 30, 31, 40, 50, 60][i] * 2.0) for i, u in enumerate(used)]
    (estimate,) = robust_estimates(rows, now=NOW)
    assert estimate.estimate_usd == pytest.approx(200.0, rel=0.05)
    assert estimate.quota_span_percent < 100


def test_a_scoped_limit_is_its_own_product():
    view = product_view({"provider": "claude", "plan": "max_5x", "window": "weekly_fable"})
    assert (view["plan"], view["window"]) == ("max_5x+fable", "weekly")
    assert product_label("claude", view["plan"]) == "Claude Max 5X · Fable"
    assert product_label("claude", "max_5x") == "Claude Max 5X"
    assert product_view({"provider": "claude", "plan": "max_5x", "window": "weekly"})["window"] == "weekly"


def _import(db, stamp, days):
    """A Claude daily report with a model breakdown, as the log reader writes it."""
    raw = {"daily": [{"date": day, "totalCost": opus + fable, "modelBreakdowns": [
        {"modelName": "claude-fable-5-2", "cost": fable}, {"modelName": "claude-opus-5-5", "cost": opus}]}
        for day, opus, fable in days]}
    cursor = db.execute(
        "INSERT INTO imports (imported_at, provider, report, account_id, command, payload_sha256, raw_json, "
        "last_seen_at) VALUES (?, 'claude', 'daily', 'acct', NULL, ?, ?, ?)",
        (stamp, f"hash-{stamp}", json.dumps(raw), stamp))
    for index, (day, opus, fable) in enumerate(days):
        rows = [(None, str(opus + fable), f"$.daily[{index}]"),
                ("claude-fable-5-2", None, f"$.daily[{index}].modelBreakdowns[0]"),
                ("claude-opus-5-5", None, f"$.daily[{index}].modelBreakdowns[1]")]
        for model, cost, path in rows:
            db.execute(
                """INSERT INTO usage_rows (import_id, provider, report, period_start, period_end, model,
                    input_tokens, cached_input_tokens, cache_write_tokens, cache_read_tokens,
                    output_tokens, reasoning_output_tokens, reported_cost_usd, source_path)
                   VALUES (?, 'claude', 'daily', ?, NULL, ?, 1, 0, 0, 0, 1, 0, ?, ?)""",
                (cursor.lastrowid, day, model, cost, path))


def _reading(db, stamp, window, used, allowance=None):
    db.execute(
        """INSERT INTO entitlement_snapshots (observed_at, provider, account_id, window, used_percent,
            resets_at, duration_minutes, source, plan, allowance_percent)
           VALUES (?, 'claude', 'acct', ?, ?, ?, 10080, 'test', 'max_5x', ?)""",
        (stamp, window, used, RESET, allowance))


def test_the_store_prices_a_scoped_window_with_its_own_family_only(tmp_path):
    db = connect(tmp_path / "s.sqlite3")
    stamp = "2026-11-12T00:00:00+00:00"
    _import(db, stamp, [("2026-11-10", 100.0, 40.0), ("2026-11-11", 50.0, 20.0)])
    _reading(db, stamp, "weekly", 50.0)
    db.commit()
    # Before any Fable reading exists, nothing is scoped.
    (weekly,) = regression_points(db)
    assert (weekly["cost_usd"], weekly["excluded_cost_usd"]) == (210.0, 0.0)

    _reading(db, stamp, "weekly_fable", 60.0, 50.0)
    db.commit()
    rows = {row["window"]: row for row in regression_points(db)}
    # The new scope re-prices the weekly reading: Fable's $60 and 30 weekly points come out.
    assert rows["weekly_fable"]["cost_usd"] == pytest.approx(60.0)
    assert rows["weekly"]["cost_usd"] == pytest.approx(150.0)
    assert rows["weekly"]["used_percent"] == pytest.approx(20.0)
    assert rows["weekly"]["excluded_cost_usd"] == pytest.approx(0.0)
