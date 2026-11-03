"""How the database connection waits for another process's lock."""
from __future__ import annotations

from subbench.store import connect


def test_a_connection_waits_a_minute_for_another_writer(tmp_path):
    # The collector and a command such as `subbench doctor` both write (priced points).
    # A re-pricing can hold the lock for longer than SQLite's default 5 s, which made a
    # cycle fail with "database is locked" and crashed the 0.2 collector once.
    db = connect(tmp_path / "subbench.sqlite3")
    assert db.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
