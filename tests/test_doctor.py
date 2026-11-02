from pathlib import Path

from subbench.doctor import Check, exit_code, run_doctor
from subbench.store import connect


def test_doctor_reports_database_and_missing_observations(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "subbench.sqlite3"
    db = connect(database)
    monkeypatch.setattr("subbench.doctor.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("subbench.doctor.discover_logs", lambda provider: iter(()))
    monkeypatch.setattr("subbench.doctor.subprocess.run", lambda *args, **kwargs: type("Result", (), {"stdout": "tool 1.0", "stderr": ""})())
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex" / "auth.json").write_text('{"tokens": {"access_token": "a.e30.b", "account_id": "acct"}}')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))

    checks = run_doctor(db, database, ("codex",))
    names = {check.name for check in checks}
    assert "database" in names
    assert "codex logs" in names
    assert "codex latest usage" in names
    assert exit_code(checks) == 0


def test_doctor_exit_code_only_fails_on_errors() -> None:
    assert exit_code([Check("x", "warn", "missing")]) == 0
    assert exit_code([Check("x", "error", "broken")]) == 1


def test_doctor_fails_without_a_login(tmp_path: Path, monkeypatch) -> None:
    db = connect(tmp_path / "subbench.sqlite3")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "nowhere"))
    monkeypatch.setattr("subbench.doctor.discover_logs", lambda provider: iter(()))
    checks = run_doctor(db, tmp_path / "subbench.sqlite3", ("codex",))
    login = next(check for check in checks if check.name == "codex login")
    assert login.status == "error" and "codex login" in login.detail
    assert exit_code(checks) == 1
