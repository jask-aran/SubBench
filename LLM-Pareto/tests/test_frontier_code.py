from __future__ import annotations

import copy
import unittest

import frontier_code


def source_row(score: float, pass_rate: float, cost: float, tokens: float) -> dict:
    return {
        "correct": pass_rate,
        "new_score": score,
        "tokens": tokens,
        "cost": cost,
        "tool_calls": None,
        "steps": None,
        "ote": None,
    }


FIXTURE = {
    "v1_1": {
        "models": ["Model A", "Model B"],
        "colors": {"Model A": "#111111", "Model B": "#222222"},
        "lab_colors": {"Lab": "#111111"},
        "harness": {"Model A": "agent-a", "Model B": "agent-b"},
        "efforts": {"Model A": ["low", "high"], "Model B": ["none"]},
        "subsets": {"main": 100, "extended": 150},
        "data": {
            "Model A": {
                "low": {
                    "main": source_row(0.2, 0.3, 1.0, 100),
                    "extended": source_row(0.3, 0.4, 1.1, 110),
                },
                "high": {
                    "main": source_row(0.4, 0.529, 2.0, 200),
                    "extended": source_row(0.5, 0.6, 2.1, 210),
                },
            },
            "Model B": {
                "none": {
                    "main": source_row(0.3, 0.4, 0.5, 50),
                    "extended": source_row(0.4, 0.5, 0.6, 60),
                }
            },
        },
    }
}


class FrontierCodeTests(unittest.TestCase):
    def test_best_effort_rows_are_ranked_and_normalized(self) -> None:
        result = frontier_code.extract_leaderboard(
            FIXTURE,
            collected_at="2026-07-28T00:00:00Z",
        )

        rows = result["data"]["results"]
        self.assertEqual([row["model"] for row in rows], ["Model A", "Model B"])
        self.assertEqual(rows[0]["reasoning_effort"], "high")
        self.assertEqual(rows[0]["score_percent"], 40.0)
        self.assertEqual(rows[0]["pass_rate_percent"], 52.9)
        self.assertEqual([row["rank"] for row in rows], [1, 2])
        self.assertEqual(result["data"]["task_count"], 100)
        self.assertEqual(result["data"]["selection"], "best_reasoning_effort")

    def test_all_efforts_and_model_filter(self) -> None:
        result = frontier_code.extract_leaderboard(
            FIXTURE,
            models=["Model A"],
            all_efforts=True,
        )

        rows = result["data"]["results"]
        self.assertEqual(
            [(row["reasoning_effort"], row["score_percent"]) for row in rows],
            [("high", 40.0), ("low", 20.0)],
        )
        self.assertEqual(result["data"]["model_count"], 1)
        self.assertEqual(result["data"]["count"], 2)

    def test_subset_selection(self) -> None:
        result = frontier_code.extract_leaderboard(
            FIXTURE,
            subset="extended",
            models=["Model B"],
        )

        self.assertEqual(result["data"]["results"][0]["score_percent"], 40.0)
        self.assertEqual(result["data"]["task_count"], 150)

    def test_verbose_preserves_source_record(self) -> None:
        result = frontier_code.extract_leaderboard(
            FIXTURE,
            models=["Model B"],
            include_source=True,
        )

        self.assertEqual(
            result["data"]["results"][0]["source_record"],
            FIXTURE["v1_1"]["data"]["Model B"]["none"]["main"],
        )

    def test_unknown_model_is_an_error(self) -> None:
        with self.assertRaisesRegex(frontier_code.ExtractionError, "Unknown model"):
            frontier_code.extract_leaderboard(FIXTURE, models=["Missing"])

    def test_schema_drift_is_an_error(self) -> None:
        broken = copy.deepcopy(FIXTURE)
        del broken["v1_1"]["data"]["Model A"]["low"]["main"]["new_score"]

        with self.assertRaisesRegex(frontier_code.ExtractionError, "missing fields"):
            frontier_code.extract_leaderboard(broken)

    def test_boolean_is_not_accepted_as_a_score(self) -> None:
        broken = copy.deepcopy(FIXTURE)
        broken["v1_1"]["data"]["Model A"]["low"]["main"]["new_score"] = True

        with self.assertRaisesRegex(frontier_code.ExtractionError, "must be a number"):
            frontier_code.extract_leaderboard(broken)

    def test_model_argument_parser_deduplicates(self) -> None:
        self.assertEqual(
            frontier_code.parse_models(["Model A,Model B", "Model A"]),
            ["Model A", "Model B"],
        )


if __name__ == "__main__":
    unittest.main()

