#!/usr/bin/env python3
"""Extract Artificial Analysis task-cost data for one model.

Uses only the Python standard library. The two public pages embed their chart
datasets as JSON in the returned HTML, so no browser automation is required.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Any, Iterable


BASE = "https://artificialanalysis.ai"
EVALUATION_REFERENCE_MODEL = "gpt-5-6-sol"

# Evaluation fields present in Artificial Analysis' canonical model records.
# The output schema is the subset with a non-null value on the reference model.
EVALUATION_FIELDS = {
    "gdpval": "gdpval_aa",
    "gdpvalNormalized": "gdpval_aa_normalized",
    "tau2": "tau2",
    "tauBanking": "tau_banking",
    "terminalbenchHard": "terminal_bench_hard",
    "terminalbenchV21": "terminal_bench_v2_1",
    "scicode": "scicode",
    "hle": "humanitys_last_exam",
    "gpqa": "gpqa_diamond",
    "critpt": "critpt",
    "omniscience": "aa_omniscience",
    "lcr": "aa_long_context_reasoning",
    "ifbench": "ifbench",
    "apexAgents": "apex_agents",
    "itBenchSre": "itbench_sre",
    "mmmuPro": "mmmu_pro",
    "livecodebench": "livecodebench",
    "aime25": "aime_2025",
}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
)


class ExtractionError(RuntimeError):
    pass


class _ScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[tuple[dict[str, str | None], str]] = []
        self._attrs: dict[str, str | None] | None = None
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            self._attrs = dict(attrs)
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._attrs is not None:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._attrs is not None:
            self.scripts.append((self._attrs, "".join(self._parts)))
            self._attrs = None
            self._parts = []


def fetch(url: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except (urllib.error.URLError, TimeoutError) as exc:
        raise ExtractionError(f"Failed to load {url}: {exc}") from exc


def _walk(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _decode_flight_script(text: str) -> str | None:
    prefix = "self.__next_f.push("
    if not text.startswith(prefix) or not text.endswith(")"):
        return None
    try:
        value = json.loads(text[len(prefix) : -1])
    except json.JSONDecodeError:
        return None
    return value[1] if isinstance(value, list) and len(value) > 1 and isinstance(value[1], str) else None


def _objects_around_marker(text: str, marker: str) -> Iterable[dict[str, Any]]:
    """Decode JSON objects enclosing marker occurrences in mixed RSC text."""
    decoder = json.JSONDecoder()
    position = 0
    while True:
        marker_at = text.find(marker, position)
        if marker_at < 0:
            return
        position = marker_at + len(marker)
        start = marker_at
        attempts = 0
        while attempts < 250:
            start = text.rfind("{", 0, start)
            if start < 0:
                break
            attempts += 1
            try:
                value, length = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                continue
            if start + length >= marker_at + len(marker) and isinstance(value, dict):
                yield value
                break


def extract_records(page: str, slug: str) -> list[dict[str, Any]]:
    parser = _ScriptParser()
    parser.feed(page)
    records: list[dict[str, Any]] = []
    markers = (f'"slug":"{slug}"', f'"detailsUrl":"/models/{slug}"')

    for attrs, script in parser.scripts:
        if attrs.get("type") == "application/ld+json":
            try:
                decoded = json.loads(script)
            except json.JSONDecodeError:
                continue
            for item in _walk(decoded):
                if isinstance(item, dict) and (
                    item.get("slug") == slug or item.get("detailsUrl") == f"/models/{slug}"
                ):
                    records.append(item)

        payload = _decode_flight_script(script)
        if payload:
            for marker in markers:
                records.extend(_objects_around_marker(payload, marker))

    if not records:
        raise ExtractionError(f"No embedded data found for model slug {slug!r}")
    return records


def _find(records: Iterable[dict[str, Any]], key: str) -> Any:
    for record in records:
        if key in record and record[key] is not None:
            return record[key]
    return None


def _find_record(records: Iterable[dict[str, Any]], *keys: str) -> dict[str, Any] | None:
    for record in records:
        if all(key in record for key in keys):
            return record
    return None


def _required(value: Any, field: str) -> Any:
    if value is None:
        raise ExtractionError(f"Required field {field!r} was not present in the page data")
    return value


def _canonical_model(records: Iterable[dict[str, Any]], slug: str) -> dict[str, Any] | None:
    for record in records:
        if record.get("slug") == slug and "intelligenceIndexCostPerTask" in record:
            return record
    return None


def evaluation_schema(page: str, reference_slug: str = EVALUATION_REFERENCE_MODEL) -> dict[str, str]:
    reference = _canonical_model(extract_records(page, reference_slug), reference_slug)
    if reference is None:
        raise ExtractionError(f"Evaluation reference model {reference_slug!r} was not present")
    return {
        source: output
        for source, output in EVALUATION_FIELDS.items()
        if reference.get(source) is not None
    }


def extract_single_model(
    slug: str,
    timeout: float = 30.0,
    *,
    general_page: str | None = None,
    coding_page: str | None = None,
    evaluation_fields: dict[str, str] | None = None,
    evaluation_reference_model: str = EVALUATION_REFERENCE_MODEL,
) -> dict[str, Any]:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ExtractionError(f"Invalid model slug: {slug!r}")

    general_url = f"{BASE}/models/{slug}"
    coding_url = (
        f"{BASE}/models/capabilities/coding"
        f"?cost-per-task=index-vs-cost-per-task&models={slug}"
    )
    general_html = general_page or fetch(general_url, timeout)
    coding_html = coding_page or fetch(coding_url, timeout)
    general = extract_records(general_html, slug)
    coding = extract_records(coding_html, slug)
    if evaluation_fields is None:
        evaluation_fields = evaluation_schema(general_html, evaluation_reference_model)

    general_cost = _find_record(general, "answer", "reasoning", "cacheWrite", "cacheHit", "input")
    general_tokens = _find_record(general, "answer", "reasoning")
    general_time_minutes = _find(general, "timePerTask")

    # The larger RSC model record contains latency and exact aggregate fields.
    general_model = _canonical_model(general, slug)
    if general_model:
        canonical_cost = (general_model.get("intelligenceIndexCostPerTask") or {}).get("cost")
        canonical_tokens = general_model.get("intelligenceIndexOutputTokensPerTask")
        if canonical_cost:
            general_cost = {
                "input": canonical_cost.get("nonCacheInput"),
                "answer": canonical_cost.get("answer"),
                "reasoning": canonical_cost.get("reasoning"),
                "cacheWrite": canonical_cost.get("cacheWrite"),
                "cacheHit": canonical_cost.get("cacheRead"),
            }
        if canonical_tokens:
            general_tokens = canonical_tokens
        if general_model.get("intelligenceIndexTimePerTask") is not None:
            general_time_minutes = general_model["intelligenceIndexTimePerTask"] / 60

    coding_model = _find_record(coding, "slug", "headlineValue", "costPerTask", "timePerTaskSeconds")
    if coding_model is None:
        raise ExtractionError("Coding dataset record was not present")
    coding_cost = _required(coding_model.get("costPerTask"), "coding costPerTask")
    coding_tokens = coding_model.get("outputTokensPerTask") or {}
    coding_total_cost = coding_model.get("evalCost") or {}

    if general_cost is None or general_tokens is None:
        raise ExtractionError("General cost or token breakdown was not present")

    input_cost = general_cost["input"] + general_cost["cacheWrite"] + general_cost["cacheHit"]
    coding_input_cost = _required(coding_cost.get("input"), "coding input")

    # Prefer the canonical model record where available, while retaining the
    # small JSON-LD chart records as stable fallbacks.
    name = _find(general, "name") or _find(general, "label")
    short_name = _find(general, "shortName") or name
    intelligence_index = _find(general, "intelligenceIndex")
    if intelligence_index is None:
        intelligence_index = _find(general, "artificialAnalysisIntelligenceIndex")

    total_cost = _find(general, "costPerIntelligenceIndexTask")
    if total_cost is None and general_model:
        total_cost = ((general_model.get("intelligenceIndexCostPerTask") or {}).get("cost") or {}).get("total")
    time_seconds = float(_required(general_time_minutes, "timePerTask")) * 60

    e2e = None
    ttft = None
    first_token = None
    input_tokens = None
    if general_model:
        e2e_data = general_model.get("endToEndResponseTime") or {}
        ttft_data = general_model.get("timeToFirstAnswerToken") or {}
        e2e = e2e_data.get("total")
        ttft = ttft_data.get("total")
        first_token = ttft_data.get("input")
        input_tokens = general_model.get("inputTokensPerTask")
        if input_tokens is None:
            canonical = general_model.get("canonicalIntelligenceIndexTokenCount") or {}
            per_task = general_model.get("intelligenceIndexOutputTokensPerTask") or {}
            total_output = canonical.get("output")
            output_per_task = per_task.get("output")
            total_input = canonical.get("input")
            if total_output and output_per_task and total_input is not None:
                # Both canonical totals use the same weighted task denominator.
                weighted_task_count = total_output / output_per_task
                input_tokens = total_input / weighted_task_count

    model = {
        "slug": slug,
        "name": _required(name, "name"),
        "short_name": _required(short_name, "short_name"),
        "cost_per_task": _required(total_cost, "cost_per_task"),
        "input_tokens_per_task": round(input_tokens, 2) if input_tokens is not None else None,
        "output_tokens_per_task": round(general_tokens["answer"], 2),
        "reasoning_tokens_per_task": round(general_tokens["reasoning"], 2),
        "intelligence_index": _required(intelligence_index, "intelligence_index"),
        "time_per_task": time_seconds,
        "output_speed_tokens_per_second": (
            (general_model.get("outputSpeedVariance") or {}).get("median") if general_model else None
        ),
        "end_to_end_response_time": e2e,
        "end_to_end_response_time_breakdown": (
            general_model.get("endToEndResponseTime") if general_model else None
        ),
        "time_to_first_token": first_token,
        "time_to_first_token_variance": (
            general_model.get("timeToFirstChunkVariance") if general_model else None
        ),
        "time_to_first_answer_token": ttft,
        "time_to_first_answer_token_breakdown": (
            general_model.get("timeToFirstAnswerToken") if general_model else None
        ),
        "ttft": ttft,
        "output_tokens_per_task_breakdown": {
            "total": (general_model.get("intelligenceIndexOutputTokensPerTask") or {}).get("output"),
            "answer": general_tokens["answer"],
            "reasoning": general_tokens["reasoning"],
        },
        "cost_per_task_breakdown": {
            "input_tokens": input_cost,
            "output_tokens": general_cost["answer"],
            "reasoning_tokens": general_cost["reasoning"],
            "cache_write": general_cost["cacheWrite"],
            "cache_hit": general_cost["cacheHit"],
        },
        "intelligence_index_total_cost_breakdown": (
            general_model.get("intelligenceIndexCost") if general_model else None
        ),
        "intelligence_evaluations": {
            output: general_model.get(source) if general_model else None
            for source, output in evaluation_fields.items()
        },
        "evaluation_reference_model": evaluation_reference_model,
        "coding_index": _required(coding_model.get("headlineValue"), "coding_index"),
        "coding_cost_per_task": _required(coding_cost.get("total"), "coding_cost_per_task"),
        "coding_cost_per_task_breakdown": {
            "input_tokens": coding_input_cost,
            "output_tokens": _required(coding_cost.get("answer"), "coding answer cost"),
            "reasoning_tokens": _required(coding_cost.get("reasoning"), "coding reasoning cost"),
            "cache_write": _required(coding_cost.get("cacheWrite"), "coding cache write cost"),
            "cache_hit": _required(coding_cost.get("cacheRead"), "coding cache hit cost"),
        },
        "coding_time_per_task": _required(coding_model.get("timePerTaskSeconds"), "coding_time_per_task"),
        "coding_output_tokens_per_task": {
            "total": coding_tokens.get("output"),
            "answer": coding_tokens.get("answer"),
            "reasoning": coding_tokens.get("reasoning"),
        },
        "coding_index_total_cost_breakdown": coding_total_cost,
        "model_metadata": {
            "release_date": general_model.get("releaseDate") if general_model else None,
            "creator": general_model.get("creator") if general_model else None,
            "is_reasoning": general_model.get("isReasoning") if general_model else None,
            "is_open_weights": general_model.get("isOpenWeights") if general_model else None,
            "deprecated": general_model.get("deprecated") if general_model else None,
            "context_window_tokens": general_model.get("contextWindowTokens") if general_model else None,
            "input_price_per_million_tokens": general_model.get("price1mInputTokens") if general_model else None,
            "output_price_per_million_tokens": general_model.get("price1mOutputTokens") if general_model else None,
            "cache_hit_price_per_million_tokens": general_model.get("cacheHitPrice") if general_model else None,
            "cache_write_price_per_million_tokens": general_model.get("cacheWritePrice") if general_model else None,
        },
    }
    return {"status": "success", "data": {"model": model}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug", help="Artificial Analysis model slug")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    args = parser.parse_args()
    try:
        result = extract_single_model(args.slug, args.timeout)
    except ExtractionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=None if args.compact else 2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
