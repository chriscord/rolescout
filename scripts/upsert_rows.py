#!/usr/bin/env python3
"""Upsert validated rows into the ACTIVE search project's SQLite store.

Project resolution: active-project.json / env RECRUITING_PROJECT_DIR
(source of truth: <project>/data/recruiting.db).

Usage:
  python3 scripts/upsert_rows.py job_list rows.json
  python3 scripts/upsert_rows.py tracker rows.json

Pipeline (all-or-nothing):
  1. Re-run the matching validator against the current store state — refuse to write on FAIL.
  2. Upsert in one transaction (job_list keys on job_id, tracker on application_id).
     Empty incoming values never overwrite non-empty existing values.
  3. Read-after-write verification from the DB.
  4. Regenerate views: <project>/data/*.csv mirrors + recruiting-pipeline.xlsx.

Never edit the CSV mirrors or xlsx by hand — they are overwritten on every write.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from location_normalize import normalize_job_rows
import store_io

ROOT = store_io.ROOT
VALIDATORS = {"job_list": "validate_job_rows.py", "tracker": "validate_tracker_rows.py"}


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("job_list", "tracker"):
        print(__doc__)
        return 1
    store, rows_path = sys.argv[1], sys.argv[2]

    con = store_io.connect()
    store_io.export_views(con)  # ensure mirrors reflect current DB before validating

    with open(rows_path, encoding="utf-8") as f:
        rows = json.load(f)
    tmp_path = None
    validator_rows_path = rows_path
    if store == "job_list":
        rows = normalize_job_rows(rows)
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp_path = tmp.name
        with tmp:
            json.dump(rows, tmp, indent=2, ensure_ascii=False)
        validator_rows_path = tmp_path

    m = store_io.mirrors()
    cmd = [sys.executable, str(ROOT / "scripts" / VALIDATORS[store]), validator_rows_path]
    if store == "job_list":
        cmd += ["--existing", str(m["job_list"])]
    else:
        cmd += ["--job-list", str(m["job_list"]), "--current", str(m["tracker"])]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    print(res.stdout, end="")
    if res.returncode != 0:
        print("REFUSED: validation failed; nothing written.", file=sys.stderr)
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        con.close()
        return 1

    key = store_io.KEYS[store]
    try:
        with con:  # transaction
            appended, updated = store_io.upsert(store, rows, con)
    except sqlite3.IntegrityError as e:
        print(f"REFUSED: DB constraint violation — {e}", file=sys.stderr)
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        con.close()
        return 1

    # read-after-write verification
    final = {r[key] for r in store_io.read_rows(store, con)}
    missing = [row.get(key) for row in rows if row.get(key) not in final]
    if missing:
        print(f"FAIL: read-after-write check missing keys: {missing}")
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        con.close()
        return 1

    store_io.export_views(con)
    con.close()
    if tmp_path:
        Path(tmp_path).unlink(missing_ok=True)
    print(f"OK: {appended} appended, {updated} updated in {store}; "
          f"read-after-write verified ({len(final)} total rows); views regenerated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
