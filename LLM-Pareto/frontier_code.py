#!/usr/bin/env python3
"""Scrape and normalize Cognition's FrontierCode leaderboard.

Uses only the Python standard library.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterable


PAGE_URL = "https://cognition.com/frontiercode"
DATA_URL = "https://cognition.com/data/frontiercode-leaderboard/data.json"
SCHEMA_VERSION = 1
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)
VERSIONS = {
    "1.1": ("v1_1", "FrontierCode 1.1"),
    "1.0": ("v1", "FrontierCode 1.0"),
}
ROW_FIELDS = (
    "correct",
    "new_score",
    "tokens",
    "cost",
    "tool_calls",
    "steps",
    "ote",
)


class ExtractionError(RuntimeError):
    """Raised when the source cannot be fetched or has an unexpected schema."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def fetch_json(url: str = DATA_URL, timeout: float = 30.0) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            payload = json.loads(response.read().decode(charset))
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExtractionError(f"Failed to load {url}: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractionError(f"{url} did not return valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ExtractionError("FrontierCode data must be a JSON object")
    return payload


def parse_models(values: Iterable[str]) -> list[str]:
    models: list[str] = []
    for value in values:
        models.extend(part.strip() for part in value.split(",") if part.strip())
    return list(dict.fromkeys(models))


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ExtractionError(f"{path} must be an object")
    return value


def _string_list(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ExtractionError(f"{path} must be an array of strings")
    if len(value) != len(set(value)):
        raise ExtractionError(f"{path} contains duplicate values")
    return value


def _number(
    value: Any,
    path: str,
    *,
    optional: bool = False,
    minimum: float | None = None,
    maximum: float | None = None,
) -> int | float | None:
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        qualifier = "a number or null" if optional else "a number"
        raise ExtractionError(f"{path} must be {qualifier}")
    if not math.isfinite(value):
        raise ExtractionError(f"{path} must be finite")
    if minimum is not None and value < minimum:
        raise ExtractionError(f"{path} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ExtractionError(f"{path} must be at most {maximum}")
    return value


def _validate_row(value: Any, path: str) -> dict[str, Any]:
    row = _object(value, path)
    missing = set(ROW_FIELDS) - row.keys()
    if missing:
        raise ExtractionError(f"{path} is missing fields: {', '.join(sorted(missing))}")
    _number(row["correct"], f"{path}.correct", minimum=0, maximum=1)
    _number(row["new_score"], f"{path}.new_score", minimum=0, maximum=1)
    _number(row["tokens"], f"{path}.tokens", minimum=0)
    _number(row["cost"], f"{path}.cost", minimum=0)
    for field in ("tool_calls", "steps", "ote"):
        _number(row[field], f"{path}.{field}", optional=True, minimum=0)
    for field in ("score_preflag", "shortcut_rate_1_0"):
        if field in row:
            _number(row[field], f"{path}.{field}", optional=True, minimum=0, maximum=1)
    if "duration_min" in row:
        _number(row["duration_min"], f"{path}.duration_min", optional=True, minimum=0)
    return row


def validate_revision(document: dict[str, Any], version: str) -> dict[str, Any]:
    try:
        source_key, _ = VERSIONS[version]
    except KeyError as exc:
        raise ExtractionError(f"Unsupported FrontierCode version {version!r}") from exc
    if source_key not in document:
        raise ExtractionError(f"Source data does not contain version {source_key!r}")
    revision = _object(document[source_key], source_key)
    required = {"models", "colors", "lab_colors", "harness", "efforts", "subsets", "data"}
    missing = required - revision.keys()
    if missing:
        raise ExtractionError(
            f"{source_key} is missing fields: {', '.join(sorted(missing))}"
        )

    models = _string_list(revision["models"], f"{source_key}.models")
    colors = _object(revision["colors"], f"{source_key}.colors")
    _object(revision["lab_colors"], f"{source_key}.lab_colors")
    harness = _object(revision["harness"], f"{source_key}.harness")
    efforts = _object(revision["efforts"], f"{source_key}.efforts")
    subsets = _object(revision["subsets"], f"{source_key}.subsets")
    data = _object(revision["data"], f"{source_key}.data")

    for subset, task_count in subsets.items():
        if not isinstance(subset, str):
            raise ExtractionError(f"{source_key}.subsets keys must be strings")
        if isinstance(task_count, bool) or not isinstance(task_count, int) or task_count <= 0:
            raise ExtractionError(
                f"{source_key}.subsets.{subset} must be a positive integer"
            )

    for model in models:
        path = f"{source_key}.{model}"
        if not isinstance(colors.get(model), str):
            raise ExtractionError(f"{path} is missing a color")
        if not isinstance(harness.get(model), str):
            raise ExtractionError(f"{path} is missing a harness")
        model_efforts = _string_list(efforts.get(model), f"{path}.efforts")
        model_data = _object(data.get(model), f"{path}.data")
        if set(model_efforts) != set(model_data):
            raise ExtractionError(f"{path} effort metadata and data do not match")
        for effort in model_efforts:
            effort_data = _object(model_data[effort], f"{path}.data.{effort}")
            for subset in subsets:
                if subset not in effort_data:
                    raise ExtractionError(
                        f"{path}.data.{effort} is missing subset {subset!r}"
                    )
                _validate_row(
                    effort_data[subset],
                    f"{path}.data.{effort}.{subset}",
                )
    return revision


def _normalize_row(
    revision: dict[str, Any],
    model: str,
    effort: str,
    subset: str,
    *,
    include_source: bool,
) -> dict[str, Any]:
    source = revision["data"][model][effort][subset]
    row = {
        "model": model,
        "harness": revision["harness"][model],
        "reasoning_effort": effort,
        "score_percent": round(100 * source["new_score"], 10),
        "pass_rate_percent": round(100 * source["correct"], 10),
        "cost_per_rollout_usd": source["cost"],
        "output_tokens_per_rollout": source["tokens"],
        "tool_calls_per_rollout": source["tool_calls"],
        "agent_steps_per_rollout": source["steps"],
        "output_token_equivalent_per_rollout": source["ote"],
    }
    if "score_preflag" in source:
        value = source["score_preflag"]
        row["score_preflag_percent"] = (
            round(100 * value, 10) if value is not None else None
        )
    if "shortcut_rate_1_0" in source:
        value = source["shortcut_rate_1_0"]
        row["flag_rate_percent"] = (
            round(100 * value, 10) if value is not None else None
        )
    if "duration_min" in source:
        row["duration_minutes_per_rollout"] = source["duration_min"]
    if include_source:
        row["source_record"] = source
    return row


def extract_leaderboard(
    document: dict[str, Any],
    *,
    version: str = "1.1",
    subset: str = "main",
    models: list[str] | None = None,
    all_efforts: bool = False,
    include_source: bool = False,
    collected_at: str | None = None,
) -> dict[str, Any]:
    revision = validate_revision(document, version)
    _, version_label = VERSIONS[version]
    if subset not in revision["subsets"]:
        choices = ", ".join(sorted(revision["subsets"]))
        raise ExtractionError(f"Unknown subset {subset!r}; choose one of: {choices}")

    selected_models = models or list(revision["models"])
    unknown = [model for model in selected_models if model not in revision["models"]]
    if unknown:
        raise ExtractionError("Unknown model(s): " + ", ".join(unknown))

    rows: list[dict[str, Any]] = []
    for model in selected_models:
        efforts = revision["efforts"][model]
        if all_efforts:
            selected_efforts = efforts
        else:
            selected_efforts = [
                max(
                    efforts,
                    key=lambda effort: revision["data"][model][effort][subset][
                        "new_score"
                    ],
                )
            ]
        rows.extend(
            _normalize_row(
                revision,
                model,
                effort,
                subset,
                include_source=include_source,
            )
            for effort in selected_efforts
        )

    rows.sort(
        key=lambda row: (
            -row["score_percent"],
            row["model"].casefold(),
            row["reasoning_effort"],
        )
    )
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank

    return {
        "schema_version": SCHEMA_VERSION,
        "status": "success",
        "entity_type": "frontier_code_leaderboard",
        "collected_at": collected_at or utc_now(),
        "data": {
            "version": version,
            "version_label": version_label,
            "subset": subset,
            "task_count": revision["subsets"][subset],
            "selection": "all_reasoning_efforts" if all_efforts else "best_reasoning_effort",
            "results": rows,
            "count": len(rows),
            "model_count": len(selected_models),
            "sources": {
                "leaderboard": PAGE_URL,
                "data": DATA_URL,
            },
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        choices=tuple(VERSIONS),
        default="1.1",
        help="Leaderboard revision (default: 1.1)",
    )
    parser.add_argument(
        "--subset",
        choices=("main", "extended"),
        default="main",
        help="Benchmark subset (default: main)",
    )
    parser.add_argument(
        "--model",
        action="append",
        default=[],
        help="Exact model name; comma-separated or repeatable",
    )
    parser.add_argument(
        "--all-efforts",
        action="store_true",
        help="Return every reasoning effort instead of each model's best",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include each canonical source record",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    try:
        result = extract_leaderboard(
            fetch_json(timeout=args.timeout),
            version=args.version,
            subset=args.subset,
            models=parse_models(args.model) or None,
            all_efforts=args.all_efforts,
            include_source=args.verbose,
        )
    except ExtractionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=None if args.compact else 2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

