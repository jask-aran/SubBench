#!/usr/bin/env python3
"""Produce archived comparison data for one or more models.

Uses only the Python standard library.

Artificial Analysis publishes no free per-task cost data through its API, so this reads the
data its pages embed for their charts (Next.js flight payloads). Those pages change without
notice. Everything here is therefore written to degrade rather than fail: a source that has
gone away leaves its fields null, and `--check` exercises every mode against the live site
and says exactly what is missing. Run it before trusting a collection.

Site changes this version follows (checked 2026-11):

* The per-model Coding Index is retired; /models/capabilities/coding now redirects to the
  coding-agent leaderboard. Coding is measured per harness-model configuration.
* The Intelligence Index evaluations are listed per model in
  `intelligenceIndexEvaluations`, each with its own score, cost, time and output tokens.
* A model page with `?models=a,b,c` no longer embeds every requested model, so details are
  read one page per model, skipping models an earlier page already carried.
* Coding-agent records no longer carry `hostModelSlug`; the host model is its display name
  and creators.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Iterable


BASE = "https://artificialanalysis.ai"
# None: the evaluation field set is every field reported by any collected model. A named
# reference model narrows it to that model's fields, as earlier versions always did; a
# reference that has disappeared from the site falls back to the union instead of failing.
EVALUATION_REFERENCE_MODEL: str | None = None
CATALOGUE_URL = f"{BASE}/leaderboards/models"
AGENTS_URL = f"{BASE}/agents/coding-agents"

# Labels for the evaluations inside the Intelligence Index, by the slug AA uses. A slug not
# listed here is still collected, under a label made from the slug.
INDEX_EVALUATION_LABELS = {
    "aa-briefcase": "AA Briefcase",
    "automationbench-aa": "AutomationBench",
    "terminalbench-4-0": "Terminal-Bench 4.0",
    "terminal-bench-science": "Terminal-Bench Science",
    "frontiercode-2-0": "FrontierCode 2.0",
    "omniscience": "AA Omniscience",
    "gdp-pdf": "GDP-PDF",
    "hle-diamond": "HLE Diamond",
    "critpt-v2-hard": "CritPt v2 Hard",
}

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
    # Fields of the 2026-11 Intelligence Index.
    "terminalBench40": "terminal_bench_4_0",
    "terminalBenchScience": "terminal_bench_science",
    "frontierCode": "frontiercode",
    "frontierCode20": "frontiercode_2_0",
    "hleDiamond": "hle_diamond",
    "critptAgenticHard": "critpt_agentic_hard",
    "automationBenchPartialScore": "automationbench_partial",
    "healthbench": "healthbench",
}

DEFAULT_MODEL_FIELDS = (
    "collected_at", "slug", "name", "short_name", "intelligence_index",
    "cost_per_task", "cost_per_task_breakdown", "input_tokens_per_task",
    "output_tokens_per_task", "reasoning_tokens_per_task", "time_per_task",
    "output_speed_tokens_per_second", "end_to_end_response_time",
    "time_to_first_answer_token", "coding_index", "coding_cost_per_task",
    "coding_cost_per_task_breakdown", "coding_time_per_task",
    "model_metadata.release_date",
)
DEFAULT_CATALOG_FIELDS = (
    "slug", "short_name", "release_date",
)
DEFAULT_AGENT_FIELDS = (
    "id", "display_label", "agent_name", "host_model_slug", "index_score",
    "benchmark_scores", "cost_per_task", "time_per_task_seconds",
    "output_tokens_per_task", "total_tokens_per_task",
)

FIELD_GROUPS = {
    "identity": ("slug", "name", "short_name", "model_metadata.release_date"),
    "summary": DEFAULT_MODEL_FIELDS,
    "cost": (
        "cost_per_task", "cost_per_task_breakdown",
        "intelligence_index_total_cost_breakdown",
        "intelligence_evaluation_cost_contributions", "coding_cost_per_task",
        "coding_cost_per_task_breakdown", "coding_index_total_cost_breakdown",
    ),
    "tokens": (
        "input_tokens_per_task", "output_tokens_per_task",
        "reasoning_tokens_per_task", "output_tokens_per_task_breakdown",
        "canonical_intelligence_index_token_totals",
        "coding_output_tokens_per_task",
    ),
    "timing": (
        "time_per_task", "output_speed_tokens_per_second",
        "output_speed_variance", "time_to_first_token",
        "time_to_first_token_variance", "time_to_first_answer_token",
        "time_to_first_answer_token_breakdown", "end_to_end_response_time",
        "end_to_end_response_time_breakdown", "performance_by_prompt_type",
        "performance_timeseries",
    ),
    "evaluations": (
        "intelligence_index", "intelligence_evaluations", "benchmark_details",
        "coding_index",
    ),
    "coding": (
        "coding_index", "coding_cost_per_task", "coding_cost_per_task_breakdown",
        "coding_time_per_task", "coding_output_tokens_per_task",
        "coding_index_total_cost_breakdown", "benchmark_details.coding_sub_scores",
        "benchmark_details.coding_weighted_index",
    ),
    "metadata": ("model_metadata", "result_status"),
    "performance": (
        "output_speed_tokens_per_second", "output_speed_variance",
        "performance_by_prompt_type", "performance_timeseries",
        "time_to_first_token", "time_to_first_token_variance",
        "time_to_first_answer_token", "time_to_first_answer_token_breakdown",
        "end_to_end_response_time", "end_to_end_response_time_breakdown",
    ),
    "source": ("source_record",),
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


def extract_embedded_objects(page: str) -> list[dict[str, Any]]:
    """Return JSON objects embedded in structured data and RSC payloads."""
    parser = _ScriptParser()
    parser.feed(page)
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for attrs, script in parser.scripts:
        if attrs.get("type") == "application/ld+json":
            try:
                decoded = json.loads(script)
            except json.JSONDecodeError:
                pass
            else:
                objects.extend(item for item in _walk(decoded) if isinstance(item, dict))
        payload = _decode_flight_script(script)
        if not payload:
            continue
        for match in re.finditer(r'\{\"id\":\"', payload):
            try:
                value, _ = decoder.raw_decode(payload[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                objects.append(value)
        # Leaderboard rows carry no "id" first; find them by a field every model row has.
        if '"intelligenceIndexCostPerTask"' in payload:
            objects.extend(
                value for value in _objects_around_marker(payload, '"intelligenceIndexCostPerTask"')
                if "slug" in value and "id" not in value
            )
    return objects


def _canonical_model_record(record: dict[str, Any]) -> bool:
    required = {
        "id", "slug", "name", "shortName", "releaseDate", "creator",
        "intelligenceIndex", "intelligenceIndexCostPerTask",
    }
    return required.issubset(record)


def _canonical_coding_record(record: dict[str, Any]) -> bool:
    required = {
        "id", "slug", "name", "headlineValue", "costPerTask",
        "timePerTaskSeconds", "outputTokensPerTask",
    }
    return required.issubset(record)


def _canonical_agent_record(record: dict[str, Any]) -> bool:
    # hostModelSlug was dropped from these records in 2026; the host model is now named by
    # display.model and modelCreators, so it is not required.
    required = {
        "id", "agentName", "provider", "display",
        "displayLabel", "evals", "indexScore", "mean",
    }
    return required.issubset(record)


def _leaderboard_model_record(record: dict[str, Any]) -> bool:
    return {"slug", "name", "intelligenceIndex", "intelligenceIndexCostPerTask"}.issubset(record)


def _host_model_slug(record: dict[str, Any]) -> str | None:
    """The host model's slug, given by the record or derived from its display name."""
    if record.get("hostModelSlug"):
        return record["hostModelSlug"]
    model = ((record.get("display") or {}).get("model") or "").strip()
    if not model:
        return None
    # "Claude Sonnet 5.5 (Max, Default Fallback)" -> "claude-sonnet-5-5"
    base = re.sub(r"\s*\(.*\)\s*$", "", model)
    slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-")
    return slug or None


