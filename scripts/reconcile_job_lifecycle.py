#!/usr/bin/env python3
"""Reconcile job open/removed state after provider enumeration.

Only a complete, authoritative source scan may mark an unseen posting removed.
Failed, blocked, rate-limited, or truncated scans update source diagnostics but
never close previously captured jobs. Preference filtering is intentionally
outside this lifecycle and cannot delete or close a job.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import store_io


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reconcile(manifest: dict) -> dict:
    run_id = str(manifest.get("run_id", "")).strip()
    sources = manifest.get("sources", [])
    if not run_id or not isinstance(sources, list):
        raise ValueError("manifest requires run_id and sources[]")
    con = store_io.connect()
    reopened: set[str] = set()
    removed: set[str] = set()
    affected: set[str] = set()
    now = _now()
    try:
        with con:
            existing_ids = {row[0] for row in con.execute("SELECT job_id FROM job_list")}
            for source in sources:
                if not isinstance(source, dict):
                    continue
                source_key = str(source.get("source_key", "")).strip()
                if not source_key:
                    continue
                company = str(source.get("company", "")).strip()
                provider = str(source.get("provider", "")).strip()
                source_url = str(source.get("source_url", "")).strip()
                scan_status = str(source.get("scan_status", "")).strip()
                authoritative = bool(source.get("authoritative", False))
                seen_ids = {
                    str(item).strip() for item in source.get("seen_job_ids", [])
                    if str(item).strip() in existing_ids
                }
                con.execute(
                    "INSERT INTO source_scan_state(source_key,company,provider,source_url,"
                    "last_run_id,last_checked_at,scan_status,authoritative) VALUES(?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(source_key) DO UPDATE SET company=excluded.company,"
                    "provider=excluded.provider,source_url=excluded.source_url,"
                    "last_run_id=excluded.last_run_id,last_checked_at=excluded.last_checked_at,"
                    "scan_status=excluded.scan_status,authoritative=excluded.authoritative",
                    (source_key, company, provider, source_url, run_id, now, scan_status,
                     1 if authoritative else 0),
                )
                for job_id in seen_ids:
                    prior = con.execute(
                        "SELECT source_status FROM job_source_state WHERE job_id=? AND source_key=?",
                        (job_id, source_key),
                    ).fetchone()
                    if prior and prior[0] == "removed":
                        reopened.add(job_id)
                    con.execute(
                        "INSERT INTO job_source_state(job_id,source_key,company,provider,source_url,"
                        "first_seen_at,last_seen_at,last_checked_at,last_seen_run_id,source_status) "
                        "VALUES(?,?,?,?,?,?,?,?,?,'open') "
                        "ON CONFLICT(job_id,source_key) DO UPDATE SET company=excluded.company,"
                        "provider=excluded.provider,source_url=excluded.source_url,"
                        "last_seen_at=excluded.last_seen_at,last_checked_at=excluded.last_checked_at,"
                        "last_seen_run_id=excluded.last_seen_run_id,source_status='open'",
                        (job_id, source_key, company, provider, source_url, now, now, now, run_id),
                    )
                    affected.add(job_id)
                if authoritative and scan_status in {"scanned", "no_match"}:
                    prior_open = {
                        row[0] for row in con.execute(
                            "SELECT job_id FROM job_source_state "
                            "WHERE source_key=? AND source_status='open'", (source_key,)
                    )}
                    missing = prior_open - seen_ids
                    if missing:
                        con.executemany(
                            "UPDATE job_source_state SET source_status='removed',last_checked_at=? "
                            "WHERE job_id=? AND source_key=?",
                            [(now, job_id, source_key) for job_id in sorted(missing)],
                        )
                        removed.update(missing)
                        affected.update(missing)

            status_changed = 0
            for job_id in affected:
                any_open = con.execute(
                    "SELECT 1 FROM job_source_state WHERE job_id=? AND source_status='open' LIMIT 1",
                    (job_id,),
                ).fetchone()
                desired = "open" if any_open else "removed"
                current = con.execute(
                    "SELECT posting_status FROM job_list WHERE job_id=?", (job_id,)
                ).fetchone()
                if current and current[0] != desired:
                    con.execute("UPDATE job_list SET posting_status=? WHERE job_id=?",
                                (desired, job_id))
                    status_changed += 1
            if status_changed:
                con.execute("UPDATE export_meta SET revision=revision+1 WHERE name='job_list'")
        return {
            "sources": len([s for s in sources if isinstance(s, dict)]),
            "affected_jobs": len(affected),
            "removed_candidates": len(removed),
            "reopened_candidates": len(reopened),
            "status_changed": status_changed,
        }
    finally:
        con.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile source-backed job lifecycle state.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--project", type=Path, required=True)
    args = parser.parse_args(argv)
    os.environ["RECRUITING_PROJECT_DIR"] = str(args.project.resolve())
    try:
        payload = json.loads(args.manifest.read_text(encoding="utf-8"))
        print(json.dumps(reconcile(payload), ensure_ascii=False))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
