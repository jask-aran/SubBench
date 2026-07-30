"""Cloudflare Python Worker entry point.

Deliberately thin. Everything that decides anything lives in `ingest`, `queries` and
`confidence`, which are synchronous and tested against SQLite; this module only moves
bytes between D1 and those functions. Keeping the runtime boundary this narrow is what
makes moving to Containers a redeploy rather than a rewrite.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from js import Response  # type: ignore[import-not-found]  # provided by the Workers runtime
from workers import handler  # type: ignore[import-not-found]

from . import ingest, queries

STATIC_PAGE = "index.html"
JSON_HEADERS = {"content-type": "application/json; charset=utf-8"}


def _json(payload, status: int = 200) -> "Response":
    return Response.new(json.dumps(payload), status=status, headers=JSON_HEADERS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _rows(env, sql: str, params: list) -> list[dict]:
    statement = env.DB.prepare(sql)
    if params:
        statement = statement.bind(*params)
    result = await statement.all()
    return [dict(row) for row in result.results.to_py()]


async def _agent_id(env) -> str | None:
    """The single agent's id.

    Multi-agent aggregation is deliberately not implemented: pooled estimates must be
    computed over combined pairs, which cannot be reconstructed from per-agent summaries,
    so it is a design change rather than a query change. Until then the newest agent wins.
    """
    rows = await _rows(env, "SELECT agent_id FROM agents ORDER BY last_seen DESC LIMIT 1", [])
    return rows[0]["agent_id"] if rows else None


async def _points(env) -> list[dict]:
    agent_id = await _agent_id(env)
    if agent_id is None:
        return []
    return await _rows(env, queries.REGRESSION_POINTS_SQL, queries.points_params(agent_id))


async def _ingest(request, env) -> "Response":
    expected = env.SUBBENCH_INGEST_TOKEN
    supplied = request.headers.get("authorization") or ""
    if not expected or supplied != f"Bearer {expected}":
        return _json({"error": "unauthorized"}, 401)

    try:
        payload = json.loads(await request.text())
    except ValueError:
        return _json({"error": "body must be JSON"}, 400)

    try:
        batch = ingest.parse(payload)
    except ingest.IngestError as error:
        await _log(env, payload.get("agent_id"), error.status, error.detail)
        return _json({"error": error.detail}, error.status)

    now = _now()
    statements = [env.DB.prepare(ingest.AGENT_UPSERT).bind(batch.agent_id, now, now)]
    for row in batch.entitlements:
        statements.append(
            env.DB.prepare(ingest.ENTITLEMENT_UPSERT).bind(*ingest.bind(row, ingest.ENTITLEMENT_COLUMNS))
        )
    for row in batch.usage:
        statements.append(
            env.DB.prepare(ingest.USAGE_UPSERT).bind(*ingest.bind(row, ingest.USAGE_COLUMNS))
        )
    # One batch: a partial write would leave quota advancing against spend that was never
    # stored, which the estimator would read as unobserved usage and discard.
    await env.DB.batch(statements)
    await _log(env, batch.agent_id, 200, None)

    return _json({
        "accepted": {"entitlements": len(batch.entitlements), "usage": len(batch.usage)},
        "cursor": batch.cursor,
    })


async def _log(env, agent_id, status: int, detail) -> None:
    await env.DB.prepare(
        "INSERT INTO ingest_log (received_at, agent_id, status, detail) VALUES (?, ?, ?, ?)"
    ).bind(_now(), agent_id, status, detail).run()


@handler
async def on_fetch(request, env):
    url = str(request.url)
    path = "/" + url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0] if "://" in url else "/"
    path = path.rstrip("/") or "/"

    if request.method == "POST" and path == "/ingest":
        return await _ingest(request, env)

    if request.method != "GET":
        return _json({"error": "method not allowed"}, 405)

    try:
        if path == "/api/current":
            return _json(queries.current_payload(await _points(env)))
        if path == "/api/history":
            return _json(queries.history_payload(await _points(env)))
        if path == "/api/models":
            agent_id = await _agent_id(env)
            rows = await _rows(env, queries.MODEL_MIX_SQL, [agent_id]) if agent_id else []
            return _json(queries.models_payload(rows))
        if path == "/api/health":
            rows = await _rows(env, queries.HEALTH_SQL, [])
            return _json({
                "schema_version": ingest.SUPPORTED_SCHEMA_VERSION,
                "now": _now(),
                **(rows[0] if rows else {}),
            })
    except Exception as error:  # noqa: BLE001 - a read failure must not render a stale number
        return _json({"error": f"{type(error).__name__}: {error}"}, 503)

    return env.ASSETS.fetch(request)
