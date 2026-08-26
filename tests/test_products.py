from datetime import datetime, timedelta, timezone

from subbench.products import (
    POOL_DAYS,
    completed_direct,
    product_estimates,
    product_label,
)
from subbench.regression import robust_estimates

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


def _window(**overrides):
    row = {
        "provider": "codex",
        "plan": "plus",
        "account_id": "acct-A",
        "window": "weekly",
        "reset_key": (NOW - timedelta(days=2)).isoformat(),
        "estimate_usd": 100.0,
        "covered_quota_percent": 60.0,
        "tier": "confirmed",
    }
    row.update(overrides)
    return row


def test_product_label_joins_provider_and_reported_plan():
    assert product_label("codex", "plus") == "ChatGPT Plus"
    assert product_label("codex", "pro") == "ChatGPT Pro"
    assert product_label("codex", "max_20x") == "ChatGPT Max 20X"


def test_product_label_without_a_plan_names_the_provider_only():
    assert product_label("claude", None) == "Claude"
    assert product_label("claude", "") == "Claude"


def test_product_label_does_not_repeat_a_provider_named_plan():
    assert product_label("claude", "claude_pro") == "Claude Pro"


def test_pooling_combines_accounts_that_hold_the_same_plan():
    rows = [
        _window(account_id="acct-A", estimate_usd=100.0),
        _window(account_id="acct-B", estimate_usd=120.0),
    ]
    pooled = product_estimates(rows, now=NOW)
    assert len(pooled) == 1
    assert pooled[0].product == "ChatGPT Plus"
    assert pooled[0].account_count == 2
    assert pooled[0].window_count == 2
    assert 100.0 <= pooled[0].estimate_usd <= 120.0


def test_a_different_plan_is_a_different_product():
    rows = [
        _window(plan="plus", estimate_usd=100.0),
        _window(plan="pro", estimate_usd=400.0, account_id="acct-B"),
    ]
    pooled = {row.product: row for row in product_estimates(rows, now=NOW)}
    assert set(pooled) == {"ChatGPT Plus", "ChatGPT Pro"}
    assert pooled["ChatGPT Plus"].estimate_usd == 100.0
    assert pooled["ChatGPT Pro"].estimate_usd == 400.0


def test_pooling_weights_a_window_by_the_quota_it_measured():
    """The window that watched most of the meter decides the pooled figure."""
    rows = [
        _window(account_id="acct-A", estimate_usd=100.0, covered_quota_percent=90.0),
        _window(account_id="acct-B", estimate_usd=300.0, covered_quota_percent=26.0),
    ]
    assert product_estimates(rows, now=NOW)[0].estimate_usd == 100.0


def test_windows_that_are_not_confirmed_never_pool():
    assert product_estimates([_window(tier="likely")], now=NOW) == []
    assert product_estimates([_window(tier="provisional")], now=NOW) == []


def test_open_windows_never_pool():
    ahead = (NOW + timedelta(days=1)).isoformat()
    assert product_estimates([_window(reset_key=ahead)], now=NOW) == []


def test_converted_windows_never_pool():
    """A "~via~" window restates evidence already present, so pooling it counts it twice."""
    reset_key = (NOW - timedelta(days=2)).isoformat() + "~via~five_hour"
    assert product_estimates([_window(reset_key=reset_key)], now=NOW) == []


def test_windows_older_than_the_pool_period_are_left_out():
    old = (NOW - timedelta(days=POOL_DAYS + 1)).isoformat()
    assert product_estimates([_window(reset_key=old)], now=NOW) == []
    assert len(product_estimates([_window(reset_key=old)], now=NOW, within_days=None)) == 1


def test_completed_direct_keeps_only_finished_confirmed_direct_windows():
    rows = [
        _window(),
        _window(tier="likely"),
        _window(reset_key=(NOW + timedelta(days=1)).isoformat()),
        _window(estimate_usd=0.0),
    ]
    assert len(completed_direct(rows, now=NOW)) == 1


def _point(observed_at, used_percent, cost_usd, *, plan, resets_at="2026-08-05T00:00:00+00:00"):
    return {
        "provider": "codex",
        "account_id": "acct-A",
        "plan": plan,
        "window": "weekly",
        "observed_at": observed_at,
        "used_percent": used_percent,
        "cost_usd": cost_usd,
        "resets_at": resets_at,
    }


def test_a_plan_change_splits_the_series_instead_of_mixing_quota_points():
    """One quota point on Plus is not one quota point on Pro, so the two never share slopes."""
    points = [
        _point("2026-08-01T00:00:00+00:00", 10.0, 10.0, plan="plus"),
        _point("2026-08-01T02:00:00+00:00", 30.0, 30.0, plan="plus"),
        _point("2026-08-01T04:00:00+00:00", 10.0, 50.0, plan="pro"),
        _point("2026-08-01T06:00:00+00:00", 30.0, 130.0, plan="pro"),
    ]
    estimates = robust_estimates(points)
    by_plan = {estimate.plan: estimate for estimate in estimates}
    assert set(by_plan) == {"plus", "pro"}
    # 20 dollars over 20 quota points is 100 dollars a limit; 80 over 20 is 400.
    assert by_plan["plus"].estimate_usd == 100.0
    assert by_plan["pro"].estimate_usd == 400.0
