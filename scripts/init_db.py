#!/usr/bin/env python3
"""Initialize (or verify) the SQLite store and import any existing CSV data.

Operates on the ACTIVE search project (active-project.json / RECRUITING_PROJECT_DIR).

Usage:
  python3 scripts/init_db.py            # create/verify split public/private stores
  python3 scripts/init_db.py --check    # verify DB exists with expected tables/columns

Import is upsert-based and idempotent: rerunning never duplicates rows.
"""
import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store_io


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    if args.check and not store_io.db_path().exists():
        print(f"FAIL: {store_io.db_path()} missing (run scripts/init_db.py)")
        return 1
    print(f"Project: {store_io.project_dir().name}")

    con = store_io.connect()
    status = 0
    for store, cols in store_io.COLS.items():
        pragma = f"PRAGMA table_info({store})" if store == "job_list" else "PRAGMA private.table_info(tracker)"
        got = [r[1] for r in con.execute(pragma)]
        if got != cols:
            print(f"FAIL: table {store} columns do not match schema.\n  expected: {cols}\n  got:      {got}")
            status = 1
        else:
            n = con.execute(f"SELECT COUNT(*) FROM {store}").fetchone()[0]
            print(f"OK: table {store} ({len(cols)} columns, {n} rows)")

    if not args.check and status == 0:
        # import existing CSV mirrors (job_list first — tracker has FK on it)
        for store in ("job_list", "tracker"):
            path = store_io.mirrors()[store]
            if path.exists():
                with open(path, newline="", encoding="utf-8") as f:
                    rows = [r for r in csv.DictReader(f) if any(v.strip() for v in r.values())]
                if rows:
                    inserted, changed, unchanged = store_io.upsert(store, rows, con)
                    print(f"IMPORTED {path.name}: {inserted} inserted, {changed} changed, "
                          f"{unchanged} unchanged")
        con.commit()
        print(f"Stores ready: {store_io.public_db_path().name}; {store_io.private_db_path().name}")
    con.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
