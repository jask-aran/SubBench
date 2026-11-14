"""Offline tests for the Artificial Analysis scraper, on pages shaped like the live site.

The live pages change without notice; these pin the shapes the scraper was last fixed
against (2026-11) so a regression in the parsing shows here, and `--check` covers the
site itself.
"""
from __future__ import annotations

import json
import unittest

import artificial_analysis_v2 as aa


def flight_page(*objects: dict) -> str:
    """A page whose Next.js flight payload embeds the objects, as AA's pages do."""
    payload = "0:" + json.dumps(list(objects), separators=(",", ":"))
    script = "self.__next_f.push(" + json.dumps([1, payload]) + ")"
    return f"<html><body><script>{script}</script></body></html>"


def model_record(slug: str, **overrides) -> dict:
    record = {
        "id": f"id-{slug}", "slug": slug, "name": slug.upper(), "shortName": slug.title(),
        "releaseDate": "2026-10-01", "creator": {"name": "Lab"}, "isReasoning": True,
        "intelligenceIndex": 60.0, "intelligenceIndexTimePerTask": 600.0,
        "intelligenceIndexOutputTokensPerTask": {"reasoning": 4000.0, "answer": 1000.0, "output": 5000.0},
        "intelligenceIndexCostPerTask": {
            "cost": {"total": 1.5, "nonCacheInput": 0.1, "cacheWrite": 0.2, "cacheRead": 0.3,
                     "answer": 0.4, "reasoning": 0.5},
            "evaluations": [{"slug": "terminalbench-4-0", "weightedCostPerTask": 0.25}],
        },
        "intelligenceIndexEvaluations": [
            {"slug": "terminalbench-4-0", "score": 0.5, "costPerTask": 2.0, "timePerTask": 900.0, "outputTokensPerTask": 9000.0},
            {"slug": "brand-new-eval", "score": 0.7, "costPerTask": 0.2, "timePerTask": 30.0, "outputTokensPerTask": 500.0},
        ],
        "terminalBench40": 0.5, "gpqa": None,
    }
    record.update(overrides)
    return record


def agent_record(identifier: str, model: str, cost: float | None = 3.0) -> dict:
    # The 2026-11 shape: no hostModelSlug.
    return {
        "id": identifier, "agentName": "Claude Code", "provider": "anthropic",
        "display": {"agent": "Claude Code", "model": model}, "displayLabel": f"Claude Code - {model}",
        "evals": [{"datasetIndexName": "terminal-bench-v4", "evaluationDatasetSlug": "Terminal-Bench v4",
                   "mean": {"reward": 0.6, "outputTokens": 100}}],
        "indexScore": 0.6, "mean": {"costUsd": cost, "agentWallTimeSec": 1200.0, "totalTokens": 1e6},
        "modelCreators": [{"name": "Anthropic"}], "percentiles": {}, "versions": {},
    }


class EvaluationSchemaTest(unittest.TestCase):
    def test_a_missing_reference_model_falls_back_to_every_model(self) -> None:
        records = {"a": model_record("a", gpqa=0.8, terminalBench40=None), "b": model_record("b")}
        fields = aa.evaluation_schema(records, "gone-from-the-site")
        self.assertIn("gpqa", fields)
        self.assertIn("terminalBench40", fields)

    def test_a_present_reference_narrows_the_fields(self) -> None:
        records = {"a": model_record("a", gpqa=0.8, terminalBench40=None), "b": model_record("b")}
        self.assertEqual(set(aa.evaluation_schema(records, "b")), {"terminalBench40"})


class IndexEvaluationsTest(unittest.TestCase):
    def test_each_evaluation_carries_its_own_resources_and_weight(self) -> None:
        rows = {row["slug"]: row for row in aa.index_evaluations(model_record("a"))}
        self.assertEqual(rows["terminalbench-4-0"]["cost_per_task"], 2.0)
        self.assertEqual(rows["terminalbench-4-0"]["time_per_task"], 900.0)
        self.assertEqual(rows["terminalbench-4-0"]["weighted_cost_per_task"], 0.25)
        self.assertEqual(rows["terminalbench-4-0"]["label"], "Terminal-Bench 4.0")
        # An evaluation the scraper has never heard of is still collected.
        self.assertEqual(rows["brand-new-eval"]["label"], "Brand New Eval")