def _deduplicate_typed_records(
    records: Iterable[dict[str, Any]],
    predicate: Any,
    key: str,
) -> dict[str, dict[str, Any]]:
    """Deduplicate one explicit record type; reject conflicting canonical copies."""
    selected: dict[str, dict[str, Any]] = {}
    for record in records:
        if not predicate(record) or not isinstance(record.get(key), str):
            continue
        identity = record[key]
        previous = selected.get(identity)
        if previous is None:
            selected[identity] = record
        elif previous != record:
            previous_non_null = sum(value is not None for value in previous.values())
            current_non_null = sum(value is not None for value in record.values())
            if current_non_null > previous_non_null:
                selected[identity] = record
            elif current_non_null == previous_non_null:
                raise ExtractionError(
                    f"Conflicting canonical representations for {key}={identity!r}"
                )
    return selected


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


def parse_field_selection(values: list[str] | None) -> list[str] | None:
    if not values:
        return None
    requested: list[str] = []
    for value in values:
        requested.extend(item.strip() for item in value.split(",") if item.strip())
    expanded: list[str] = []
    for item in requested:
        expanded.extend(FIELD_GROUPS.get(item, (item,)))
    return list(dict.fromkeys(expanded))


def _lookup_path(record: dict[str, Any], path: str) -> Any:
    current: Any = record
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ExtractionError(f"Unknown output field {path!r}")
        current = current[part]
    return current


