#!/usr/bin/env python3
"""Persist verbose collections as recoverable JSON records and SQLite snapshots."""

from __future__ import annotations

import argparse
import hashlib
import json
import argcomplete
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import artificial_analysis_v2 as scraper


ARCHIVE_VERSION = 1
DEFAULT_DATA_DIR = Path("data")
DATABASE_NAME = "archive.sqlite3"
JSON_DIRECTORY = "json"
MAX_MODELS_PER_BATCH = 40
MAX_ENCODED_SLUGS_LENGTH = 6500

SCHEMA = """
CREATE TABLE IF NOT EXISTS collection_runs (
    run_id TEXT PRIMARY KEY,
    collected_at TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    job_json TEXT NOT NULL,
    response_sha256 TEXT NOT NULL,
    json_path TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS snapshots (
    run_id TEXT NOT NULL REFERENCES collection_runs(run_id) ON DELETE CASCADE,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    release_date TEXT,
    collected_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS snapshots_entity_history
    ON snapshots(entity_type, entity_id, collected_at);
CREATE INDEX IF NOT EXISTS snapshots_payload_hash
    ON snapshots(entity_type, entity_id, payload_sha256);
"""


class ArchiveError(RuntimeError):
    pass


@dataclass(frozen=True)
class PersistedRun:
    path: Path
    run_id: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def paths(data_dir: Path) -> tuple[Path, Path]:
    return data_dir / DATABASE_NAME, data_dir / JSON_DIRECTORY


def connect_database(database: Path) -> sqlite3.Connection:
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        connection = sqlite3.connect(database)
        result = connection.execute("PRAGMA quick_check").fetchone()
        if result != ("ok",):
            raise ArchiveError(f"SQLite integrity check failed: {result!r}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(SCHEMA)
        return connection
    except (sqlite3.DatabaseError, OSError) as exc:
        raise ArchiveError(
            f"Cannot use database {database}: {exc}. "
            "Preserve the JSON records and run `archive.py rebuild --replace`."
        ) from exc


def atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def make_archive_record(job: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
    collected_at = response.get("collected_at") or utc_now()
    return {
        "archive_version": ARCHIVE_VERSION,
        "run_id": uuid.uuid4().hex,
        "collected_at": collected_at,
        "job": job,
        "response_sha256": digest(response),
        "response": response,
    }


def json_relative_path(record: dict[str, Any]) -> Path:
    date = str(record["collected_at"])[0:10]
    year, month, day = date.split("-")
    job_type = record["job"]["type"]
    return Path(year, month, day, f"{record['collected_at'].replace(':', '')}_{job_type}_{record['run_id']}.json")


def entities(record: dict[str, Any]) -> Iterable[tuple[str, str, str | None, dict[str, Any]]]:
    response = record["response"]
    job_type = record["job"]["type"]
    data = response.get("data") or {}
    if job_type == "model_catalogue":
        for model in data.get("models") or []:
            yield "model_catalogue_entry", model["slug"], model.get("release_date"), model
    elif job_type == "models":
        models = [data["model"]] if "model" in data else data.get("models") or []
        for model in models:
            yield "model", model["slug"], (model.get("model_metadata") or {}).get("release_date"), model
    elif job_type == "coding_agents":
        for agent in data.get("coding_agents") or []:
            yield "coding_agent_configuration", agent["id"], None, agent
    elif job_type == "frontier_manifest":
        manifest = data["manifest"]
        yield "frontier_collection", manifest["collection_id"], None, manifest
    else:
        raise ArchiveError(f"Unknown archive job type {job_type!r}")


def validate_archive_record(record: dict[str, Any], source: Path | None = None) -> None:
    label = str(source) if source else "archive record"
    required = {"archive_version", "run_id", "collected_at", "job", "response_sha256", "response"}
    missing = required - record.keys()
    if missing:
        raise ArchiveError(f"{label} is missing fields: {', '.join(sorted(missing))}")
    if record["archive_version"] != ARCHIVE_VERSION:
        raise ArchiveError(f"{label} uses unsupported archive version {record['archive_version']!r}")
    if digest(record["response"]) != record["response_sha256"]:
        raise ArchiveError(f"{label} failed its SHA-256 check")
    list(entities(record))


def insert_record(connection: sqlite3.Connection, record: dict[str, Any], relative_path: Path) -> None:
    validate_archive_record(record)
    response = record["response"]
    job_type = record["job"]["type"]
    connection.execute(
        "INSERT INTO collection_runs VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            record["run_id"], record["collected_at"], job_type,
            int(response.get("schema_version", 0)), canonical_json(record["job"]),
            record["response_sha256"], relative_path.as_posix(),
        ),
    )
    for entity_type, entity_id, release_date, payload in entities(record):
        connection.execute(
            "INSERT INTO snapshots VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record["run_id"], entity_type, entity_id, release_date,
                record["collected_at"], canonical_json(payload), digest(payload),
            ),
        )


