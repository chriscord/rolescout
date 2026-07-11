#!/usr/bin/env python3
"""Build deterministic per-position context scaffolds for prep-interview.

The script intentionally does not classify a company into a hard-coded industry.
It gathers local RoleScout evidence and emits web-search prompts the interview
agent must use to fill a researched industry thesis before drafting The Whys.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from jd_text_cleaner import clean_jd_text


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _focused_ids(project: Path) -> list[str]:
    data = _load_json(project / "data" / "focused-jobs.json")
    ids = data.get("job_ids")
    return [str(job_id) for job_id in ids] if isinstance(ids, list) else []


def _job_rows(project: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    csv_path = project / "data" / "job_list.csv"
    if not csv_path.exists():
        return rows
    with csv_path.open("r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            job_id = str(row.get("job_id", "") or "").strip()
            if job_id:
                rows[job_id] = dict(row)
    return rows


def _research_candidates(project: Path) -> dict[str, dict]:
    data = _load_json(project / "targets" / "research-log.json")
    out: dict[str, dict] = {}
    for candidate in data.get("candidates", []) if isinstance(data, dict) else []:
        if not isinstance(candidate, dict):
            continue
        job_id = str(candidate.get("job_id") or candidate.get("id") or "").strip()
        if job_id and job_id not in out:
            out[job_id] = candidate
    return out


def _business_arm(title: str) -> str:
    title = str(title or "").strip()
    if not title:
        return ""
    parts = [p.strip() for p in re.split(r"\s[-–—]\s|,\s*", title) if p.strip()]
    if len(parts) <= 1:
        return ""
    tails = [
        p for p in parts[1:]
        if not re.fullmatch(r"(?i)(senior|lead|manager|principal|staff|strategy|operations|business development|finance|partnerships)", p)
    ]
    return "; ".join(tails[-2:]) if tails else parts[-1]


def _compact(value, limit: int = 700) -> str:
    if isinstance(value, list):
        value = "; ".join(str(v) for v in value)
    text = re.sub(r"\s+", " ", clean_jd_text(str(value or ""))).strip()
    return text[:limit]


def _role_context(job_id: str, row: dict, candidate: dict) -> dict:
    company = _compact(row.get("company") or candidate.get("company"), 120)
    title = _compact(row.get("title") or candidate.get("title"), 180)
    arm = _business_arm(title)
    query_arm = arm or title
    jd_summary = _compact(row.get("jd_summary") or candidate.get("jd_summary"), 900)
    must_have = _compact(row.get("must_have_requirements") or candidate.get("must_have_requirements"), 900)
    source_url = _compact(row.get("job_page_url") or row.get("source_url") or candidate.get("url") or candidate.get("source_url"), 400)
    queries = [
        f'"{company}" "{query_arm}" industry business model products',
        f'"{company}" "{query_arm}" customers users market',
        f'"{company}" "{query_arm}" strategy interview why this industry',
    ]
    return {
        "job_id": job_id,
        "company": company,
        "title": title,
        "business_arm": arm,
        "job_group": _compact(row.get("job_group") or candidate.get("job_group"), 120),
        "location": _compact(row.get("location") or candidate.get("location"), 180),
        "source_url": source_url,
        "local_evidence": {
            "jd_summary": jd_summary,
            "must_have_requirements": must_have,
            "research_notes": _compact(candidate.get("reason") or candidate.get("notes"), 900),
        },
        "web_search_queries": queries,
        "industry_thesis": {
            "status": "research_required",
            "industry": "",
            "business_model": "",
            "customer_or_user": "",
            "market_tension": "",
            "candidate_bridge": "",
            "sources": [],
            "instruction": (
                "Fill after web-searching the company and business arm. The "
                "industry is the company/product market, not the job function, "
                "job family, or job_group."
            ),
        },
    }


def build_context(project: Path) -> dict:
    rows = _job_rows(project)
    candidates = _research_candidates(project)
    focused = _focused_ids(project)
    roles = []
    for job_id in focused:
        row = rows.get(job_id, {})
        candidate = candidates.get(job_id, {})
        if row or candidate:
            roles.append(_role_context(job_id, row, candidate))
    return {
        "schema": "interview-context-v1",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "project": project.name,
        "quality_contract": {
            "industry_thesis": (
                "Every prep-notes.md must ground Why this industry in a "
                "web-researched company/product market thesis."
            ),
            "not_job_function": (
                "Do not use job_group, role family, or function labels such as "
                "strategy/GTM/finance/BD as the industry."
            ),
            "retryable_quality": (
                "If validate_interview_prep.py reports QUALITY issues, rewrite "
                "The Whys from this context and rerun validation instead of "
                "dropping the artifact."
            ),
        },
        "roles": roles,
    }


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Build prep-interview context scaffold.")
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    project = args.project
    context = build_context(project)
    out = project / "interviews" / "interview-context.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(context, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"OK: wrote {out} ({len(context['roles'])} focused role context(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
