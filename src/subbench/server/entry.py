"""Cloudflare Python Worker entry point.

Deliberately thin. Everything that decides anything lives in `ingest`, `queries`,
`assemble` and `confidence`, which are synchronous and tested against SQLite; this module
only moves bytes between D1 and those functions. Keeping the runtime boundary this narrow
is what makes moving to Containers a redeploy rather than a rewrite.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from workers import Response, WorkerEntrypoint  # type: ignore[import-not-found]

from . import ingest, queries
from .assemble import IMPORT_DAYS_SQL, SNAPSHOTS_SQL, assemble_points

JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}
DEFAULT_RETENTION_DAYS = 90


def _json(payload, status: int = 200) -> "Response":
    return Response(json.dumps(payload), status=status, headers=JSON_HEADERS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path(url: str) -> str:
    without_scheme = url.split("://", 1)[-1]
    path = "/" + without_scheme.split("/", 1)[1] if "/" in without_scheme else "/"
    return path.split("?", 1)[0].rstrip("/") or "/"


class Default(WorkerEntrypoint):
    async def _rows(self, sql: str, params: list | None = None) -> list[dict]:
        statement = self.env.DB.prepare(sql)
        if params:
            statement = statement.bind(*params)
        result = await statement.all()
        return [dict(row) for row in result.results]

    async def _points(self) -> list[dict]:
        """Evidence from every agent, merged per account.

        Agents reporting the same (provider, account) describe one meter and one
        allowance, and each ccusage sees only its own machine, so their spend is summed.
        Separate accounts stay separate: those are different entitlements, pooled at the
        estimate level by rolling_values, never at the pair level.
        """
        snapshots = await self._rows(SNAPSHOTS_SQL)
        imports = await self._rows(IMPORT_DAYS_SQL)
        return assemble_points(snapshots, imports)

    async def _log(self, agent_id, status: int, detail) -> None:
        await self.env.DB.prepare(
            "INSERT INTO ingest_log (received_at, agent_id, status, detail) VALUES (?, ?, ?, ?)"
        ).bind(_now(), agent_id, status, detail).run()

    async def _ingest(self, request) -> "Response":
        expected = getattr(self.env, "SUBBENCH_INGEST_TOKEN", None)
        supplied = request.headers.get("authorization") or ""
        if not expected or supplied != f"Bearer {expected}":
            return _json({"error": "unauthorized"}, 401)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 - any decode failure is a client error
            return _json({"error": "body must be JSON"}, 400)
        if hasattr(payload, "to_py"):
            payload = payload.to_py()

        try:
            batch = ingest.parse(payload)
        except ingest.IngestError as error:
            await self._log(payload.get("agent_id") if hasattr(payload, "get") else None,
                            error.status, error.detail)
            return _json({"error": error.detail}, error.status)

        now = _now()
        statements = [self.env.DB.prepare(ingest.AGENT_UPSERT).bind(batch.agent_id, now, now)]
        for row in batch.entitlements:
            statements.append(
                self.env.DB.prepare(ingest.ENTITLEMENT_UPSERT).bind(
                    *ingest.bind(row, ingest.ENTITLEMENT_COLUMNS))
            )
        for row in batch.usage:
            statements.append(
                self.env.DB.prepare(ingest.USAGE_UPSERT).bind(
                    *ingest.bind(row, ingest.USAGE_COLUMNS))
            )
        # One batch: a partial write would leave quota advancing against spend that was
        # never stored, which the estimator reads as unobserved usage and discards.
        await self.env.DB.batch(statements)
        await self._log(batch.agent_id, 200, None)

        return _json({
            "accepted": {"entitlements": len(batch.entitlements), "usage": len(batch.usage)},
            "cursor": batch.cursor,
        })

    async def fetch(self, request):
        path = _path(str(request.url))

        if request.method == "POST" and path == "/ingest":
            return await self._ingest(request)
        if request.method != "GET":
            return _json({"error": "method not allowed"}, 405)

        try:
            if path == "/api/current":
                return _json(queries.current_payload(await self._points()))
            if path == "/api/history":
                return _json(queries.history_payload(await self._points()))
            if path == "/api/models":
                return _json(queries.models_payload(await self._rows(queries.MODEL_MIX_SQL)))
            if path == "/api/weights":
                mix = await self._rows(queries.MODEL_MIX_SQL)
                return _json(queries.weights_payload(await self._points(), mix))
            if path == "/api/health":
                rows = await self._rows(queries.HEALTH_SQL)
                return _json({
                    "schema_version": ingest.SUPPORTED_SCHEMA_VERSION,
                    "now": _now(),
                    **(rows[0] if rows else {}),
                })
        except Exception as error:  # noqa: BLE001 - a read failure must not render a stale number
            return _json({"error": f"{type(error).__name__}: {error}"}, 503)

        return await self.env.ASSETS.fetch(request)

    async def scheduled(self, event):
        """Prune usage rows once their windows are long settled.

        Entitlement snapshots are never pruned: they are small, and an unsampled moment
        cannot be recovered. Usage rows can be re-pushed from any agent that still holds
        them, and storage is the binding constraint -- rows carry per-import metadata, so
        they cost roughly 3.6x their local footprint.
        """
        days = int(getattr(self.env, "USAGE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await self.env.DB.prepare("DELETE FROM usage_rows WHERE last_seen_at < ?").bind(cutoff).run()
        await self.env.DB.prepare("DELETE FROM ingest_log WHERE received_at < ?").bind(cutoff).run()