def persist(data_dir: Path, job: dict[str, Any], response: dict[str, Any]) -> PersistedRun:
    database, json_dir = paths(data_dir)
    record = make_archive_record(job, response)
    relative = json_relative_path(record)
    destination = json_dir / relative
    atomic_json_write(destination, record)
    try:
        with closing(connect_database(database)) as connection:
            with connection:
                insert_record(connection, record, relative)
    except Exception:
        # Deliberately retain the durable JSON record for a later rebuild.
        raise
    return PersistedRun(destination, record["run_id"])


def batch_slugs(slugs: list[str]) -> list[list[str]]:
    """Bound model-set URLs by both model count and encoded query length."""
    batches: list[list[str]] = []
    current: list[str] = []
    current_length = 0
    for slug in slugs:
        encoded_length = len(slug) + (3 if current else 0)  # encoded comma is %2C
        if current and (
            len(current) >= MAX_MODELS_PER_BATCH
            or current_length + encoded_length > MAX_ENCODED_SLUGS_LENGTH
        ):
            batches.append(current)
            current = []
            current_length = 0
            encoded_length = len(slug)
        current.append(slug)
        current_length += encoded_length
    if current:
        batches.append(current)
    return batches


def manifest_response(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "status": "success",
        "entity_type": "frontier_collection_manifest",
        "collected_at": manifest["finished_at"],
        "data": {"manifest": manifest},
    }


