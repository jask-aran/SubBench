"""Check that an upgrade kept the estimates: the stored report before it against the one after.

The old collector stored its latest derived report in the `reports` table, so a backup
taken before upgrading holds every window estimate as the old code last computed it. After
the new collector has run once, the live database holds the same report computed by the new
code from the same readings. A window that closed before the backup was taken has the same
readings in both, so its estimate should agree; this lists any that do not.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .push import VALUE_REPORT_KIND

FIELDS = ("estimate_usd", "lower_usd", "upper_usd")


def _report(path: Path) -> tuple[str, list[dict[str, Any]]] | None:
    import json

    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        row = db.execute("SELECT generated_at, payload FROM reports WHERE kind = ?", (VALUE_REPORT_KIND,)).fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        db.close()
    if row is None:
        return None
    return str(row[0]), list(json.loads(row[1]).get("windows", []))


def _key(row: dict[str, Any]) -> tuple:
    return (row.get("provider"), row.get("window"), row.get("account_id"), row.get("plan"), str(row.get("reset_key")))


def _moment(value: Any) -> datetime | None:
    # A derived window's key carries how it was derived after "~" ("...~via~five_hour").
    try:
        return datetime.fromisoformat(str(value).split("~", 1)[0].replace("Z", "+00:00"))
    except ValueError:
        return None


def compare(before: Path, after: Path, *, tolerance: float = 0.01) -> dict[str, Any]:
    old = _report(before)
    new = _report(after)
    if old is None:
        raise SystemExit(f"{before} holds no stored report to compare against")
    if new is None:
        raise SystemExit(f"{after} holds no stored report yet; run `subbench watch --once` first")
    taken_at, old_rows = old
    _, new_rows = new
    cutoff = _moment(taken_at)
    current = {_key(row): row for row in new_rows}
    closed, still_open, missing, changed = 0, 0, [], []
    for row in old_rows:
        reset = _moment(row.get("reset_key"))
        if cutoff is None or reset is None or reset > cutoff:
            still_open += 1  # still running when the backup was taken: it has gained readings since
            continue
        closed += 1
        match = current.get(_key(row))
        if match is None:
            missing.append(row)
            continue
        drift = max(
            abs(float(match[f]) - float(row[f])) / max(abs(float(row[f])), 1e-9)
            for f in FIELDS if row.get(f) is not None and match.get(f) is not None
        ) if any(row.get(f) is not None for f in FIELDS) else 0.0
        if drift > tolerance:
            changed.append((row, match, drift))
    return {"taken_at": taken_at, "closed": closed, "open": still_open, "missing": missing,
            "changed": changed, "new_windows": len(new_rows)}


def run(before: Path, after: Path, *, tolerance: float) -> int:
    result = compare(before, after, tolerance=tolerance)
    closed, missing, changed = result["closed"], result["missing"], result["changed"]
    print(f"backup report computed {result['taken_at']}")
    print(f"{closed} window(s) closed before then, {result['open']} still open (skipped)")
    print(f"{closed - len(missing) - len(changed)} unchanged within {tolerance:.0%}, "
          f"{len(changed)} changed, {len(missing)} missing")
    for row in missing:
        print(f"  missing  {row.get('provider')} {row.get('window')} {row.get('plan')} reset {row.get('reset_key')}")
    for old, new, drift in sorted(changed, key=lambda item: -item[2]):
        print(f"  changed  {old.get('provider')} {old.get('window')} {old.get('plan')} reset {old.get('reset_key')}: "
              f"${float(old['estimate_usd']):.2f} -> ${float(new['estimate_usd']):.2f} ({drift:+.1%})")
    print(f"the new report holds {result['new_windows']} window(s) in all")
    return 1 if missing else 0
