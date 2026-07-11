#!/usr/bin/env python3
"""Classify search coverage quality after shard capture/finalization.

This is intentionally deterministic and stdlib-only. It does not decide which
companies should be in the universe; it checks whether the artifacts already
claiming to be in scope were actually pursued deeply enough to call the run
complete.

Exit codes:
  0 = coverage acceptable
  1 = artifact/read error
  2 = partial coverage; persist valid rows, but report the gaps
  3 = blocked; the run cannot make meaningful progress without an external fix
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


UNRESOLVED_CAPTURE_MARKERS = (
    "outbound_dns_blocked",
    "dns_error",
    "temporary failure in name resolution",
    "name or service not known",
    "nodename nor servname",
    "connection timed out",
    "connection refused",
    "network is unreachable",
    "js shell",
    "javascript shell",
    "requires javascript",
    "static html",
    "browser unavailable",
    "playwright unavailable",
    "browser runtime unavailable",
    "connector_error",
    "approval_required",
    "authwall",
    "signed_out",
    "verification_prompt",
    "captcha",
    "rate limit",
    "429",
    "403 forbidden",
)

PLAN_MARKERS = (
    "planned",
    "pending",
    "follow-up",
    "follow up",
    "followup",
    "deferred",
    "todo",
    "next pass",
    "next run",
)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _company_display(universe: dict, plan: dict, candidates: list[dict],
                     queries: list[dict]) -> dict[str, str]:
    names: dict[str, str] = {}
    for bucket in universe.get("buckets", []) if isinstance(universe, dict) else []:
        for company in bucket.get("companies", []) if isinstance(bucket, dict) else []:
            if isinstance(company, dict) and company.get("name"):
                names.setdefault(_norm(company["name"]), str(company["name"]))
    for company in plan.get("companies", []) if isinstance(plan, dict) else []:
        if isinstance(company, dict) and company.get("name"):
            names.setdefault(_norm(company["name"]), str(company["name"]))
    for item in candidates + queries:
        if isinstance(item, dict) and item.get("company"):
            names.setdefault(_norm(item["company"]), str(item["company"]))
    return names


def _all_candidates_and_queries(log) -> tuple[list[dict], list[dict]]:
    if isinstance(log, list):
        runs = [r for r in log if isinstance(r, dict)]
    elif isinstance(log, dict):
        runs = [log]
    else:
        runs = []
    candidates: list[dict] = []
    queries: list[dict] = []
    for run in runs:
        candidates.extend(c for c in run.get("candidates", []) if isinstance(c, dict))
        queries.extend(q for q in run.get("queries", []) if isinstance(q, dict))
    return candidates, queries


def _haystack(*values) -> str:
    return " ".join(str(v or "") for v in values).lower()


def _has_marker(text: str, markers: tuple[str, ...] = UNRESOLVED_CAPTURE_MARKERS) -> bool:
    return any(marker in text for marker in markers)


def _candidate_text(candidate: dict, queries_by_company: dict[str, list[dict]]) -> str:
    company_norm = _norm(candidate.get("company", ""))
    query_text = " ".join(
        _haystack(q.get("scope"), q.get("q"), q.get("observed"), q.get("note"))
        for q in queries_by_company.get(company_norm, [])
    )
    return _haystack(
        candidate.get("reason"),
        candidate.get("reason_code"),
        candidate.get("notes"),
        candidate.get("observed"),
        candidate.get("fallbacks_attempted"),
        query_text,
    )


def _planned_sources_by_company(plan: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = defaultdict(list)
    for company in plan.get("companies", []) if isinstance(plan, dict) else []:
        if not isinstance(company, dict):
            continue
        name = _norm(company.get("name", ""))
        if not name:
            continue
        for source in company.get("sources", []):
            if not isinstance(source, dict):
                continue
            source_type = str(source.get("type", "")).lower()
            if "linkedin" in source_type:
                continue
            if str(source.get("status", "")).lower() == "planned":
                out[name].append(source)
    return out


def _source_statuses_by_company(plan: dict) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for company in plan.get("companies", []) if isinstance(plan, dict) else []:
        if not isinstance(company, dict):
            continue
        name = _norm(company.get("name", ""))
        for source in company.get("sources", []):
            if isinstance(source, dict):
                out[name][str(source.get("status", "unknown") or "unknown").lower()] += 1
    return out


def analyze(project: Path) -> dict:
    project = Path(project)
    targets = project / "targets"
    universe = _load_json(targets / "company-universe.json", {})
    plan = _load_json(targets / "source-plan.json", {})
    log = _load_json(targets / "research-log.json", {})
    candidates, queries = _all_candidates_and_queries(log)
    names = _company_display(universe, plan, candidates, queries)
    issues: list[str] = []
    blocking: list[str] = []
    retry_companies: list[str] = []

    def add_retry(company_name: str) -> None:
        company_name = str(company_name or "").strip()
        if company_name and company_name not in retry_companies:
            retry_companies.append(company_name)

    cand_counts: dict[str, Counter] = defaultdict(Counter)
    queries_by_company: dict[str, list[dict]] = defaultdict(list)
    for candidate in candidates:
        company = _norm(candidate.get("company", ""))
        decision = str(candidate.get("decision", "") or "unknown")
        try:
            count = max(1, int(candidate.get("count", 1) or 1))
        except (TypeError, ValueError):
            count = 1
        cand_counts[company][decision] += count
    for query in queries:
        company = _norm(query.get("company", ""))
        if company:
            queries_by_company[company].append(query)

    kept_total = sum(counter.get("kept", 0) for counter in cand_counts.values())
    failed_total = sum(counter.get("failed_capture", 0) for counter in cand_counts.values())
    logged_total = sum(sum(counter.values()) for counter in cand_counts.values())

    planned_by_company = _planned_sources_by_company(plan)
    source_statuses = _source_statuses_by_company(plan)
    for company_norm, sources in sorted(planned_by_company.items()):
        if cand_counts[company_norm].get("kept", 0):
            continue
        display = names.get(company_norm, company_norm)
        labels = [
            " ".join(str(source.get(k, "")) for k in ("type", "url")).strip()
            for source in sources[:4]
        ]
        issues.append(
            f"{display}: 0 kept rows while non-LinkedIn source(s) remain planned: "
            + "; ".join(label for label in labels if label)
        )
        # A recorded pending_fallback means the shard already attempted the ladder and
        # hit an unresolved environment/source blocker. Retrying immediately in the
        # same run repeats the same failure and can multiply runtime by the full
        # universe size. Keep the run partial, but leave retry to a later explicit
        # run after the blocker clears.
        if cand_counts[company_norm].get("pending_fallback", 0) == 0:
            add_retry(display)

    for i, candidate in enumerate(candidates):
        if candidate.get("decision") != "failed_capture":
            continue
        company_norm = _norm(candidate.get("company", ""))
        display = str(candidate.get("company") or names.get(company_norm, "(unknown)"))
        text = _candidate_text(candidate, queries_by_company)
        if _has_marker(text):
            issues.append(
                f"{display}: failed_capture contains an unresolved capture/tooling "
                f"blocker; use pending_fallback or finish the fallback ladder "
                f"(candidate index {i})"
            )
            add_retry(display)
        if any(marker in text for marker in PLAN_MARKERS):
            issues.append(
                f"{display}: failed_capture still contains planned/pending follow-up "
                f"language; attempts only belong in fallbacks_attempted "
                f"(candidate index {i})"
            )
            add_retry(display)

    linkedin_queries = [
        q for q in queries
        if "linkedin" in str(q.get("scope", "")).lower()
        or "linkedin.com/jobs" in str(q.get("q", "")).lower()
    ]
    if linkedin_queries and kept_total <= 1:
        linkedin_observed = " ".join(str(q.get("observed", "")).lower()
                                     for q in linkedin_queries)
        if "connector_error" in linkedin_observed:
            issues.append(
                "LinkedIn Jobs probe recorded connector_error while the run had low "
                "yield; report as partial and provide rerun/login/browser next step."
            )

    if names and kept_total == 0:
        issues.append("No kept job rows were captured for a non-empty company universe.")

    if logged_total and failed_total / max(1, logged_total) >= 0.35 and kept_total <= 1:
        issues.append(
            f"High failed_capture ratio ({failed_total}/{logged_total}) with "
            f"{kept_total} kept row(s); coverage is too thin to report complete."
        )

    # If every planned source is blocked/failed and no rows were kept, the run is
    # blocked on environment/source access, not merely thin.
    if names and kept_total == 0 and not any(
        "ok" in statuses or "empty" in statuses or "planned" in statuses
        for statuses in source_statuses.values()
    ):
        if any(_has_marker(_haystack(q.get("observed"), q.get("note"), q.get("q")))
               for q in queries):
            blocking.append(
                "No kept rows and all observed source evidence is blocked/failed; "
                "fix network/browser/source access or rerun after the blocker clears."
            )

    status = "blocked" if blocking else "partial" if issues else "ok"
    return {
        "status": status,
        "issues": issues,
        "blocking": blocking,
        "companies": len(names),
        "candidates_logged": logged_total,
        "kept": kept_total,
        "failed_capture": failed_total,
        "linkedin_queries": len(linkedin_queries),
        "retry_companies": retry_companies,
    }


def _print_text(report: dict) -> None:
    label = report["status"].upper()
    print(
        f"{label}: companies={report['companies']} "
        f"candidates={report['candidates_logged']} kept={report['kept']} "
        f"failed_capture={report['failed_capture']} "
        f"linkedin_queries={report['linkedin_queries']}"
    )
    for item in report.get("blocking", []):
        print(f"  BLOCKED: {item}")
    for item in report.get("issues", []):
        print(f"  PARTIAL: {item}")


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Analyze RoleScout search coverage.")
    parser.add_argument("project", type=Path)
    parser.add_argument("--json", action="store_true",
                        help="print machine-readable report")
    args = parser.parse_args(argv)
    try:
        report = analyze(args.project)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_text(report)
    if report["status"] == "blocked":
        return 3
    if report["status"] == "partial":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