def _assign_path(result: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    current = result
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def project_record(record: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for path in fields:
        _assign_path(result, path, _lookup_path(record, path))
    return result


def with_identity_fields(fields: Iterable[str], required: Iterable[str]) -> list[str]:
    """Return a stable projection that cannot omit the entity identity."""
    return list(dict.fromkeys((*required, *fields)))


def _canonical_model(records: Iterable[dict[str, Any]], slug: str) -> dict[str, Any] | None:
    for record in records:
        if record.get("slug") == slug and "intelligenceIndexCostPerTask" in record:
            return record
    return None


def evaluation_schema(
    page_or_records: str | dict[str, dict[str, Any]],
    reference_slug: str | None = EVALUATION_REFERENCE_MODEL,
) -> dict[str, str]:
    """The evaluation fields to report: the reference model's, or every model's.

    A reference model that the site no longer carries used to fail the whole collection; it
    now falls back to the union over the records at hand.
    """
    if isinstance(page_or_records, str):
        records = _deduplicate_typed_records(
            extract_embedded_objects(page_or_records), _canonical_model_record, "slug"
        )
    else:
        records = page_or_records
    reference = records.get(reference_slug) if reference_slug else None
    pool = [reference] if reference else list(records.values())
    return {
        source: output
        for source, output in EVALUATION_FIELDS.items()
        if any(record.get(source) is not None for record in pool)
    }


def index_evaluations(model: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Each evaluation of the Intelligence Index with its own score and resources."""
    if not model:
        return []
    weighted = {
        item.get("slug"): item.get("weightedCostPerTask")
        for item in ((model.get("intelligenceIndexCostPerTask") or {}).get("evaluations") or [])
    }
    rows = []
    for item in model.get("intelligenceIndexEvaluations") or []:
        slug = item.get("slug")
        if not slug:
            continue
        rows.append({
            "slug": slug,
            "label": INDEX_EVALUATION_LABELS.get(slug) or slug.replace("-", " ").title(),
            "score": item.get("score"),
            "cost_per_task": item.get("costPerTask"),
            "time_per_task": item.get("timePerTask"),
            "output_tokens_per_task": item.get("outputTokensPerTask"),
            "weighted_cost_per_task": weighted.get(slug),
        })
    return rows


def collect_model_records(
    slugs: list[str],
    timeout: float = 30.0,
    *,
    workers: int = 4,
    fetcher: Any = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Canonical model records for the slugs, one page per model not yet seen.

    Each model page embeds its own record and a couple of dozen chart defaults, so models
    already carried by an earlier page are not fetched again. Returns the records and the
    page URL each one came from.
    """
    from concurrent.futures import ThreadPoolExecutor

    fetch_page = fetcher or (lambda url: fetch(url, timeout))
    records: dict[str, dict[str, Any]] = {}
    sources: dict[str, str] = {}
    pending = list(dict.fromkeys(slugs))
    while pending:
        wave = pending[:workers]
        pending = pending[workers:]
        urls = {slug: f"{BASE}/models/{slug}" for slug in wave}
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            pages = dict(zip(wave, pool.map(lambda slug: fetch_page(urls[slug]), wave)))
        for slug, page in pages.items():
            found = _deduplicate_typed_records(
                extract_embedded_objects(page), _canonical_model_record, "slug"
            )
            for key, record in found.items():
                previous = records.get(key)
                if previous is None or sum(v is not None for v in record.values()) > sum(
                    v is not None for v in previous.values()
                ):
                    records[key] = record
                    sources[key] = urls[slug]
        pending = [slug for slug in pending if slug not in records]
    return records, sources


def extract_single_model(
    slug: str,
    timeout: float = 30.0,
    *,
    general_page: str | None = None,
    coding_page: str | None = None,
    evaluation_fields: dict[str, str] | None = None,
    evaluation_reference_model: str | None = EVALUATION_REFERENCE_MODEL,
    collected_at: str | None = None,
    general_record: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One model's normalised record.

    general_record: the model's canonical record when the caller already has it (from
    collect_model_records); its page is then not fetched again. coding_page: a page with the
    retired per-model Coding Index, for archived pages only; without it the coding fields
    are null.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        raise ExtractionError(f"Invalid model slug: {slug!r}")

    if general_record is not None:
        general = [general_record]
        general_html = None
    else:
        general_html = general_page or fetch(f"{BASE}/models/{slug}", timeout)
        general = extract_records(general_html, slug)
    coding: list[dict[str, Any]] = []
    if coding_page:
        try:
            coding = extract_records(coding_page, slug)
        except ExtractionError:
            coding = []
    if evaluation_fields is None:
        evaluation_fields = evaluation_schema(
            general_html if general_html is not None else {slug: general_record or {}},
            evaluation_reference_model,
        )

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

    # The per-model Coding Index is retired; an archived coding page still supplies it.
    coding_model = _find_record(coding, "slug", "headlineValue", "costPerTask", "timePerTaskSeconds") or {}
    coding_cost = coding_model.get("costPerTask") or {}
    coding_tokens = coding_model.get("outputTokensPerTask") or {}
    coding_total_cost = coding_model.get("evalCost") or None
    intelligence_cost_per_task = (general_model or {}).get("intelligenceIndexCostPerTask") or {}

    if general_cost is None or general_tokens is None:
        raise ExtractionError("General cost or token breakdown was not present")

    input_cost = sum(general_cost.get(key) or 0 for key in ("input", "cacheWrite", "cacheHit"))

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
    # AA publishes cost/token data before latency data for some newly listed
    # models. Preserve the model with a null latency rather than rejecting the
    # entire collection; the canonical source record remains available below.
    time_seconds = float(general_time_minutes) * 60 if general_time_minutes is not None else None

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
        "collected_at": collected_at or datetime.now(timezone.utc).isoformat(),
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
        "output_speed_variance": general_model.get("outputSpeedVariance") if general_model else None,
        "performance_by_prompt_type": (
            general_model.get("performanceByPromptType") if general_model else None
        ),
        "performance_timeseries": general_model.get("timescaleData") if general_model else None,
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
        "intelligence_evaluation_cost_contributions": (
            intelligence_cost_per_task.get("evaluations") or []
        ),
        "canonical_intelligence_index_token_totals": (
            general_model.get("canonicalIntelligenceIndexTokenCount") if general_model else None
        ),
        "intelligence_evaluations": {
            output: general_model.get(source) if general_model else None
            for source, output in evaluation_fields.items()
        },
        # Every evaluation inside the index, each with its own score, cost, time and tokens.
        "index_evaluations": index_evaluations(general_model),
        "evaluation_reference_model": evaluation_reference_model,
        "benchmark_details": {
            "omniscience_breakdown": (
                general_model.get("omniscienceBreakdown") if general_model else None
            ),
            "coding_sub_scores": coding_model.get("subScores"),
            "coding_weighted_index": coding_model.get("weightedIndex"),
        },
        "result_status": {
            "intelligence_index_is_estimated": (
                general_model.get("intelligenceIndexIsEstimated") if general_model else None
            ),
            "performance_data_source": (
                general_model.get("performanceDataSource") if general_model else None
            ),
            "micro_evaluations_enabled": (
                general_model.get("microevalsEnabled") if general_model else None
            ),
        },
        "coding_index": coding_model.get("headlineValue"),
        "coding_cost_per_task": coding_cost.get("total"),
        "coding_cost_per_task_breakdown": {
            "input_tokens": coding_cost.get("input"),
            "output_tokens": coding_cost.get("answer"),
            "reasoning_tokens": coding_cost.get("reasoning"),
            "cache_write": coding_cost.get("cacheWrite"),
            "cache_hit": coding_cost.get("cacheRead"),
        },
        "coding_time_per_task": coding_model.get("timePerTaskSeconds"),
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
            "deprecated_to": general_model.get("deprecatedTo") if general_model else None,
            "knowledge_cutoff_date": (
                general_model.get("knowledgeCutoffDate") if general_model else None
            ),
            "parameters": general_model.get("parameters") if general_model else None,
            "active_parameters_billions": (
                general_model.get("inferenceParametersActiveBillions") if general_model else None
            ),
            "size_class": general_model.get("sizeClass") if general_model else None,
            "host_model_count": general_model.get("hostModelCount") if general_model else None,
            "context_window_tokens": general_model.get("contextWindowTokens") if general_model else None,
            "modalities": {
                "input": {
                    "text": general_model.get("inputModalityText") if general_model else None,
                    "image": general_model.get("inputModalityImage") if general_model else None,
                    "speech": general_model.get("inputModalitySpeech") if general_model else None,
                    "video": general_model.get("inputModalityVideo") if general_model else None,
                },
                "output": {
                    "text": general_model.get("outputModalityText") if general_model else None,
                    "image": general_model.get("outputModalityImage") if general_model else None,
                    "speech": general_model.get("outputModalitySpeech") if general_model else None,
                    "video": general_model.get("outputModalityVideo") if general_model else None,
                },
            },
            "license": {
                "name": general_model.get("licenseName") if general_model else None,
                "url": general_model.get("licenseUrl") if general_model else None,
                "commercial_allowed": (
                    general_model.get("commercialAllowed") if general_model else None
                ),
                "open_source_categorization": (
                    general_model.get("openSourceCategorization") if general_model else None
                ),
                "weights_source_url": (
                    general_model.get("modelWeightsSourceUrl") if general_model else None
                ),
            },
            "input_price_per_million_tokens": general_model.get("price1mInputTokens") if general_model else None,
            "output_price_per_million_tokens": general_model.get("price1mOutputTokens") if general_model else None,
            "cache_hit_price_per_million_tokens": general_model.get("cacheHitPrice") if general_model else None,
            "cache_write_price_per_million_tokens": general_model.get("cacheWritePrice") if general_model else None,
            "cache_hit_discount_percent": (
                general_model.get("cacheHitDiscountPercent") if general_model else None
            ),
            "blended_prices_per_million_tokens": {
                "0_1_1": general_model.get("price1mBlended0To1To1") if general_model else None,
                "0_3_1": general_model.get("price1mBlended0To3To1") if general_model else None,
                "0_100_1": general_model.get("price1mBlended0To100To1") if general_model else None,
                "7_2_1": general_model.get("price1mBlended7To2To1") if general_model else None,
                "100_1_1": general_model.get("price1mBlended100To1To1") if general_model else None,
            },
            "image_price_per_thousand_1mp_images": (
                general_model.get("pricePer1k1mpImages") if general_model else None
            ),
        },
        "source_record": {"general": general_model, "coding": coding_model or None},
    }
    return {"status": "success", "data": {"model": model}}


def parse_slugs(values: list[str]) -> list[str]:
    slugs: list[str] = []
    for value in values:
        slugs.extend(part.strip() for part in value.split(",") if part.strip())
    return list(dict.fromkeys(slugs))


def build_model_catalog(
    timeout: float = 30.0,
    eligibility: str = "active",
    include_deprecated: bool = False,
    since: str | None = None,
    fields: list[str] | None = None,
    verbose: bool = False,
    *,
    page: str | None = None,
) -> dict[str, Any]:
    """The models with an Intelligence Index, from the models leaderboard.

    The leaderboard is the page meant to list every indexed model, so the catalogue no
    longer depends on one model's page happening to embed the others. Its rows carry no
    release date; a full model record seen on the same page supplies one where present.
    """
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    general_url = CATALOGUE_URL
    objects = extract_embedded_objects(page if page is not None else fetch(general_url, timeout))
    rows = _deduplicate_typed_records(objects, _leaderboard_model_record, "slug")
    full = _deduplicate_typed_records(objects, _canonical_model_record, "slug")
    if not rows and not full:
        raise ExtractionError(f"No model rows found on {general_url}; the page layout has changed")
    general = {slug: {**rows.get(slug, {}), **full.get(slug, {})} for slug in {*rows, *full}}
    coding: dict[str, dict[str, Any]] = {}
    coding_url = None
    catalogue: list[dict[str, Any]] = []
    for slug, record in general.items():
        coding_record = coding.get(slug) or {}
        # A leaderboard row gives the cost per task as a number; a full record as a breakdown.
        cost_field = record.get("intelligenceIndexCostPerTask")
        general_cost = cost_field if isinstance(cost_field, (int, float)) else (
            ((cost_field or {}).get("cost") or {}).get("total"))
        coding_cost = ((coding_record.get("costPerTask") or {}).get("total"))
        item = {
            "slug": slug,
            "name": record.get("name"),
            "short_name": record.get("shortName"),
            "release_date": record.get("releaseDate"),
            "deprecated": bool(record.get("deprecated")),
            "deprecated_to": record.get("deprecatedTo"),
            "creator": record.get("creator") or (
                {"name": record["modelCreatorName"]} if record.get("modelCreatorName") else None
            ),
            "intelligence_index": record.get("intelligenceIndex"),
            "is_reasoning": record.get("isReasoning"),
            "is_open_weights": record.get("isOpenWeights"),
            "has_intelligence_index": record.get("intelligenceIndex") is not None,
            "has_general_cost_data": general_cost is not None,
            "has_coding_index": (
                record.get("codingIndex") is not None or coding_record.get("headlineValue") is not None
            ),
            # The per-model Coding Index is retired, so this is false for every current model.
            "has_coding_cost_data": coding_cost is not None,
            "source_record": {"general": record, "coding": coding_record or None},
        }
        if not include_deprecated and item["deprecated"]:
            continue
        if since and (not item["release_date"] or item["release_date"] < since):
            continue
        eligible = {
            "all": True,
            "active": not item["deprecated"] or include_deprecated,
            "general": item["has_general_cost_data"],
            "coding": item["has_coding_cost_data"],
            # "full" once meant general and coding data. With the Coding Index retired, a model
            # with general cost data is as complete as the site makes it.
            "full": item["has_general_cost_data"],
        }.get(eligibility)
        if eligible is None:
            raise ExtractionError(f"Unknown eligibility mode {eligibility!r}")
        if eligible:
            catalogue.append(item)
    catalogue.sort(key=lambda item: (item.get("release_date") or "", item.get("intelligence_index") or 0, item["slug"]), reverse=True)
    selected_fields = with_identity_fields(
        fields or DEFAULT_CATALOG_FIELDS,
        ("slug",),
    )
    if not verbose:
        catalogue = [project_record(item, selected_fields) for item in catalogue]
    return {
        "schema_version": 2,
        "status": "success",
        "entity_type": "model_catalogue",
        "collected_at": collected_at,
        "data": {
            "models": catalogue,
            "count": len(catalogue),
            "eligibility": eligibility,
            "include_deprecated": include_deprecated,
            "since": since,
            "output_fields": "all" if verbose else selected_fields,
            "sources": {"general": general_url, "coding": coding_url},
        },
    }


def _normalize_agent(record: dict[str, Any], collected_at: str) -> dict[str, Any]:
    mean = record.get("mean") or {}
    benchmark_scores = {
        evaluation.get("datasetIndexName") or evaluation.get("evaluationDatasetSlug"): {
            "dataset_slug": evaluation.get("evaluationDatasetSlug"),
            "reference_dataset": evaluation.get("refDatasetName"),
            "weight": evaluation.get("weight"),
            "score": (evaluation.get("mean") or {}).get("reward"),
            "input_tokens_per_task": (evaluation.get("mean") or {}).get("inputTokens"),
            "cache_write_tokens_per_task": (evaluation.get("mean") or {}).get("cacheWriteTokens"),
            "output_tokens_per_task": (evaluation.get("mean") or {}).get("outputTokens"),
        }
        for evaluation in record.get("evals") or []
    }
    return {
        "collected_at": collected_at,
        "id": record["id"],
        "entity_type": "coding_agent_configuration",
        "agent_name": record.get("agentName"),
        "provider": record.get("provider"),
        "host_model_slug": _host_model_slug(record),
        "host_model_name": (record.get("display") or {}).get("model"),
        "model_creators": [
            creator.get("name") for creator in record.get("modelCreators") or [] if isinstance(creator, dict)
        ],
        "agent_creator": record.get("agentCreator"),
        "display": record.get("display"),
        "display_label": record.get("displayLabel"),
        "variant_of": record.get("variantOf"),
        "index_component_count": record.get("indexComponentCount"),
        "evaluation_count": record.get("evalCount"),
        "index_score": record.get("indexScore"),
        "benchmark_scores": benchmark_scores,
        "cost_per_task": mean.get("costUsd"),
        "time_per_task_seconds": mean.get("agentWallTimeSec"),
        "steps": mean.get("steps"),
        "input_tokens_per_task": mean.get("inputTokens"),
        "cache_write_tokens_per_task": mean.get("cacheWriteTokens"),
        "cache_tokens_per_task": mean.get("cacheTokens"),
        "output_tokens_per_task": mean.get("outputTokens"),
        "total_tokens_per_task": mean.get("totalTokens"),
        "cache_hit_rate": mean.get("cacheHitRate"),
        "total_cost": (record.get("sums") or {}).get("costUsd"),
        "percentiles": record.get("percentiles"),
        "versions": record.get("versions"),
        "source_record": record,
    }


def extract_coding_agents(
    selectors: list[str] | None = None,
    timeout: float = 30.0,
    fields: list[str] | None = None,
    verbose: bool = False,
    *,
    page: str | None = None,
) -> dict[str, Any]:
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    url = AGENTS_URL
    records = _deduplicate_typed_records(
        extract_embedded_objects(page if page is not None else fetch(url, timeout)),
        _canonical_agent_record,
        "id",
    )
    selected = list(records.values())
    if selectors:
        chosen: list[dict[str, Any]] = []
        for selector in selectors:
            matches = [
                record for record in selected
                if selector in {record["id"], record.get("displayLabel")}
            ]
            if len(matches) != 1:
                raise ExtractionError(
                    f"Coding-agent selector {selector!r} matched {len(matches)} records; "
                    "use the stable configuration id or exact display label"
                )
            chosen.append(matches[0])
        selected = chosen
    selected.sort(key=lambda record: record.get("indexScore") or -1, reverse=True)
    agents = [_normalize_agent(record, collected_at) for record in selected]
    selected_fields = with_identity_fields(
        fields or DEFAULT_AGENT_FIELDS,
        ("id", "host_model_slug"),
    )
    if not verbose:
        agents = [project_record(agent, selected_fields) for agent in agents]
    return {
        "schema_version": 2,
        "status": "success",
        "entity_type": "coding_agent_configuration_collection",
        "collected_at": collected_at,
        "data": {
            "coding_agents": agents,
            "count": len(agents),
            "output_fields": "all" if verbose else selected_fields,
            "source": url,
        },
    }


def extract_models(
    slugs: list[str],
    timeout: float = 30.0,
    evaluation_reference_model: str | None = EVALUATION_REFERENCE_MODEL,
    fields: list[str] | None = None,
    verbose: bool = False,
    *,
    workers: int = 4,
    fetcher: Any = None,
) -> dict[str, Any]:
    if not slugs:
        raise ExtractionError("At least one model slug is required")
    for slug in slugs:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
            raise ExtractionError(f"Invalid model slug: {slug!r}")

    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    wanted = list(dict.fromkeys((*slugs, *([evaluation_reference_model] if evaluation_reference_model else []))))
    records, sources = collect_model_records(wanted, timeout, workers=workers, fetcher=fetcher)
    evaluation_fields = evaluation_schema({slug: records[slug] for slug in slugs if slug in records} | (
        {evaluation_reference_model: records[evaluation_reference_model]}
        if evaluation_reference_model in records else {}
    ), evaluation_reference_model)

    models: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for slug in slugs:
        if slug not in records:
            errors.append({"slug": slug, "error": "no model record on its page"})
            continue
        try:
            response = extract_single_model(
                slug,
                timeout,
                general_record=records[slug],
                evaluation_fields=evaluation_fields,
                evaluation_reference_model=evaluation_reference_model,
                collected_at=collected_at,
            )
            models.append(response["data"]["model"])
        except ExtractionError as exc:
            errors.append({"slug": slug, "error": str(exc)})

    if errors:
        raise ExtractionError(
            "Incomplete model records: " + json.dumps(errors, ensure_ascii=False)
        )
    general_url = sources.get(slugs[0])
    coding_url = None

    selected_fields = with_identity_fields(
        fields or DEFAULT_MODEL_FIELDS,
        ("slug",),
    )
    if not verbose:
        models = [project_record(model, selected_fields) for model in models]

    common = {
        "requested_count": len(slugs),
        "returned_count": len(models),
        "evaluation_reference_model": evaluation_reference_model,
        "evaluation_fields": list(evaluation_fields.values()),
        "sources": {"general": general_url, "coding": coding_url},
        "output_fields": "all" if verbose else selected_fields,
    }
    data: dict[str, Any]
    if len(models) == 1:
        data = {"model": models[0], **common}
    else:
        data = {"models": models, **common}
    return {
        "schema_version": 2,
        "status": "success",
        "collected_at": collected_at,
        "data": data,
    }


def check(timeout: float = 30.0, sample: int = 3) -> dict[str, Any]:
    """Exercise every mode against the live site and report what each one returned.

    The pages this reads change without notice, so a collection is only as good as the last
    time this passed. Each check names what it expected; nothing here is cached.
    """
    # An error means the extraction itself is broken (a source gone, nothing parsed). A
    # warning is a gap in what AA has published so far (a model listed before its latency
    # runs, an agent on an unpriced model), which a collection keeps as null.
    report: dict[str, Any] = {"status": "success", "checks": []}

    def record(name: str, ok: bool, detail: str, warning: bool = False) -> None:
        level = "ok" if ok else "warning" if warning else "error"
        report["checks"].append({"check": name, "result": level, "detail": detail})
        if level == "error":
            report["status"] = "error"
        elif level == "warning" and report["status"] == "success":
            report["status"] = "warning"

    try:
        catalogue = build_model_catalog(timeout, "general", verbose=True)["data"]["models"]
        record("catalogue", len(catalogue) >= 20, f"{len(catalogue)} models with general cost data")
    except ExtractionError as exc:
        catalogue = []
        record("catalogue", False, str(exc))

    if catalogue:
        slugs = [item["slug"] for item in catalogue[:sample]]
        try:
            models = extract_models(slugs, timeout, verbose=True)["data"]
            models = models.get("models") or [models["model"]]
            for model in models:
                missing = [key for key in ("cost_per_task", "time_per_task", "intelligence_index", "output_tokens_per_task")
                           if model.get(key) is None]
                evals = model.get("index_evaluations") or []
                with_resources = sum(1 for e in evals if e.get("cost_per_task") is not None and e.get("time_per_task") is not None)
                broken = model.get("intelligence_index") is None or model.get("cost_per_task") is None or not evals
                record(f"model {model['slug']}", not missing and with_resources > 0,
                       f"{len(evals)} index evaluations, {with_resources} with cost and time"
                       + (f"; not yet published: {', '.join(missing)}" if missing else ""),
                       warning=not broken)
        except ExtractionError as exc:
            record("model details", False, str(exc))

    try:
        agents = extract_coding_agents(timeout=timeout, verbose=True)["data"]["coding_agents"]
        components = sorted({key for agent in agents for key in (agent.get("benchmark_scores") or {})})
        unpriced = [a["display_label"] for a in agents if a.get("cost_per_task") is None]
        record("coding agents", len(agents) >= 5 and not unpriced,
               f"{len(agents)} configurations; components {', '.join(components) or 'none'}"
               + (f"; unpriced: {', '.join(unpriced)}" if unpriced else ""),
               warning=len(agents) >= 5 and len(unpriced) <= len(agents) // 4)
    except ExtractionError as exc:
        record("coding agents", False, str(exc))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slugs", nargs="*", help="Model slugs separated by spaces and/or commas")
    parser.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--evaluation-reference-model",
        default=EVALUATION_REFERENCE_MODEL,
        help="Model whose reported evaluations define the benchmark field set",
    )
    parser.add_argument("--list-models", action="store_true", help="Return the model catalogue")
    parser.add_argument(
        "--eligibility",
        choices=("all", "active", "general", "coding", "full"),
        default="active",
        help="Catalogue eligibility filter",
    )
    parser.add_argument("--include-deprecated", action="store_true")
    parser.add_argument("--since", help="Catalogue release-date lower bound (YYYY-MM-DD)")
    parser.add_argument(
        "--coding-agents", "--list-coding-agents",
        dest="coding_agents", action="store_true",
        help="Return Coding Agent Index configurations",
    )
    parser.add_argument(
        "--coding-agent",
        action="append",
        default=[],
        help="Select a coding-agent configuration by id or exact display label; repeatable",
    )
    parser.add_argument(
        "--fields",
        action="append",
        help="Comma-separated dotted fields or groups: summary, cost, tokens, timing, "
             "evaluations, coding, metadata, performance, source",
    )
    parser.add_argument("--verbose", action="store_true", help="Return every available field")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    parser.add_argument("--check", action="store_true",
                        help="Exercise every mode against the live site and report what is missing")
    args = parser.parse_args()
    if args.check:
        result = check(args.timeout)
        print(json.dumps(result, indent=None if args.compact else 2, ensure_ascii=False))
        return 1 if result["status"] == "error" else 0
    if args.verbose and args.fields:
        parser.error("--verbose and --fields are mutually exclusive")
    modes = int(bool(args.slugs)) + int(args.list_models) + int(bool(args.coding_agents or args.coding_agent))
    if modes != 1:
        parser.error("choose exactly one mode: model slugs, --list-models, or --coding-agents")
    selected_fields = parse_field_selection(args.fields)
    try:
        if args.list_models:
            result = build_model_catalog(
                args.timeout,
                args.eligibility,
                args.include_deprecated,
                args.since,
                selected_fields,
                args.verbose,
            )
        elif args.coding_agents or args.coding_agent:
            result = extract_coding_agents(
                args.coding_agent or None,
                args.timeout,
                selected_fields,
                args.verbose,
            )
        else:
            result = extract_models(
                parse_slugs(args.slugs),
                args.timeout,
                args.evaluation_reference_model,
                selected_fields,
                args.verbose,
            )
    except ExtractionError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=None if args.compact else 2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
