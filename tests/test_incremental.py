from pathlib import Path

from subbench.incremental import LogChangeDetector


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
