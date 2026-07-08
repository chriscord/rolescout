#!/usr/bin/env python3
"""Validate job rows before writing to the job_list store (Sheet tab or data/job_list.csv).

Usage:
  python3 scripts/validate_job_rows.py rows.json [--existing data/job_list.csv]

rows.json is a JSON list of dicts keyed by job_list column names.
Checks: required fields, ISO dates, URL shape, enums, fit_score range,
unknown columns, duplicate job_id within the batch and against --existing.
Prints a report; exit 0 if all rows pass, 1 otherwise.
"""
import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from location_normalize import normalize_location_value
from schema_defs import (JOB_LIST_COLUMNS, REMOTE_POLICIES, POSTING_STATUSES,
                         PRIORITIES, ISO_DATE_RE)

REQUIRED = ["job_id", "captured_at", "company", "title", "source_url"]


def validate_row(row: dict, idx: int) -> list:
    errs = []
    for f in REQUIRED:
        if not str(row.get(f, "")).strip():
            errs.append(f"row {idx}: missing required field '{f}'")
    unknown = set(row) - set(JOB_LIST_COLUMNS)
    if unknown:
        errs.append(f"row {idx}: unknown columns {sorted(unknown)}")
    for f in ("captured_at", "last_seen_at"):
        v = str(row.get(f, "")).strip()
        if v and not re.match(ISO_DATE_RE, v):
            errs.append(f"row {idx}: {f}='{v}' is not YYYY-MM-DD")
    for f in ("source_url", "job_page_url"):
        v = str(row.get(f, "")).strip()
        if v and not re.match(r"^https?://", v):
            errs.append(f"row {idx}: {f}='{v}' is not an http(s) URL")
    loc = str(row.get("location", "")).strip()
    if loc:
        norm_loc = normalize_location_value(loc)
        if norm_loc != loc:
            errs.append(f"row {idx}: location must be normalized as '{norm_loc}' "
                        f"(got '{loc}')")
    v = str(row.get("posting_status", "")).strip()
    if v and v not in POSTING_STATUSES:
        errs.append(f"row {idx}: posting_status='{v}' not in {sorted(POSTING_STATUSES)}")
    v = str(row.get("remote_policy", "")).strip()
    if v and v not in REMOTE_POLICIES:
        errs.append(f"row {idx}: remote_policy='{v}' not in {sorted(REMOTE_POLICIES)}")
    v = str(row.get("priority", "")).strip()
    if v not in PRIORITIES:
        errs.append(f"row {idx}: priority='{v}' not in {sorted(p for p in PRIORITIES if p)}")
    v = str(row.get("fit_score", "")).strip()
    if v:
        if not v.isdigit() or not 1 <= int(v) <= 5:
            errs.append(f"row {idx}: fit_score='{v}' must be integer 1-5 or empty")
    jid = str(row.get("job_id", ""))
    if jid and not re.match(r"^[a-z0-9-]+--[a-z0-9-]+--[0-9a-f]{8}$", jid):
        errs.append(f"row {idx}: job_id='{jid}' does not match <company>--<title>--<hash8> "
                    "(generate with scripts/normalize_job_url.py)")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rows_json")
    ap.add_argument("--existing", help="CSV of current job_list to check dedupe against")
    args = ap.parse_args()

    with open(args.rows_json, encoding="utf-8") as f:
        rows = json.load(f)
    if not isinstance(rows, list):
        print("ERROR: input must be a JSON list of row objects", file=sys.stderr)
        return 1

    errors = []
    seen = {}
    for i, row in enumerate(rows):
        errors.extend(validate_row(row, i))
        jid = row.get("job_id", "")
        if jid in seen:
            errors.append(f"row {i}: duplicate job_id '{jid}' (also row {seen[jid]})")
        seen[jid] = i

    if args.existing and Path(args.existing).exists():
        with open(args.existing, newline="", encoding="utf-8") as f:
            existing_ids = {r.get("job_id") for r in csv.DictReader(f)}
        dupes = [jid for jid in seen if jid in existing_ids]
        if dupes:
            print(f"NOTE: {len(dupes)} row(s) already exist and will be UPSERTED "
                  f"(not appended): {dupes}")

    if errors:
        print(f"FAIL: {len(errors)} error(s) in {len(rows)} row(s)")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"PASS: {len(rows)} row(s) valid")
    return 0


if __name__ == "__main__":
    sys.exit(main())
