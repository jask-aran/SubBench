#!/usr/bin/env python3
"""Build the compact, static data bundle consumed by the GitHub Pages app."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DATA_DIR = Path("data")
DATABASE_NAME = "archive.sqlite3"
DEEPSWE_PATH = Path("leaderboard.json")
FRONTIERCODE_PATH = Path("frontiercode-data.json")
OUTPUT_PATH = Path("frontier-data.json")

BENCHMARKS = {
    "aa_long_context_reasoning": (
        "aa-long-context",
        "AA Long Context Reasoning",
        "percent",
        "Long-context reasoning evaluation.",
    ),
    "aa_omniscience": (
        "aa-omniscience",
        "AA Omniscience",
        "score100",
        "Knowledge accuracy with hallucination penalties.",
    ),
    "critpt": ("critpt", "CritPt", "percent", "Critical reasoning evaluation."),
    "gdpval_aa_normalized": (
        "gdpval-aa",
        "GDPval-AA",
        "percent",
        "Real-world economically valuable tasks, normalized.",
    ),
    "gpqa_diamond": (
        "gpqa-diamond",
        "GPQA Diamond",
        "percent",
        "Graduate-level science questions.",
    ),
    "humanitys_last_exam": (
        "humanitys-last-exam",
        "Humanity's Last Exam",
        "percent",
        "Broad expert-level academic questions.",
    ),
    "ifbench": (
        "ifbench",
        "IFBench",
        "percent",
        "Instruction-following reliability.",
    ),
    "itbench_sre": (
        "itbench-sre",
        "ITBench SRE",
        "percent",
        "Site-reliability engineering tasks.",
    ),
    "mmmu_pro": (
        "mmmu-pro",
        "MMMU-Pro",
        "percent",
        "Expert-level multimodal reasoning.",
    ),
    "scicode": (
        "scicode",
        "SciCode",
        "percent",
        "Scientific research coding.",
    ),
    "tau2": (
        "tau2",
        "τ²-Bench",
        "percent",
        "Tool-agent interaction tasks.",
    ),
    "tau_banking": (
        "tau-banking",
        "τ-Bench Banking",
        "percent",
        "Tool use in a banking environment.",
    ),
    "terminal_bench_hard": (
        "terminal-bench-hard",
        "Terminal-Bench Hard",
        "percent",
        "Hard terminal-based agent tasks.",
    ),
    "terminal_bench_v2_1": (
        "terminal-bench-v2-1",
        "Terminal-Bench 2.1",
        "percent",
        "Terminal-based agent tasks.",
    ),
}

COST_SLUGS = {
    "artificial-analysis-long-context-reasoning": "aa-long-context",
    "omniscience": "aa-omniscience",
    "critpt": "critpt",
    "gdpval-aa": "gdpval-aa",
    "gpqa-diamond": "gpqa-diamond",
    "humanitys-last-exam": "humanitys-last-exam",
    "scicode": "scicode",
    "tau3-banking": "tau-banking",
    "terminalbench-v2-1": "terminal-bench-v2-1",
}

FAMILY_PREFIXES = (
    ("claude", "Anthropic"),
    ("gpt", "OpenAI"),
    ("o1", "OpenAI"),
    ("o3", "OpenAI"),
    ("o4", "OpenAI"),
    ("gemini", "Google"),
    ("gemma", "Google"),
    ("grok", "xAI"),
    ("kimi", "Moonshot AI"),
    ("glm", "Zhipu AI"),
    ("qwen", "Alibaba"),
    ("deepseek", "DeepSeek"),
    ("mistral", "Mistral"),
    ("ministral", "Mistral"),
    ("magistral", "Mistral"),
    ("llama", "Meta"),
    ("muse", "Meta"),
    ("nova", "Amazon"),
    ("command", "Cohere"),
    ("nemotron", "NVIDIA"),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def infer_family(slug: str, provider: str | None = None) -> str:
    if provider:
        return provider
    lowered = slug.lower()
    for prefix, family in FAMILY_PREFIXES:
        if lowered.startswith(prefix):
            return family
    return "Other"


def latest_manifest(connection: sqlite3.Connection) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT payload_json FROM snapshots "
        "WHERE entity_type = 'frontier_collection' "
        "ORDER BY collected_at DESC, rowid DESC"
    )
    for (payload_json,) in rows:
        manifest = json.loads(payload_json)
        if manifest.get("completed"):
            return manifest
    raise SystemExit("No completed frontier collection found; run `uv run archive.py collect`")


def snapshots(
    connection: sqlite3.Connection,
    run_ids: list[str],
    entity_type: str,
) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        f"SELECT payload_json FROM snapshots WHERE entity_type = ? "
        f"AND run_id IN ({placeholders}) ORDER BY entity_id",
        (entity_type, *run_ids),
    )
    return [json.loads(row[0]) for row in rows]


def scalar(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, dict):
        total = value.get("total")
        if isinstance(total, (int, float)) and not isinstance(total, bool):
            return float(total)
    return None


def compact_resources(model: dict[str, Any], coding: bool = False) -> dict[str, Any]:
    prefix = "coding_" if coding else ""
    resources = {
        "cost": model.get(f"{prefix}cost_per_task"),
        "time": model.get(f"{prefix}time_per_task")
        if coding
        else model.get("time_per_task") or model.get("end_to_end_response_time"),
        "output_tokens": scalar(model.get(f"{prefix}output_tokens_per_task"))
        if coding
        else scalar(model.get("output_tokens_per_task")),
        "input_tokens": None if coding else scalar(model.get("input_tokens_per_task")),
        "reasoning_tokens": None if coding else scalar(model.get("reasoning_tokens_per_task")),
        "ttft": None if coding else model.get("time_to_first_token") or model.get("ttft"),
        "first_answer": None if coding else model.get("time_to_first_answer_token"),
        "output_speed": None if coding else model.get("output_speed_tokens_per_second"),
        "steps": None,
        "weighted_cost": None,
    }
    return {key: value for key, value in resources.items() if value is not None}


def deep_effort_slug(model: str, effort: str) -> str:
    if effort in {"max", "default"}:
        return model
    return f"{model}-{effort}"


def observation(
    entity_type: str,
    entity_id: str,
    metric_id: str,
    score: float,
    resources: dict[str, Any] | None,
    source: str,
    resource_scope: str,
    variant: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "metric_id": metric_id,
        "score": score,
        "source": source,
        "resource_scope": resource_scope,
    }
    if resources:
        result["resources"] = resources
    if variant:
        result["variant"] = variant
    return result


def main() -> None:
    database = DATA_DIR / DATABASE_NAME
    if not database.exists():
        raise SystemExit(f"{database} not found; run `uv run archive.py collect`")
    if not DEEPSWE_PATH.exists():
        raise SystemExit(f"{DEEPSWE_PATH} not found; run `./update.sh`")

    with closing(sqlite3.connect(database)) as connection:
        manifest = latest_manifest(connection)
        runs = manifest["runs"]
        models = snapshots(connection, runs.get("models") or [], "model")
        agents = snapshots(
            connection,
            [runs["coding_agents"]] if runs.get("coding_agents") else [],
            "coding_agent_configuration",
        )

    entities: dict[str, dict[str, dict[str, Any]]] = {"model": {}, "agent": {}}
    observations: list[dict[str, Any]] = []
    metric_counts: defaultdict[str, int] = defaultdict(int)

    metrics: list[dict[str, Any]] = [
        {
            "id": "aa-intelligence",
            "label": "Intelligence Index",
            "short_label": "Intelligence",
            "group": "AA indexes",
            "entity_type": "model",
            "format": "score100",
            "composite": True,
            "description": "Artificial Analysis composite intelligence score.",
        },
        {
            "id": "aa-coding",
            "label": "Coding Index",
            "short_label": "Coding",
            "group": "AA indexes",
            "entity_type": "model",
            "format": "score100",
            "composite": True,
            "description": "Artificial Analysis composite coding score.",
        },
        {
            "id": "deepswe-pass1",
            "label": "DeepSWE Pass@1",
            "short_label": "DeepSWE P@1",
            "group": "DeepSWE",
            "entity_type": "model",
            "format": "percent",
            "composite": False,
            "description": "Single-run success rate on the DeepSWE benchmark.",
        },
        {
            "id": "deepswe-pass4",
            "label": "DeepSWE Pass@4",
            "short_label": "DeepSWE P@4",
            "group": "DeepSWE",
            "entity_type": "model",
            "format": "percent",
            "composite": False,
            "description": "Success rate with up to four DeepSWE attempts.",
        },
        {
            "id": "frontiercode-score",
            "label": "FrontierCode Score",
            "short_label": "FC Score",
            "group": "FrontierCode",
            "entity_type": "model",
            "format": "percent",
            "composite": False,
            "description": "Cognition FrontierCode leaderboard score.",
        },
        {
            "id": "frontiercode-pass-rate",
            "label": "FrontierCode Pass Rate",
            "short_label": "FC Pass Rate",
            "group": "FrontierCode",
            "entity_type": "model",
            "format": "percent",
            "composite": False,
            "description": "Cognition FrontierCode pass rate (correct@1).",
        },
    ]
    for _, (metric_id, label, fmt, description) in BENCHMARKS.items():
        metrics.append(
            {
                "id": metric_id,
                "label": label,
                "short_label": label,
                "group": "AA benchmarks",
                "entity_type": "model",
                "format": fmt,
                "composite": False,
                "description": description,
            }
        )

    for model in models:
        slug = model["slug"]
        metadata = model.get("model_metadata") or {}
        creator = metadata.get("creator") or {}
        provider = creator.get("name")
        entities["model"][slug] = {
            "id": slug,
            "label": model.get("short_name") or model.get("name") or slug,
            "slug": slug,
            "family": infer_family(slug, provider),
            "provider": provider,
            "release_date": metadata.get("release_date"),
            "reasoning": metadata.get("is_reasoning"),
            "open_weights": metadata.get("is_open_weights"),
            "sources": ["Artificial Analysis"],
        }

        general_resources = compact_resources(model)
        coding_resources = compact_resources(model, coding=True)
        if model.get("intelligence_index") is not None:
            observations.append(
                observation(
                    "model",
                    slug,
                    "aa-intelligence",
                    model["intelligence_index"],
                    general_resources,
                    "Artificial Analysis",
                    "matched",
                )
            )
            metric_counts["aa-intelligence"] += 1
        if model.get("coding_index") is not None:
            observations.append(
                observation(
                    "model",
                    slug,
                    "aa-coding",
                    model["coding_index"],
                    coding_resources,
                    "Artificial Analysis",
                    "matched",
                )
            )
            metric_counts["aa-coding"] += 1

        weighted_costs = {
            COST_SLUGS[item["slug"]]: item["weightedCostPerTask"]
            for item in model.get("intelligence_evaluation_cost_contributions") or []
            if item.get("slug") in COST_SLUGS and item.get("weightedCostPerTask") is not None
        }
        for source_key, value in (model.get("intelligence_evaluations") or {}).items():
            if source_key not in BENCHMARKS or value is None:
                continue
            metric_id = BENCHMARKS[source_key][0]
            benchmark_resources = {}
            if metric_id in weighted_costs:
                benchmark_resources["weighted_cost"] = weighted_costs[metric_id]
            observations.append(
                observation(
                    "model",
                    slug,
                    metric_id,
                    value,
                    benchmark_resources,
                    "Artificial Analysis",
                    "evaluation-only",
                )
            )
            metric_counts[metric_id] += 1

    deepswe_rows = json.loads(DEEPSWE_PATH.read_text(encoding="utf-8"))
    for row in deepswe_rows:
        slug = deep_effort_slug(row["model"], row["effort"])
        if slug not in entities["model"]:
            family = infer_family(row["model"])
            entities["model"][slug] = {
                "id": slug,
                "label": f"{row['model']} ({row['effort']})",
                "slug": slug,
                "family": family,
                "provider": None,
                "release_date": None,
                "reasoning": None,
                "open_weights": None,
                "sources": ["DeepSWE"],
            }
        elif "DeepSWE" not in entities["model"][slug]["sources"]:
            entities["model"][slug]["sources"].append("DeepSWE")
        resources = {
            "cost": row.get("cost"),
            "time": row.get("dur"),
            "output_tokens": row.get("outTok"),
            "input_tokens": row.get("inTok"),
            "steps": row.get("steps"),
        }
        resources = {key: value for key, value in resources.items() if value is not None}
        for metric_id, source_key in (
            ("deepswe-pass1", "p1"),
            ("deepswe-pass4", "p4"),
        ):
            if row.get(source_key) is None:
                continue
            observations.append(
                observation(
                    "model",
                    slug,
                    metric_id,
                    row[source_key],
                    resources,
                    "DeepSWE",
                    "matched",
                    row["effort"],
                )
            )
            metric_counts[metric_id] += 1

    if FRONTIERCODE_PATH.exists():
        fc_data = json.loads(FRONTIERCODE_PATH.read_text(encoding="utf-8"))
        if fc_data.get("status") == "success":
            for row in fc_data["data"]["results"]:
                slug = row["model"].replace(" ", "-").lower()
                if slug not in entities["model"]:
                    family = infer_family(row["model"])
                    entities["model"][slug] = {
                        "id": slug,
                        "label": row["model"],
                        "slug": slug,
                        "family": family,
                        "provider": None,
                        "release_date": None,
                        "reasoning": None,
                        "open_weights": None,
                        "sources": ["FrontierCode"],
                    }
                elif "FrontierCode" not in entities["model"][slug]["sources"]:
                    entities["model"][slug]["sources"].append("FrontierCode")
                resources = {
                    "cost": row.get("cost_per_rollout_usd"),
                    "output_tokens": row.get("output_tokens_per_rollout"),
                }
                if row.get("tool_calls_per_rollout") is not None:
                    resources["tool_calls"] = row["tool_calls_per_rollout"]
                if row.get("agent_steps_per_rollout") is not None:
                    resources["steps"] = row["agent_steps_per_rollout"]
                resources = {key: value for key, value in resources.items() if value is not None}
                for metric_id, source_key in (
                    ("frontiercode-score", "score_percent"),
                    ("frontiercode-pass-rate", "pass_rate_percent"),
                ):
                    if row.get(source_key) is None:
                        continue
                    observations.append(
                        observation(
                            "model",
                            slug,
                            metric_id,
                            row[source_key],
                            resources,
                            "FrontierCode",
                            "matched",
                            row["reasoning_effort"],
                        )
                    )
                    metric_counts[metric_id] += 1

    agent_metrics = [
        (
            "agent-index",
            "Coding Agent Index",
            "Composite",
            "Composite score across the Coding Agent Index components.",
        ),
        ("agent-deepswe", "DeepSWE", "Benchmarks", "DeepSWE component score."),
        (
            "agent-terminal-bench-v2",
            "Terminal-Bench v2",
            "Benchmarks",
            "Terminal-Bench v2 component score.",
        ),
        (
            "agent-swe-atlas-qna",
            "SWE-Atlas-QnA",
            "Benchmarks",
            "SWE-Atlas-QnA component score.",
        ),
    ]
    metrics.extend(
        {
            "id": metric_id,
            "label": label,
            "short_label": label,
            "group": group,
            "entity_type": "agent",
            "format": "percent",
            "composite": metric_id == "agent-index",
            "description": description,
        }
        for metric_id, label, group, description in agent_metrics
    )
    agent_benchmark_keys = {
        "agent-deepswe": "deep-swe",
        "agent-terminal-bench-v2": "terminal-bench-v2",
        "agent-swe-atlas-qna": "swe-atlas-qna",
    }
    for agent in agents:
        agent_id = agent["id"]
        display = agent.get("display") or {}
        creator = display.get("creator") or {}
        provider = creator.get("agent") or agent.get("provider")
        entities["agent"][agent_id] = {
            "id": agent_id,
            "label": agent.get("display_label") or agent_id,
            "agent": agent.get("agent_name"),
            "model": display.get("model") or agent.get("host_model_slug"),
            "host_model_slug": agent.get("host_model_slug"),
            "family": agent.get("agent_name") or infer_family(agent.get("host_model_slug") or ""),
            "provider": provider,
            "release_date": None,
            "sources": ["Artificial Analysis"],
        }
        resources = {
            "cost": agent.get("cost_per_task"),
            "time": agent.get("time_per_task_seconds"),
            "output_tokens": agent.get("output_tokens_per_task"),
            "input_tokens": agent.get("input_tokens_per_task"),
            "total_tokens": agent.get("total_tokens_per_task"),
            "steps": agent.get("steps"),
        }
        resources = {key: value for key, value in resources.items() if value is not None}
        if agent.get("index_score") is not None:
            observations.append(
                observation(
                    "agent",
                    agent_id,
                    "agent-index",
                    agent["index_score"],
                    resources,
                    "Artificial Analysis",
                    "matched",
                )
            )
            metric_counts["agent-index"] += 1
        scores = agent.get("benchmark_scores") or {}
        for metric_id, score_key in agent_benchmark_keys.items():
            score = (scores.get(score_key) or {}).get("score")
            if score is None:
                continue
            observations.append(
                observation(
                    "agent",
                    agent_id,
                    metric_id,
                    score,
                    resources,
                    "Artificial Analysis",
                    "configuration-wide",
                )
            )
            metric_counts[metric_id] += 1

    metrics = [
        {**metric, "count": metric_counts.get(metric["id"], 0)}
        for metric in metrics
        if metric_counts.get(metric["id"], 0)
    ]
    payload = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "collection": {
            "id": manifest["collection_id"],
            "started_at": manifest["started_at"],
            "finished_at": manifest["finished_at"],
        },
        "sources": {
            "aa": {
                "label": "Artificial Analysis",
                "collected_at": manifest["finished_at"],
            },
            "deepswe": {
                "label": "DeepSWE Datacurve",
                "updated_at": datetime.fromtimestamp(
                    DEEPSWE_PATH.stat().st_mtime, timezone.utc
                )
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z"),
            },
            **(
                {
                    "frontiercode": {
                        "label": "FrontierCode",
                        "updated_at": datetime.fromtimestamp(
                            FRONTIERCODE_PATH.stat().st_mtime, timezone.utc
                        )
                        .isoformat(timespec="seconds")
                        .replace("+00:00", "Z"),
                    }
                }
                if FRONTIERCODE_PATH.exists()
                else {}
            ),
        },
        "metrics": metrics,
        "entities": {
            entity_type: list(records.values())
            for entity_type, records in entities.items()
        },
        "observations": observations,
        "counts": {
            "models": len(entities["model"]),
            "agents": len(entities["agent"]),
            "metrics": len(metrics),
            "observations": len(observations),
        },
    }
    OUTPUT_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    print(
        f"Wrote {OUTPUT_PATH} with {payload['counts']['models']} models, "
        f"{payload['counts']['agents']} agent configurations, "
        f"{payload['counts']['metrics']} capability lenses, and "
        f"{payload['counts']['observations']} observations."
    )


if __name__ == "__main__":
    main()