class ModelDetailsTest(unittest.TestCase):
    def test_details_need_no_coding_page_and_read_one_page_per_unseen_model(self) -> None:
        pages = {
            # The first page also carries model c, as a chart default.
            f"{aa.BASE}/models/a": flight_page(model_record("a"), model_record("c")),
            f"{aa.BASE}/models/b": flight_page(model_record("b")),
        }
        fetched: list[str] = []

        def fetcher(url: str) -> str:
            fetched.append(url)
            return pages[url]

        result = aa.extract_models(["a", "b", "c"], verbose=True, workers=1, fetcher=fetcher)
        models = {model["slug"]: model for model in result["data"]["models"]}
        self.assertEqual(sorted(models), ["a", "b", "c"])
        self.assertNotIn(f"{aa.BASE}/models/c", fetched)
        self.assertIsNone(models["a"]["coding_index"])
        self.assertEqual(models["a"]["cost_per_task"], 1.5)
        self.assertEqual(models["a"]["time_per_task"], 600.0)
        self.assertEqual(len(models["a"]["index_evaluations"]), 2)

    def test_a_model_without_a_record_is_reported_not_invented(self) -> None:
        with self.assertRaises(aa.ExtractionError):
            aa.extract_models(["a", "missing"], workers=1, fetcher=lambda url: flight_page(model_record("a")))


class CatalogueTest(unittest.TestCase):
    def test_leaderboard_rows_give_the_catalogue(self) -> None:
        # Leaderboard rows: no "id", and the cost per task as a plain number.
        rows = [
            {"slug": "new-model", "name": "New", "shortName": "New", "intelligenceIndex": 70.0,
             "intelligenceIndexCostPerTask": 1.2, "deprecated": False, "modelCreatorName": "Lab"},
            {"slug": "unpriced", "name": "Unpriced", "shortName": "Unpriced", "intelligenceIndex": 40.0,
             "intelligenceIndexCostPerTask": None, "deprecated": False},
        ]
        page = flight_page(*rows)
        everything = aa.build_model_catalog(eligibility="all", verbose=True, page=page)["data"]["models"]
        self.assertEqual({item["slug"] for item in everything}, {"new-model", "unpriced"})
        full = aa.build_model_catalog(eligibility="full", page=page)["data"]["models"]
        self.assertEqual([item["slug"] for item in full], ["new-model"])

    def test_rows_that_name_only_their_release_take_the_slug_of_the_record_with_their_id(self) -> None:
        # The 2026-11-14 shape: each row has an "id" and its release, but no slug of its own.
        def row(identifier: str, name: str) -> dict:
            return {"id": identifier, "release": {"slug": "lab-1", "name": "Lab 1"}, "name": name,
                    "shortName": name, "intelligenceIndex": 60.0, "intelligenceIndexCostPerTask": 2.0,
                    "deprecated": False, "modelCreatorName": "Lab"}
        page = flight_page(row("id-lab-1", "Lab 1 (Max)"), row("id-lab-1-high", "Lab 1 (High)"),
                           row("id-gone", "Lab 1 (Gone)"))
        fetched: list[str] = []

        def fetcher(url: str) -> str:
            fetched.append(url)
            return flight_page(model_record("lab-1"), model_record("lab-1-high"))

        data = aa.build_model_catalog(eligibility="all", verbose=True, page=page, fetcher=fetcher)["data"]
        self.assertEqual(fetched, ["https://artificialanalysis.ai/models/lab-1"])
        self.assertEqual({item["slug"] for item in data["models"]}, {"lab-1", "lab-1-high"})
        self.assertEqual({item["release_date"] for item in data["models"]}, {"2026-10-01"})
        self.assertEqual(data["unresolved"], ["Lab 1 (Gone)"])

    def test_an_empty_page_is_a_layout_change_not_an_empty_catalogue(self) -> None:
        with self.assertRaises(aa.ExtractionError):
            aa.build_model_catalog(page="<html></html>")


class CodingAgentsTest(unittest.TestCase):
    def test_records_without_a_host_model_slug_are_kept_and_named(self) -> None:
        page = flight_page(agent_record("x1", "Claude Sonnet 5.5 (Max, Default Fallback)"),
                           agent_record("x2", "Claude Opus 5.5", cost=None))
        agents = aa.extract_coding_agents(verbose=True, page=page)["data"]["coding_agents"]
        self.assertEqual(len(agents), 2)
        by_id = {agent["id"]: agent for agent in agents}
        self.assertEqual(by_id["x1"]["host_model_slug"], "claude-sonnet-5-5")
        self.assertEqual(by_id["x1"]["model_creators"], ["Anthropic"])
        self.assertEqual(by_id["x1"]["benchmark_scores"]["terminal-bench-v4"]["score"], 0.6)
        # An unpriced configuration keeps a null cost rather than being dropped.
        self.assertIsNone(by_id["x2"]["cost_per_task"])


if __name__ == "__main__":
    unittest.main()
