#!/usr/bin/env python3
"""Generate a deterministic search coverage audit from search artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def _company_names(universe: dict, plan: dict, cands: list[dict], queries: list[dict]) -> list[str]:
    names: dict[str, str] = {}
    for bucket in universe.get("buckets", []) if isinstance(universe, dict) else []:
        for company in bucket.get("companies", []) if isinstance(bucket, dict) else []:
            name = company.get("name") if isinstance(company, dict) else ""
            if name:
                names.setdefault(_norm(name), str(name))
    for company in plan.get("companies", []) if isinstance(plan, dict) else []:
        name = company.get("name") if isinstance(company, dict) else ""
        if name:
            names.setdefault(_norm(name), str(name))
    for item in cands + queries:
        name = item.get("company") if isinstance(item, dict) else ""
        if name:
            names.setdefault(_norm(name), str(name))
    return [names[k] for k in sorted(names)]


def _source_summary(plan: dict) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for company in plan.get("companies", []) if isinstance(plan, dict) else []:
        name = str(company.get("name", "")).strip()
        if not name:
            continue
        for source in company.get("sources", []) if isinstance(company, dict) else []:
            if isinstance(source, dict):
                out[name][str(source.get("status", "unknown") or "unknown")] += 1
    return out


def _candidate_summary(cands: list[dict]) -> dict[str, Counter]:
    out: dict[str, Counter] = defaultdict(Counter)
    for candidate in cands:
        if not isinstance(candidate, dict):
            continue
        company = str(candidate.get("company", "") or "(unknown)").strip()
        decision = str(candidate.get("decision", "") or "unknown").strip()
        count = candidate.get("count", 1)
        try:
            n = max(1, int(count or 1))
        except (TypeError, ValueError):
            n = 1
        out[company][decision] += n
    return out


def _query_lines(queries: list[dict]) -> list[str]:
    lines = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        scope = str(query.get("scope", "") or "query")
        company = str(query.get("company", "") or "")
        seen = query.get("results_seen", "")
        observed = str(query.get("observed", "") or "")
        q = str(query.get("q", "") or "")[:120]
        label = f"{scope}"
        if company:
            label += f" / {company}"
        detail = f"results_seen={seen}" if seen != "" else "results_seen=unknown"
        if observed:
            detail += f"; observed={observed}"
        if "pages_fetched" in query:
            detail += f"; pages={query.get('pages_fetched')}"
        if query.get("advertised_total") is not None:
            detail += f"; advertised_total={query.get('advertised_total')}"
        if "pagination_complete" in query:
            detail += f"; pagination_complete={query.get('pagination_complete')}"
        if q:
            detail += f"; q={q}"
        lines.append(f"- {label}: {detail}")
    return lines


def generate(project: Path) -> str:
    project = Path(project)
    targets = project / "targets"
    universe = _load_json(targets / "company-universe.json", {})
    plan = _load_json(targets / "source-plan.json", {})
    log = _load_json(targets / "research-log.json", {})
    if isinstance(log, list):
        runs = [r for r in log if isinstance(r, dict)]
    elif isinstance(log, dict):
        runs = [log]
    else:
        runs = []
    candidates: list[dict] = []
    queries: list[dict] = []
    failed_parts: list[dict] = []
    merge_status = "unknown"
    for run in runs:
        candidates.extend(c for c in run.get("candidates", []) if isinstance(c, dict))
        queries.extend(q for q in run.get("queries", []) if isinstance(q, dict))
        failed_parts.extend(p for p in run.get("failed_parts", []) if isinstance(p, dict))
        merge_status = str(run.get("merge_status", merge_status) or merge_status)

    source_counts = _source_summary(plan)
    cand_counts = _candidate_summary(candidates)
    company_names = _company_names(universe, plan, candidates, queries)
    linkedin = [q for q in queries if "linkedin" in str(q.get("scope", "")).lower()]
    low_coverage = [
        q for q in queries
        if isinstance(q.get("results_seen"), int) and q.get("results_seen", 99) <= 1
    ]

    lines = [
        "# Coverage Audit",
        "",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "## Run Status",
        "",
        f"- merge_status: {merge_status}",
        f"- companies_accounted: {len(company_names)}",
        f"- candidates_logged: {len(candidates)}",
        f"- queries_logged: {len(queries)}",
        f"- failed_parts: {len(failed_parts)}",
        "",
        "## Company Coverage",
        "",
    ]
    if company_names:
        for name in company_names:
            c = cand_counts.get(name, Counter())
            s = source_counts.get(name, Counter())
            decisions = ", ".join(f"{k}={v}" for k, v in sorted(c.items())) or "no candidates logged"
            sources = ", ".join(f"{k}={v}" for k, v in sorted(s.items())) or "no planned sources"
            lines.append(f"- {name}: {decisions}; sources: {sources}")
    else:
        lines.append("- No companies found in artifacts.")

    lines.extend(["", "## Query Coverage", ""])
    lines.extend(_query_lines(queries) or ["- No queries logged."])

    lines.extend(["", "## LinkedIn Jobs", ""])
    if linkedin:
        for query in linkedin:
            lines.append(
                f"- observed={query.get('observed', 'unknown')}; "
                f"results_seen={query.get('results_seen', 'unknown')}; "
                f"q={str(query.get('q', ''))[:120]}"
            )
    else:
        lines.append("- No LinkedIn Jobs observation logged.")

    lines.extend(["", "## Low Coverage Queries", ""])
    if low_coverage:
        for query in low_coverage:
            lines.append(
                f"- {query.get('scope', 'query')}: results_seen={query.get('results_seen')}; "
                f"q={str(query.get('q', ''))[:120]}"
            )
    else:
        lines.append("- None.")

    lines.extend(["", "## Failed Parts", ""])
    if failed_parts:
        for part in failed_parts:
            lines.append(f"- {part.get('part', 'unknown')}: {part.get('error', '')}")
    else:
        lines.append("- None.")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Generate coverage-audit.md.")
    parser.add_argument("project", type=Path)
    args = parser.parse_args(argv)
    out = args.project / "targets" / "coverage-audit.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(generate(args.project), encoding="utf-8")
    print(f"OK: wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
