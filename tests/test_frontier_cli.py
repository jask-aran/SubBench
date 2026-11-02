"""The terminal's frontier arithmetic, held to the same cases as web/src/lib/frontier.js."""
from subbench.frontier import buys, frontier_ids, load_bundle, match_model, normalise_model_name


def test_names_normalise_like_the_site():
    assert normalise_model_name("gpt-5.6-sol") == "gpt-5-6-sol"
    assert normalise_model_name("claude-opus-5-20260724") == "claude-opus-5"
    assert normalise_model_name("anthropic/Claude_Sonnet 5") == "claude-sonnet-5"


def test_a_newer_model_matches_nothing_rather_than_a_neighbour():
    ids = ["gpt-5-6-sol", "gpt-5.6-sol", "claude-opus-5", "claude-fable-5-high"]
    assert match_model("gpt-5.6-sol", ids) == "gpt-5-6-sol"
    assert match_model("claude-fable-5", ids) == "claude-fable-5-high"
    assert match_model("claude-opus-5-5", ids) is None


def test_the_frontier_keeps_points_no_cheaper_point_beats():
    points = [{"key": k, "score": s, "cost": c} for k, s, c in [("a", 10, 1), ("b", 30, 2), ("c", 20, 3), ("d", 40, 4)]]
    assert frontier_ids(points) == {"a", "b", "d"}


def test_buys_divide_the_weekly_value_by_the_task_cost():
    bundle = {
        "entities": {"model": [{"id": "m1", "label": "M1"}, {"id": "m2", "label": "M2"}, {"id": "m3", "label": "M3"}]},
        "observations": [
            {"entity_id": "m1", "metric_id": "x", "score": 0.5, "resources": {"cost": 2.0}},
            {"entity_id": "m2", "metric_id": "x", "score": 0.4, "resources": {"cost": 4.0}},
            {"entity_id": "m3", "metric_id": "x", "score": 0.6, "resources": {"cost": 10.0}},
        ],
    }
    rows = {row.model_id: row for row in buys(bundle, "x", 100.0, ["m2"])}
    assert set(rows) == {"m1", "m2", "m3"}  # m2 is dominated but yours
    assert rows["m1"].tasks == 50 and rows["m1"].frontier
    assert rows["m2"].yours and not rows["m2"].frontier


def test_the_repository_bundle_loads():
    bundle = load_bundle()
    assert any(metric["id"] == "aa-intelligence" for metric in bundle["metrics"])