def collect(args: argparse.Namespace) -> tuple[list[Path], dict[str, Any]]:
    collection_id = uuid.uuid4().hex
    started_at = utc_now()
    written: list[Path] = []
    catalogue_run_id: str | None = None
    model_run_ids: list[str] = []
    coding_agents_run_id: str | None = None
    discovered_slugs: list[str] = []
    selected_slugs: list[str] = []
    skipped: list[dict[str, str]] = []
    failure: str | None = None

    try:
        if not args.no_catalogue:
            job = {
                "type": "model_catalogue", "eligibility": "all",
                "include_deprecated": True, "since": None,
                "collection_id": collection_id,
            }
            # Archive the complete catalogue. User filters only limit deep records.
            response = scraper.build_model_catalog(
                timeout=args.timeout, eligibility="all",
                include_deprecated=True, since=None, verbose=True,
            )
            persisted = persist(args.data_dir, job, response)
            written.append(persisted.path)
            catalogue_run_id = persisted.run_id
            catalogue = response["data"]["models"]
            discovered_slugs = [model["slug"] for model in catalogue]

            if args.models:
                selected_slugs = scraper.parse_slugs(args.models)
            elif not args.no_model_details:
                for model in catalogue:
                    release_date = model.get("release_date")
                    eligibility_matches = {
                        "all": True,
                        "active": not model.get("deprecated"),
                        "general": bool(model.get("has_general_cost_data")),
                        "coding": bool(model.get("has_coding_cost_data")),
                        "full": bool(
                            model.get("has_general_cost_data")
                            and model.get("has_coding_cost_data")
                        ),
                    }[args.eligibility]
                    if model.get("deprecated") and not args.include_deprecated:
                        skipped.append({"slug": model["slug"], "reason": "deprecated"})
                    elif not eligibility_matches:
                        skipped.append({"slug": model["slug"], "reason": "ineligible"})
                    elif args.since and (not release_date or release_date < args.since):
                        skipped.append({"slug": model["slug"], "reason": "before_since"})
                    elif not (
                        model.get("has_general_cost_data")
                        and model.get("has_coding_cost_data")
                    ):
                        skipped.append({"slug": model["slug"], "reason": "incomplete_detail_data"})
                    else:
                        selected_slugs.append(model["slug"])
        elif args.models:
            selected_slugs = scraper.parse_slugs(args.models)
        elif not args.no_model_details:
            raise ArchiveError("Automatic model discovery requires the catalogue")

        for batch_number, slugs in enumerate(batch_slugs(selected_slugs), start=1):
            job = {
                "type": "models", "slugs": slugs,
                "evaluation_reference_model": args.evaluation_reference_model,
                "collection_id": collection_id, "batch": batch_number,
            }
            response = scraper.extract_models(
                slugs, timeout=args.timeout,
                evaluation_reference_model=args.evaluation_reference_model, verbose=True,
            )
            persisted = persist(args.data_dir, job, response)
            written.append(persisted.path)
            model_run_ids.append(persisted.run_id)

        if not args.no_coding_agents:
            job = {
                "type": "coding_agents", "selectors": args.coding_agent or [],
                "collection_id": collection_id,
            }
            response = scraper.extract_coding_agents(
                args.coding_agent or None, timeout=args.timeout, verbose=True,
            )
            persisted = persist(args.data_dir, job, response)
            written.append(persisted.path)
            coding_agents_run_id = persisted.run_id
    except (ArchiveError, scraper.ExtractionError) as exc:
        failure = str(exc)

    manifest = {
        "collection_id": collection_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "completed": failure is None,
        "error": failure,
        "filters": {
            "eligibility": args.eligibility,
            "include_deprecated": args.include_deprecated,
            "since": args.since,
        },
        "runs": {
            "catalogue": catalogue_run_id,
            "models": model_run_ids,
            "coding_agents": coding_agents_run_id,
        },
        "discovered_model_count": len(discovered_slugs),
        "selected_model_count": len(selected_slugs),
        "selected_model_slugs": selected_slugs,
        "skipped_models": skipped,
        "model_batch_count": len(model_run_ids),
    }
    manifest_run = persist(
        args.data_dir,
        {"type": "frontier_manifest", "collection_id": collection_id},
        manifest_response(manifest),
    )
    written.append(manifest_run.path)
    if failure:
        raise ArchiveError(f"Frontier collection {collection_id} was incomplete: {failure}")
    return written, manifest


