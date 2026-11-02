#!/usr/bin/env python3
"""Export model data from the local archive to aa-data.json for the chart."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

DATA_DIR = Path("data")
DATABASE_NAME = "archive.sqlite3"

def main():
    database = DATA_DIR / DATABASE_NAME
    if not database.exists():
        raise SystemExit(f"Database {database} not found; run collect first")

    with closing(sqlite3.connect(str(database))) as conn:
        # Find the latest collection's model run IDs
        manifests = conn.execute(
            "SELECT run_id, job_json FROM collection_runs WHERE entity_type = 'frontier_manifest'"
        ).fetchall()
        if not manifests:
            raise SystemExit("No frontier manifests found; run collect first")
        latest = json.loads(manifests[-1][1])
        collection_id = latest["collection_id"]
        meta = json.loads(manifests[-1][1])

        # Get all model run IDs for this collection
        model_runs = conn.execute(
            "SELECT run_id FROM collection_runs WHERE entity_type = 'models' AND json_extract(job_json, '$.collection_id') = ?",
            (collection_id,)
        ).fetchall()
        model_run_ids = [r[0] for r in model_runs]

        # Get catalogue
        catalogue_run = conn.execute(
            "SELECT run_id FROM collection_runs WHERE entity_type = 'model_catalogue' AND json_extract(job_json, '$.collection_id') = ?",
            (collection_id,)
        ).fetchone()
        if catalogue_run:
            catalogue_rows = conn.execute(
                "SELECT payload_json FROM snapshots WHERE run_id = ? AND entity_type = 'model_catalogue_entry'",
                (catalogue_run[0],)
            ).fetchall()
            active = set()
            for row in catalogue_rows:
                entry = json.loads(row[0])
                if not entry.get("deprecated"):
                    active.add(entry["slug"])
        else:
            active = None  # no filtering

        # Get all model snapshots
        exported = []
        for run_id in model_run_ids:
            rows = conn.execute(
                "SELECT payload_json FROM snapshots WHERE run_id = ? AND entity_type = 'model'",
                (run_id,)
            ).fetchall()
            for row in rows:
                m = json.loads(row[0])
                slug = m.get("slug")
                if active is not None and slug not in active:
                    continue

                ii = m.get("intelligence_index")
                ci = m.get("coding_index")
                cpt = m.get("cost_per_task")
                tpt = m.get("time_per_task") or m.get("end_to_end_response_time")
                out_tok = m.get("output_tokens_per_task")
                in_tok = m.get("input_tokens_per_task")
                coding_cpt = m.get("coding_cost_per_task")
                coding_tpt = m.get("coding_time_per_task")
                ttft = m.get("ttft")
                speed = m.get("output_speed_tokens_per_second")

                if all(v is not None for v in [ii, ci, cpt, tpt]):
                    exported.append({
                        "slug": slug,
                        "name": m.get("short_name") or m.get("name", slug),
                        "intelligence_index": ii,
                        "coding_index": ci,
                        "cost_per_task": cpt,
                        "coding_cost_per_task": coding_cpt or cpt,
                        "time_per_task": tpt,
                        "coding_time_per_task": coding_tpt or tpt,
                        "output_tokens_per_task": out_tok,
                        "input_tokens_per_task": in_tok,
                        "ttft": ttft,
                        "output_speed": speed,
                    })

    out_path = Path("aa-data.json")
    with open(out_path, "w") as f:
        json.dump({"collection_id": collection_id, "models": exported, "count": len(exported)}, f, indent=2)
    print(f"Exported {len(exported)} models to {out_path} (collection {collection_id[:12]}...)")

if __name__ == "__main__":
    main()
