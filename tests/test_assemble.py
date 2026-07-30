from subbench.server.assemble import assemble_points


def snapshot(observed_at, used, *, account_key="A", account_id="A", window="weekly"):
    return {
        "provider": "codex", "account_id": account_id, "account_key": account_key,
        "window": window, "observed_at": observed_at, "used_percent": used,
        "resets_at": "2026-08-05T00:00:00+00:00", "duration_minutes": 10080,
    }


def day(agent, import_key, last_seen_at, cost, *, period="2026-08-01", account_key="A"):
    return {
        "agent_id": agent, "import_key": import_key, "provider": "codex",
        "account_key": account_key, "last_seen_at": last_seen_at,
        "period_start": period, "cost_usd": cost,
    }


def test_single_agent_matches_its_own_cost():
    points = assemble_points(
        [snapshot("2026-08-01T00:10:00+00:00", 10.0)],
        [day("a1", "1", "2026-08-01T00:09:00+00:00", 5.0)],
    )
    assert len(points) == 1
    assert points[0]["cost_usd"] == 5.0
    assert points[0]["contributing_agents"] == 1


def test_two_machines_on_one_account_have_their_spend_summed():
    # The point of the whole module: each ccusage sees only its own machine's logs, so
    # the entitlement's real spend is the sum. Taking either alone understates it, which
    # is exactly what makes quota look free.
    points = assemble_points(
        [snapshot("2026-08-01T00:10:00+00:00", 10.0)],
        [
            day("a1", "1", "2026-08-01T00:09:00+00:00", 5.0),
            day("a2", "9", "2026-08-01T00:09:30+00:00", 3.0),
        ],
    )
    assert points[0]["cost_usd"] == 8.0
    assert points[0]["contributing_agents"] == 2


def test_each_agent_contributes_only_its_newest_import():
    points = assemble_points(
        [snapshot("2026-08-01T00:10:00+00:00", 10.0)],
        [
            day("a1", "1", "2026-08-01T00:01:00+00:00", 5.0),
            day("a1", "2", "2026-08-01T00:09:00+00:00", 7.0),
        ],
    )
    assert points[0]["cost_usd"] == 7.0


def test_imports_after_the_reading_are_ignored():
    points = assemble_points(
        [snapshot("2026-08-01T00:10:00+00:00", 10.0)],
        [
            day("a1", "1", "2026-08-01T00:09:00+00:00", 5.0),
            day("a1", "2", "2026-08-01T00:11:00+00:00", 50.0),
        ],
    )
    assert points[0]["cost_usd"] == 5.0


def test_days_outside_the_window_are_excluded():
    points = assemble_points(
        [snapshot("2026-08-01T00:10:00+00:00", 10.0)],
        [
            day("a1", "1", "2026-08-01T00:09:00+00:00", 5.0, period="2026-08-01"),
            day("a1", "1", "2026-08-01T00:09:00+00:00", 99.0, period="2026-07-01"),
        ],
    )
    assert points[0]["cost_usd"] == 5.0


def test_freshness_is_that_of_the_stalest_contributor():
    # A point is only as current as its least current input. One machine reporting
    # promptly must not make a stale second machine look fresh.
    points = assemble_points(
        [snapshot("2026-08-01T05:00:00+00:00", 10.0)],
        [
            day("a1", "1", "2026-08-01T04:59:00+00:00", 5.0),
            day("a2", "9", "2026-08-01T00:00:00+00:00", 3.0),
        ],
        max_cost_age_minutes=30.0,
    )
    assert points == []


def test_accounts_are_never_merged():
    points = assemble_points(
        [snapshot("2026-08-01T00:10:00+00:00", 10.0, account_key="A", account_id="A")],
        [
            day("a1", "1", "2026-08-01T00:09:00+00:00", 5.0, account_key="A"),
            day("a2", "9", "2026-08-01T00:09:00+00:00", 500.0, account_key="B"),
        ],
    )
    assert points[0]["cost_usd"] == 5.0


def test_snapshot_with_no_evidence_is_dropped_not_zeroed():
    # Absent evidence is not evidence of absent spend; a zero would read as a window of
    # free quota and drag every slope through it.
    assert assemble_points([snapshot("2026-08-01T00:10:00+00:00", 10.0)], []) == []


def test_multi_agent_recovers_coverage_a_single_agent_would_lose():
    from subbench.regression import robust_estimates

    snapshots = [snapshot(f"2026-08-01T0{i}:00:00+00:00", i * 10.0) for i in range(1, 6)]
    # a1 records a little; a2 records the rest of the same work.
    imports = []
    for i in range(1, 6):
        imports.append(day("a1", str(i), f"2026-08-01T0{i}:00:00+00:00", 0.5 * i))
        imports.append(day("a2", f"b{i}", f"2026-08-01T0{i}:00:00+00:00", 4.5 * i))

    solo = robust_estimates(assemble_points(snapshots, [r for r in imports if r["agent_id"] == "a1"]))
    both = robust_estimates(assemble_points(snapshots, imports))
    assert both[0].estimate_usd > solo[0].estimate_usd * 5
    assert both[0].covered_quota_percent >= solo[0].covered_quota_percent
