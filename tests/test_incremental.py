from pathlib import Path

from subbench.incremental import AuthFileDetector, LogChangeDetector


class _FakeStat:
    def __init__(self, size: int, mtime_ns: int) -> None:
        self.st_size = size
        self.st_mtime_ns = mtime_ns


def test_detector_reports_startup_then_only_changes(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "session.jsonl"
    log.write_text("one\n")
    monkeypatch.setattr("subbench.incremental.discover_logs", lambda provider: [log])

    detector = LogChangeDetector("codex")
    assert detector.scan() is True
    assert detector.scan() is False

    log.write_text("one\ntwo\n")
    assert detector.scan() is True
    assert detector.scan() is False


def test_detector_notices_new_files(tmp_path: Path, monkeypatch) -> None:
    files: list[Path] = []
    monkeypatch.setattr("subbench.incremental.discover_logs", lambda provider: list(files))
    detector = LogChangeDetector("claude")
    assert detector.scan() is True
    assert detector.scan() is False

    log = tmp_path / "new.jsonl"
    log.write_text("{}\n")
    files.append(log)
    assert detector.scan() is True


def test_auth_detector_reports_switch_for_codex(tmp_path: Path, monkeypatch) -> None:
    auth_path = tmp_path / "auth.json"
    registry_path = tmp_path / "registry.json"
    auth_path.write_text("{}")
    registry_path.write_text("{}")
    monkeypatch.setattr("subbench.incremental.auth_file", lambda: auth_path)
    monkeypatch.setattr("subbench.incremental.registry_file", lambda: registry_path)

    detector = AuthFileDetector("codex")
    assert detector.scan() is False  # establish baseline without flagging startup
    auth_path.write_text('{"tokens": {"account_id": "A"}}')  # rewrite changes mtime/size
    assert detector.scan() is True
    assert detector.scan() is False


def test_auth_detector_ignored_for_non_codex(tmp_path: Path) -> None:
    detector = AuthFileDetector("claude")
    assert detector.scan() is False
    assert detector.scan() is False
