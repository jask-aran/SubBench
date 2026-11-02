#!/usr/bin/env python3
"""Extract task-cost data for an arbitrary set of Artificial Analysis models.

Fetches exactly two comparison pages for the whole set (general and Coding
Index), then joins their embedded JSON datasets by canonical model slug.
Requires artificial_analysis_single_model.py in the same directory.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse

import artificial_analysis_single_model as core

EVALUATION_REFERENCE_MODEL = "gpt-5-6-sol"


def extract_models(
    slugs: list[str],
    timeout: float = 30.0,
    evaluation_reference_model: str = EVALUATION_REFERENCE_MODEL,
) -> dict:
    slugs = list(dict.fromkeys(slugs))
    if not slugs:
        raise core.ExtractionError("At least one model slug is required")
    for slug in slugs:
        if not core.re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise core.ExtractionError(f"Invalid model slug: {slug!r}")

    encoded_models = urllib.parse.quote(",".join(slugs), safe="")
    anchor = slugs[0]
    general_url = (
        f"{core.BASE}/models/{anchor}"
        f"?cost=intelligence-vs-cost-per-task&models={encoded_models}"
    )
    coding_url = (
        f"{core.BASE}/models/capabilities/coding"
        f"?cost-per-task=index-vs-cost-per-task&models={encoded_models}"
    )

    general_page = core.fetch(general_url, timeout)
    coding_page = core.fetch(coding_url, timeout)
    evaluation_fields = core.evaluation_schema(general_page, evaluation_reference_model)
    models = []
    errors = []
    for slug in slugs:
        try:
            result = core.extract_single_model(
                slug,
                timeout,
                general_page=general_page,
                coding_page=coding_page,
                evaluation_fields=evaluation_fields,
                evaluation_reference_model=evaluation_reference_model,
            )
            models.append(result["data"]["model"])
        except core.ExtractionError as exc:
            errors.append({"slug": slug, "error": str(exc)})

    if errors:
        missing = ", ".join(item["slug"] for item in errors)
        raise core.ExtractionError(
            f"The comparison pages did not provide complete data for: {missing}. "
            f"Details: {json.dumps(errors, ensure_ascii=False)}"
        )

    return {
        "status": "success",
        "data": {
            "models": models,
            "requested_count": len(slugs),
            "returned_count": len(models),
            "sources": {"general": general_url, "coding": coding_url},
            "evaluation_reference_model": evaluation_reference_model,
            "evaluation_fields": list(evaluation_fields.values()),
        },
    }


def parse_slugs(values: list[str]) -> list[str]:
    slugs: list[str] = []
    for value in values:
        slugs.extend(part.strip() for part in value.split(",") if part.strip())
    return slugs


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "slugs",
        nargs="+",
        help="Model slugs separated by spaces and/or commas",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--evaluation-reference-model",
        default=EVALUATION_REFERENCE_MODEL,
        help="Model whose reported evaluations define the benchmark field set",
    )
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    args = parser.parse_args()
    try:
        result = extract_models(
            parse_slugs(args.slugs),
            args.timeout,
            args.evaluation_reference_model,
        )
    except core.ExtractionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=None if args.compact else 2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
