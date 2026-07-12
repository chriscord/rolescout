#!/usr/bin/env python3
"""Validate tracker rows/updates before writing to the tracker store.

Usage:
  python3 scripts/validate_tracker_rows.py rows.json \
      [--job-list data/job_list.csv] [--current data/tracker.csv]

rows.json is a JSON list of dicts keyed by tracker column names.
Checks: required fields, status enum, ISO dates, application_id format,
job_id linkage to job_list (if provided), and legal status transitions
against --current (if provided). Exit 0 on pass, 1 on any error.
"""
import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from schema_defs import (TRACKER_COLUMNS, STATUSES, ALLOWED_TRANSITIONS,
                         ENTRY_STATUSES, OUTCOMES, ISO_DATE_RE)

REQUIRED = ["application_id", "job_id", "company", "title", "status", "last_updated"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rows_json")
    ap.add_argument("--job-list", help="CSV of job_list to verify job_id linkage")
    ap.add_argument("--current", help="CSV of current tracker to verify status transitions")
    args = ap.parse_args()

    with open(args.rows_json, encoding="utf-8") as f:
        rows = json.load(f)
    errors = []

    job_ids = None
    if args.job_list and Path(args.job_list).exists():
        with open(args.job_list, newline="", encoding="utf-8") as f:
            job_ids = {r.get("job_id") for r in csv.DictReader(f)}

    current = {}
    if args.current and Path(args.current).exists():
        with open(args.current, newline="", encoding="utf-8") as f:
            current = {r["application_id"]: r for r in csv.DictReader(f) if r.get("application_id")}

    seen = set()
    for i, row in enumerate(rows):
        for f in REQUIRED:
            if not str(row.get(f, "")).strip():
                errors.append(f"row {i}: missing required field '{f}'")
        unknown = set(row) - set(TRACKER_COLUMNS)
        if unknown:
            errors.append(f"row {i}: unknown columns {sorted(unknown)}")

        app_id = str(row.get("application_id", ""))
        if app_id in seen:
            errors.append(f"row {i}: duplicate application_id '{app_id}' in batch")
        seen.add(app_id)
        if app_id and not app_id.startswith("app--"):
            errors.append(f"row {i}: application_id '{app_id}' must be 'app--<job_id>'")

        status = str(row.get("status", "")).strip()
        if status and status not in STATUSES:
            errors.append(f"row {i}: status '{status}' not in enum {STATUSES}")

        for f in ("applied_at", "next_action_due", "last_updated"):
            v = str(row.get(f, "")).strip()
            if v:
                try:
                    date.fromisoformat(v)
                except ValueError:
                    errors.append(f"row {i}: {f}='{v}' is not a real YYYY-MM-DD date")

        v = str(row.get("outcome", "")).strip()
        if v not in OUTCOMES:
            errors.append(f"row {i}: outcome='{v}' not in {sorted(o for o in OUTCOMES if o)}")

        if job_ids is not None and row.get("job_id") not in job_ids:
            errors.append(f"row {i}: job_id '{row.get('job_id')}' not found in job_list")

        if status in STATUSES:
            prev = current.get(app_id)
            if prev:
                prev_status = prev.get("status", "")
                if status != prev_status and status not in ALLOWED_TRANSITIONS.get(prev_status, set()):
                    errors.append(
                        f"row {i}: illegal transition '{prev_status}' -> '{status}' "
                        f"(allowed: {sorted(ALLOWED_TRANSITIONS.get(prev_status, set()))}); "
                        "requires explicit user confirmation + note")
            elif current and status not in ENTRY_STATUSES:
                errors.append(
                    f"row {i}: new tracker row must enter at {sorted(ENTRY_STATUSES)}, got '{status}'")

    if errors:
        print(f"FAIL: {len(errors)} error(s) in {len(rows)} row(s)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PASS: {len(rows)} row(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
