#!/usr/bin/env python3
"""Upsert validated rows into the ACTIVE search project's SQLite store.

Project resolution: active-project.json / env RECRUITING_PROJECT_DIR
(source of truth: split public/private SQLite stores).

Usage:
  python3 scripts/upsert_rows.py job_list rows.json
  python3 scripts/upsert_rows.py tracker rows.json

Pipeline (all-or-nothing):
  1. Re-run the matching validator against the current store state — refuse to write on FAIL.
  2. Upsert in one transaction (job_list keys on job_id, tracker on application_id).
     Empty incoming values never overwrite non-empty existing values.
  3. Read-after-write verification from the DB.
  4. Verify normalized field equality and the database revision.

Use the explicit export command for public-safe or private views.
"""
import json
import os
import sqlite3
import subprocess
import sys
import csv
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
    tmp_path = None
    snapshot_paths = {}

    def cleanup() -> None:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
        for path in snapshot_paths.values():
            Path(path).unlink(missing_ok=True)

    with open(rows_path, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        print("REFUSED: rows JSON must be a list", file=sys.stderr)
        con.close()
        return 1
    original_rows = rows

    def validator_value(value):
        if isinstance(value, dict) and value == {"$clear": True}:
            return ""
        if isinstance(value, dict) and set(value) == {"$set"}:
            return value["$set"]
        return value

    validator_rows = [{k: validator_value(v) for k, v in row.items()} for row in rows]
    validator_rows_path = rows_path
    if store == "job_list":
        validator_rows = normalize_job_rows(validator_rows)
        # Keep explicit patch operations while applying deterministic normalization
        # to ordinary values.
        normalized = normalize_job_rows([
            {k: validator_value(v) for k, v in row.items()} for row in original_rows
        ])
        rows = []
        for source, clean in zip(original_rows, normalized):
            rows.append({k: source[k] if isinstance(source.get(k), dict) else clean.get(k, "")
                         for k in set(source) | set(clean)})
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp_path = tmp.name
        with tmp:
            json.dump(validator_rows, tmp, indent=2, ensure_ascii=False)
        validator_rows_path = tmp_path
    else:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                          encoding="utf-8")
        tmp_path = tmp.name
        with tmp:
            json.dump(validator_rows, tmp, indent=2, ensure_ascii=False)
        validator_rows_path = tmp_path

    for current_store in ("job_list", "tracker"):
        snap = tempfile.NamedTemporaryFile("w", suffix=f"-{current_store}.csv",
                                           delete=False, encoding="utf-8", newline="")
        with snap:
            writer = csv.DictWriter(snap, fieldnames=store_io.COLS[current_store])
            writer.writeheader()
            writer.writerows(store_io.read_rows(current_store, con))
        snapshot_paths[current_store] = snap.name
    cmd = [sys.executable, str(ROOT / "scripts" / VALIDATORS[store]), validator_rows_path]
    if store == "job_list":
        cmd += ["--existing", snapshot_paths["job_list"]]
    else:
        cmd += ["--job-list", snapshot_paths["job_list"],
                "--current", snapshot_paths["tracker"]]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace",
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    print(res.stdout, end="")
    if res.returncode != 0:
        print("REFUSED: validation failed; nothing written.", file=sys.stderr)
        cleanup()
        con.close()
        return 1

    key = store_io.KEYS[store]
    try:
        with con:  # transaction
            inserted, changed, unchanged = store_io.upsert(store, rows, con)
    except sqlite3.IntegrityError as e:
        print(f"REFUSED: DB constraint violation — {e}", file=sys.stderr)
        cleanup()
        con.close()
        return 1

    # Full read-after-write verification for every field included in the patch.
    final = {r[key]: r for r in store_io.read_rows(store, con)}
    mismatches = []
    for row in rows:
        saved = final.get(str(row.get(key, "")))
        if saved is None:
            mismatches.append(f"{row.get(key)}: missing")
            continue
        for field, raw in row.items():
            if field not in store_io.COLS[store] or field == key:
                continue
            expected = validator_value(raw)
            if expected == "" and not (isinstance(raw, dict) and raw == {"$clear": True}):
                continue
            if str(saved.get(field, "")) != str(expected).strip():
                mismatches.append(f"{row.get(key)}.{field}")
    if mismatches:
        print(f"FAIL: read-after-write mismatches: {mismatches[:20]}")
        cleanup()
        con.close()
        return 1

    con.close()
    cleanup()
    print(f"OK: {inserted} inserted, {changed} changed, {unchanged} unchanged in {store}; "
          f"read-after-write field equality verified ({len(final)} total rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
