import os

from subbench import config


def test_a_value_from_the_file_reaches_the_environment(tmp_path, monkeypatch):
    path = tmp_path / "push.env"
    path.write_text("SUBBENCH_PUSH_URL=https://example.invalid/ingest\n")
    monkeypatch.delenv("SUBBENCH_PUSH_URL", raising=False)
    assert config.load(path) == {"SUBBENCH_PUSH_URL": "https://example.invalid/ingest"}
    assert os.environ["SUBBENCH_PUSH_URL"] == "https://example.invalid/ingest"


def test_the_real_environment_always_wins(tmp_path, monkeypatch):
    """A value exported in a shell or set in the unit must not be overridden by the file."""
    path = tmp_path / "push.env"
    path.write_text("SUBBENCH_PUSH_URL=https://from-file.invalid\n")
    monkeypatch.setenv("SUBBENCH_PUSH_URL", "https://from-shell.invalid")
    assert config.load(path) == {}
    assert os.environ["SUBBENCH_PUSH_URL"] == "https://from-shell.invalid"


def test_a_missing_file_is_not_an_error(tmp_path):
    assert config.load(tmp_path / "absent.env") == {}


def test_the_file_is_read_the_way_systemd_reads_it():
    text = """
        # a comment
        SUBBENCH_PUSH_URL=https://example.invalid/ingest
        SUBBENCH_PLAN_CLAUDE = pro
        QUOTED="python3 /home/x/claude-usage.py"
        a line without an equals sign
        EMPTY=
    """
    assert config.parse(text) == {
        "SUBBENCH_PUSH_URL": "https://example.invalid/ingest",
        "SUBBENCH_PLAN_CLAUDE": "pro",
        "QUOTED": "python3 /home/x/claude-usage.py",
        "EMPTY": "",
    }


def test_the_path_honours_an_explicit_override(monkeypatch):
    monkeypatch.setenv(config.CONFIG_ENV, "/tmp/elsewhere.env")
    assert str(config.config_path()) == "/tmp/elsewhere.env"


def test_the_path_honours_xdg(monkeypatch):
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert str(config.config_path()) == "/tmp/xdg/subbench/push.env"