def load_json_records(json_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    records: list[tuple[Path, dict[str, Any]]] = []
    if not json_dir.exists():
        return records
    for path in sorted(json_dir.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ArchiveError(f"Cannot read {path}: {exc}") from exc
        validate_archive_record(record, path)
        records.append((path.relative_to(json_dir), record))
    return records


def rebuild(data_dir: Path, replace: bool) -> tuple[int, int]:
    database, json_dir = paths(data_dir)
    records = load_json_records(json_dir)
    if database.exists() and not replace:
        raise ArchiveError(f"{database} already exists; pass --replace after preserving a backup")
    data_dir.mkdir(parents=True, exist_ok=True)
    temporary = data_dir / f".{DATABASE_NAME}.rebuild-{uuid.uuid4().hex}"
    try:
        with closing(connect_database(temporary)) as connection:
            with connection:
                for relative, record in records:
                    insert_record(connection, record, relative)
        for suffix in ("-wal", "-shm"):
            Path(str(database) + suffix).unlink(missing_ok=True)
        os.replace(temporary, database)
    finally:
        temporary.unlink(missing_ok=True)
    return len(records), sum(len(list(entities(record))) for _, record in records)


def verify(data_dir: Path) -> tuple[int, int]:
    database, json_dir = paths(data_dir)
    records = load_json_records(json_dir)
    if not database.exists():
        raise ArchiveError(f"Database {database} does not exist; run rebuild to create it")
    with closing(connect_database(database)) as connection:
        db_runs = connection.execute("SELECT COUNT(*) FROM collection_runs").fetchone()[0]
        db_snapshots = connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        expected = {record["run_id"]: record["response_sha256"] for _, record in records}
        actual = dict(connection.execute("SELECT run_id, response_sha256 FROM collection_runs"))
        if actual != expected:
            raise ArchiveError("SQLite run IDs or response hashes do not match the JSON record set")
        expected_snapshots = {
            (record["run_id"], entity_type, entity_id): digest(payload)
            for _, record in records
            for entity_type, entity_id, _, payload in entities(record)
        }
        actual_snapshots: dict[tuple[str, str, str], str] = {}
        for run_id, entity_type, entity_id, payload_json, payload_sha256 in connection.execute(
            "SELECT run_id, entity_type, entity_id, payload_json, payload_sha256 FROM snapshots"
        ):
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError as exc:
                raise ArchiveError(
                    f"SQLite payload for {entity_type}/{entity_id} is invalid JSON"
                ) from exc
            calculated = digest(payload)
            if calculated != payload_sha256:
                raise ArchiveError(
                    f"SQLite payload for {entity_type}/{entity_id} failed its SHA-256 check"
                )
            actual_snapshots[(run_id, entity_type, entity_id)] = payload_sha256
        if actual_snapshots != expected_snapshots:
            raise ArchiveError(
                "SQLite snapshot IDs or payload hashes do not match the JSON record set"
            )
    return db_runs, db_snapshots


def latest_manifest(connection: sqlite3.Connection, at: str | None = None) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT payload_json FROM snapshots "
        "WHERE entity_type = 'frontier_collection' "
        "AND (? IS NULL OR collected_at <= ?) ORDER BY collected_at DESC, rowid DESC",
        (at, at),
    )
    for (payload_json,) in rows:
        manifest = json.loads(payload_json)
        if manifest.get("completed"):
            return manifest
    qualifier = f" at or before {at}" if at else ""
    raise ArchiveError(f"No completed frontier collection exists{qualifier}")


def snapshots_for_runs(
    connection: sqlite3.Connection,
    run_ids: Iterable[str],
    entity_type: str,
) -> list[dict[str, Any]]:
    run_ids = list(run_ids)
    if not run_ids:
        return []
    placeholders = ",".join("?" for _ in run_ids)
    rows = connection.execute(
        f"SELECT payload_json FROM snapshots WHERE entity_type = ? "
        f"AND run_id IN ({placeholders}) ORDER BY collected_at, entity_id",
        (entity_type, *run_ids),
    )
    return [json.loads(row[0]) for row in rows]


def query_cache(args: argparse.Namespace) -> dict[str, Any]:
    database, _ = paths(args.data_dir)
    if not database.exists():
        raise ArchiveError(f"Database {database} does not exist; run collect or rebuild first")
    fields = scraper.parse_field_selection(args.fields)
    with closing(connect_database(database)) as connection:
        manifest = latest_manifest(connection, args.at)
        runs = manifest["runs"]
        if args.list_models:
            records = snapshots_for_runs(
                connection, [runs["catalogue"]] if runs.get("catalogue") else [],
                "model_catalogue_entry",
            )
            filtered: list[dict[str, Any]] = []
            for record in records:
                if not args.include_deprecated and record.get("deprecated"):
                    continue
                if args.since and (
                    not record.get("release_date") or record["release_date"] < args.since
                ):
                    continue
                eligible = {
                    "all": True,
                    "active": not record.get("deprecated") or args.include_deprecated,
                    "general": bool(record.get("has_general_cost_data")),
                    "coding": bool(record.get("has_coding_cost_data")),
                    "full": bool(
                        record.get("has_general_cost_data")
                        and record.get("has_coding_cost_data")
                    ),
                }[args.eligibility]
                if eligible:
                    filtered.append(record)
            records = filtered
            records.sort(
                key=lambda item: (item.get("release_date") or "", item["slug"]),
                reverse=True,
            )
            selected_fields = scraper.with_identity_fields(
                fields or scraper.DEFAULT_CATALOG_FIELDS, ("slug",),
            )
            if not args.verbose:
                records = [scraper.project_record(record, selected_fields) for record in records]
            entity_type = "model_catalogue"
            data: dict[str, Any] = {"models": records, "count": len(records)}
        elif args.coding_agents or args.coding_agent:
            records = snapshots_for_runs(
                connection, [runs["coding_agents"]] if runs.get("coding_agents") else [],
                "coding_agent_configuration",
            )
            if args.coding_agent:
                chosen: list[dict[str, Any]] = []
                for selector in args.coding_agent:
                    matches = [
                        record for record in records
                        if selector in {record["id"], record.get("display_label")}
                    ]
                    if len(matches) != 1:
                        raise ArchiveError(
                            f"Cached coding-agent selector {selector!r} matched {len(matches)} records"
                        )
                    chosen.append(matches[0])
                records = chosen
            records.sort(key=lambda record: record.get("index_score") or -1, reverse=True)
            selected_fields = scraper.with_identity_fields(
                fields or scraper.DEFAULT_AGENT_FIELDS, ("id", "host_model_slug"),
            )
            if not args.verbose:
                records = [scraper.project_record(record, selected_fields) for record in records]
            entity_type = "coding_agent_configuration_collection"
            data = {"coding_agents": records, "count": len(records)}
        else:
            requested = scraper.parse_slugs(args.slugs)
            records = snapshots_for_runs(connection, runs.get("models") or [], "model")
            by_slug = {record["slug"]: record for record in records}
            missing = [slug for slug in requested if slug not in by_slug]
            if missing:
                raise ArchiveError(
                    "Models are not present in the selected frontier collection: "
                    + ", ".join(missing)
                )
            records = [by_slug[slug] for slug in requested]
            selected_fields = scraper.with_identity_fields(
                fields or scraper.DEFAULT_MODEL_FIELDS, ("slug",),
            )
            if not args.verbose:
                records = [scraper.project_record(record, selected_fields) for record in records]
            entity_type = "model_collection"
            data = {"model": records[0]} if len(records) == 1 else {"models": records}
            data.update({"requested_count": len(requested), "returned_count": len(records)})
    data.update({
        "output_fields": "all" if args.verbose else selected_fields,
        "cache": {
            "collection_id": manifest["collection_id"],
            "collected_from": manifest["started_at"],
            "collected_to": manifest["finished_at"],
        },
    })
    return {
        "schema_version": 2,
        "status": "success",
        "entity_type": entity_type,
        "collected_at": manifest["finished_at"],
        "data": data,
    }


def reset(data_dir: Path, confirmed: bool) -> Path | None:
    if not confirmed:
        raise ArchiveError("Reset requires --yes")
    resolved = data_dir.resolve()
    if resolved in {Path("/").resolve(), Path.cwd().resolve(), Path.home().resolve()}:
        raise ArchiveError(f"Refusing to reset broad directory {resolved}")
    if not data_dir.exists():
        data_dir.mkdir(parents=True)
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = data_dir.with_name(f"{data_dir.name}.reset-{stamp}")
    counter = 1
    while backup.exists():
        backup = data_dir.with_name(f"{data_dir.name}.reset-{stamp}-{counter}")
        counter += 1
    shutil.move(data_dir, backup)
    data_dir.mkdir(parents=True)
    return backup


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser("collect", help="Collect verbose JSON and SQLite snapshots")
    collect_parser.add_argument("--models", action="append", default=[], help="Model slugs; comma-separated or repeatable")
    collect_parser.add_argument("--coding-agent", action="append", default=[], help="Limit coding agents by ID or exact label")
    collect_parser.add_argument("--no-catalogue", action="store_true")
    collect_parser.add_argument("--no-model-details", action="store_true")
    collect_parser.add_argument("--no-coding-agents", action="store_true")
    collect_parser.add_argument("--eligibility", choices=("all", "active", "general", "coding", "full"), default="active")
    collect_parser.add_argument("--include-deprecated", action="store_true")
    collect_parser.add_argument("--since")
    collect_parser.add_argument("--evaluation-reference-model", default=scraper.EVALUATION_REFERENCE_MODEL)
    collect_parser.add_argument("--timeout", type=float, default=30.0)

    rebuild_parser = subparsers.add_parser("rebuild", help="Reconstruct SQLite from JSON records")
    rebuild_parser.add_argument("--replace", action="store_true", help="Atomically replace an existing database")
    subparsers.add_parser("verify", help="Verify JSON hashes, SQLite integrity, and parity")
    reset_parser = subparsers.add_parser("reset", help="Move the data directory aside and start empty")
    reset_parser.add_argument("--yes", action="store_true", help="Confirm the reset")

    query_parser = subparsers.add_parser("query", help="Query a completed frontier collection locally")
    query_parser.add_argument("slugs", nargs="*", help="Model slugs separated by spaces and/or commas")
    query_parser.add_argument("--list-models", action="store_true")
    query_parser.add_argument("--eligibility", choices=("all", "active", "general", "coding", "full"), default="active")
    query_parser.add_argument("--include-deprecated", action="store_true")
    query_parser.add_argument("--since")
    query_parser.add_argument("--coding-agents", "--list-coding-agents", dest="coding_agents", action="store_true")
    query_parser.add_argument("--coding-agent", action="append", default=[])
    query_parser.add_argument("--fields", action="append", help="Dotted fields or named field groups")
    query_parser.add_argument("--verbose", action="store_true")
    query_parser.add_argument("--at", help="Use the latest completed collection at or before this UTC timestamp")
    query_parser.add_argument("--compact", action="store_true")
    argcomplete.autocomplete(parser)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.command == "collect":
            written, manifest = collect(args)
            result = {
                "status": "success", "collection_id": manifest["collection_id"],
                "models": manifest["selected_model_count"],
                "model_batches": manifest["model_batch_count"],
                "json_records": [str(path) for path in written],
            }
        elif args.command == "rebuild":
            runs, snapshots = rebuild(args.data_dir, args.replace)
            result = {"status": "success", "runs": runs, "snapshots": snapshots}
        elif args.command == "verify":
            runs, snapshots = verify(args.data_dir)
            result = {"status": "success", "runs": runs, "snapshots": snapshots}
        elif args.command == "query":
            if args.verbose and args.fields:
                parser.error("query --verbose and --fields are mutually exclusive")
            modes = int(bool(args.slugs)) + int(args.list_models) + int(bool(args.coding_agents or args.coding_agent))
            if modes != 1:
                parser.error("query requires exactly one mode: model slugs, --list-models, or --coding-agents")
            result = query_cache(args)
        else:
            backup = reset(args.data_dir, args.yes)
            result = {"status": "success", "backup": str(backup) if backup else None}
    except (ArchiveError, scraper.ExtractionError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=None if getattr(args, "compact", False) else 2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
