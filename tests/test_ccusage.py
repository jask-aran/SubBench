from unittest import TestCase

from subbench.ccusage import CcusageSchemaError, normalise_payload


class CcusageNormaliserTests(TestCase):
    def test_codex_model_breakdown(self) -> None:
        payload = {
            "daily": [
                {
                    "date": "2026-07-27",
                    "inputTokens": 150,
                    "cachedInputTokens": 100,
                    "outputTokens": 20,
                    "modelBreakdowns": [
                        {
                            "model": "gpt-test",
                            "inputTokens": 150,
                            "cachedInputTokens": 100,
                            "outputTokens": 20,
                            "reasoningOutputTokens": 12,
                            "costUSD": 0.01,
                        }
                    ],
                }
            ]
        }

        rows = normalise_payload(payload, provider="codex", report="daily")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].model, "gpt-test")
        self.assertEqual(rows[0].cached_input_tokens, 100)
        self.assertEqual(rows[0].reasoning_output_tokens, 12)
        self.assertEqual(rows[0].reported_cost_usd, "0.01")

    def test_aggregate_cost_is_not_inherited_by_model_breakdowns(self) -> None:
        payload = {
            "daily": [
                {
                    "date": "2026-07-27",
                    "inputTokens": 150,
                    "outputTokens": 20,
                    "costUSD": 0.01,
                    "modelBreakdowns": [
                        {"model": "gpt-one", "inputTokens": 100, "outputTokens": 10},
                        {"model": "gpt-two", "inputTokens": 50, "outputTokens": 10},
                    ],
                }
            ]
        }

        rows = normalise_payload(payload, provider="codex", report="daily")
        self.assertEqual(len(rows), 3)
        costs = {row.model: row.reported_cost_usd for row in rows}
        self.assertEqual(costs, {"gpt-one": None, "gpt-two": None, None: "0.01"})

    def test_claude_cache_classes(self) -> None:
        payload = {
            "data": [
                {
                    "date": "2026-07-27",
                    "model": "claude-test",
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 500,
                    "output_tokens": 40,
                }
            ]
        }

        row = normalise_payload(payload, provider="claude", report="daily")[0]
        self.assertEqual(row.input_tokens, 10)
        self.assertEqual(row.cache_write_tokens, 30)
        self.assertEqual(row.cache_read_tokens, 500)
        self.assertEqual(row.output_tokens, 40)

    def test_rejects_impossible_codex_cache_count(self) -> None:
        payload = {"data": [{"inputTokens": 10, "cachedInputTokens": 11}]}
        with self.assertRaises(CcusageSchemaError):
            normalise_payload(payload, provider="codex", report="daily")
