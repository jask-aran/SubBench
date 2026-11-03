"""Privacy guarantees, as tests: what leaves the machine, where, and what never does."""
from __future__ import annotations

import ast
import base64
import gzip
import json
from pathlib import Path

import pytest

from subbench import net, privacy
from subbench.store import connect, upsert_account
from subbench.account import Account

from test_pipeline import _record

ACCOUNT = "7f3c2a10-0d4e-4b7a-9c1e-5a6b7c8d9e0f"
EMAIL = "someone@example.com"
SRC = Path(__file__).resolve().parents[1] / "src" / "subbench"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("SUBBENCH_EGRESS_LOG", str(tmp_path / "egress.jsonl"))
    database = connect(tmp_path / "subbench.sqlite3")
    for hour, (used, cost) in enumerate([(10.0, 1.0), (30.0, 5.0), (60.0, 11.0)]):
        _record(database, timestamp=f"2026-07-01T0{hour}:00:00+00:00", used=used, cost=cost, account_id=ACCOUNT)
    upsert_account(database, Account(account_id=ACCOUNT, alias="work", email=EMAIL, plan="plus"))
    database.commit()
    return database


def test_a_push_never_carries_account_ids_or_emails(db, monkeypatch):
    monkeypatch.setenv("SUBBENCH_PUSH_RAW_USAGE", "1")  # the widest payload there is
    text = json.dumps(privacy.preview(db, "https://example.test/ingest"))
    assert ACCOUNT not in text and ACCOUNT[:8] not in text
    assert EMAIL not in text and '"work"' not in text
    assert privacy.pseudonym(ACCOUNT) in text


def test_what_is_sent_is_what_preview_shows(db, monkeypatch):
    from subbench.push import push_once

    sent = {}
    push_once(db, url="https://example.test/ingest", token="t",
              sender=lambda url, token, payload, timeout: sent.update(payload=payload) or {})
    assert ACCOUNT not in json.dumps(sent["payload"])
    # The Worker stores gzipped reports unopened, so the collector's hashing is the only
    # guard for them: open every one and check it too.
    opened = [gzip.decompress(base64.b64decode(text)).decode() for text in sent["payload"]["report_gz"].values()]
    assert opened and all(ACCOUNT not in text and ACCOUNT[:8] not in text and EMAIL not in text for text in opened)
    assert any(privacy.pseudonym(ACCOUNT) in text for text in opened)


def test_pseudonyms_are_stable_and_distinct():
    assert privacy.pseudonym(ACCOUNT) == privacy.pseudonym(ACCOUNT)
    assert privacy.pseudonym(ACCOUNT) != privacy.pseudonym(ACCOUNT[:-1] + "1")
    assert privacy.pseudonymise({"a": [{"account_id": ACCOUNT}], "account_id": None}) == {
        "a": [{"account_id": privacy.pseudonym(ACCOUNT)}], "account_id": None}


def test_only_named_hosts_are_reachable(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBBENCH_EGRESS_LOG", str(tmp_path / "egress.jsonl"))
    monkeypatch.delenv("SUBBENCH_PUSH_URL", raising=False)
    with pytest.raises(net.Refused, match="not one of"):
        net.request("https://evil.example/collect", purpose="test")
    with pytest.raises(net.Refused, match="non-HTTPS"):
        net.request("http://api.anthropic.com/api/oauth/usage", purpose="test")


def test_a_login_goes_only_to_its_own_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("SUBBENCH_EGRESS_LOG", str(tmp_path / "egress.jsonl"))
    with pytest.raises(net.Refused, match="claude login"):
        net.request("https://chatgpt.com/backend-api/wham/usage", purpose="test", credential="claude")
    with pytest.raises(net.Refused, match="codex login"):
        net.request("https://raw.githubusercontent.com/x", purpose="test", credential="codex")


def test_every_request_is_recorded_without_its_body(monkeypatch, tmp_path):
    log = tmp_path / "egress.jsonl"
    monkeypatch.setenv("SUBBENCH_EGRESS_LOG", str(log))

    class Response:
        status = 200
        def read(self): return b"{}"
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(net.urllib.request, "urlopen", lambda request, timeout: Response())
    net.request("https://api.anthropic.com/api/oauth/usage", purpose="claude quota", credential="claude",
                headers={"Authorization": "Bearer secret-token"}, body=b"secret-body")
    entry = json.loads(log.read_text())
    assert entry["host"] == "api.anthropic.com" and entry["sent_bytes"] == 11 and entry["status"] == 200
    assert "secret" not in log.read_text()


def test_no_module_but_net_opens_a_connection():
    """The whole network surface is net.py. Anything else importing a client fails this."""
    clients = {"urllib.request", "http.client", "socket", "requests", "httpx", "urllib3", "ssl"}
    offenders = []
    for path in SRC.rglob("*.py"):
        if path.name == "net.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            offenders += [f"{path.relative_to(SRC)}: {n}" for n in names if n in clients]
    assert offenders == []


def test_logs_skip_lines_that_cannot_carry_usage(tmp_path, monkeypatch):
    from subbench import logs

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    project = tmp_path / ".claude" / "projects" / "p"
    project.mkdir(parents=True)
    # A prompt line that is not even valid JSON: had it been parsed, it would be skipped
    # anyway; it is never decoded because it lacks the usage marker.
    (project / "s.jsonl").write_text('{"type":"user","message":{"content":"my secret prompt"\n')
    from subbench.prices import Prices, _bundled

    reader = logs.LogReader("claude", Prices(_bundled()))
    parsed = []
    monkeypatch.setattr(logs.json, "loads", lambda line: parsed.append(line) or {})
    reader.entries()
    assert parsed == []


def test_server_migration_sql_rewrites_every_table(db):
    sql = privacy.migration_sql(privacy.local_account_ids(db))
    hashed = privacy.pseudonym(ACCOUNT)
    for table in ("entitlement_snapshots", "usage_rows", "reports", "ingest_log"):
        assert f"UPDATE" in sql and table in sql
    assert sql.count(hashed) == 6 and privacy.migration_sql([hashed]).count("UPDATE") == 0
