#!/usr/bin/env python3
"""Merge search shard logs into targets/research-log.json.

Shard subagents write only targets/research-log.parts/<shard>.json. The lead
or runner uses this deterministic helper to concatenate queries, dedupe
candidates by canonical URL/job_id, and keep the richer candidate record.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from normalize_job_url import canonicalize as canonicalize_job_url


CANDIDATE_ID_FIELDS = {
    "company", "title", "job_id", "source_url", "job_page_url", "url",
    "decision",
}


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _canonical_url(url: str) -> str:
    url = str(url or "").strip()
    try:
        return canonicalize_job_url(url)
    except ValueError:
        return url.lower()


def _candidate_key(candidate: dict) -> str:
    job_id = str(candidate.get("job_id", "")).strip()
    if job_id:
        return "job_id:" + job_id
    for field in ("source_url", "job_page_url", "url"):
        url = str(candidate.get(field, "")).strip()
        if url:
            return "url:" + _canonical_url(url)
    return "synthetic:" + "|".join(
        str(candidate.get(k, "")).strip().lower()
        for k in ("company", "title", "location")
    )


def _richness(value) -> int:
    if isinstance(value, dict):
        return sum(_richness(v) for v in value.values())
    if isinstance(value, list):
        return sum(_richness(v) for v in value)
    return 1 if str(value or "").strip() else 0


def _merge_candidate(existing: dict, incoming: dict) -> dict:
    primary, secondary = (
        (incoming, existing) if _richness(incoming) > _richness(existing)
        else (existing, incoming)
    )
    merged = dict(primary)
    for key, value in secondary.items():
        if key not in merged or not str(merged.get(key, "")).strip():
            merged[key] = value
    return merged


def _looks_like_wrapper(candidate: dict) -> bool:
    """Detect a whole part accidentally nested inside candidates[].

    Search shards occasionally write a run-shaped object as one candidate:
    {"queries": [...], "candidates": [...]}. Treat that as schema drift at the
    merge boundary, not as a job row, so one malformed shard does not poison the
    final log.
    """
    if not isinstance(candidate.get("candidates"), list):
        return False
    return not any(str(candidate.get(k, "")).strip() for k in CANDIDATE_ID_FIELDS)


def _flatten_candidates(candidates: list, *, part_name: str) -> tuple[list[dict], list[dict]]:
    flat: list[dict] = []
    extra_queries: list[dict] = []
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError(f"{part_name}: candidate entries must be objects")
        if _looks_like_wrapper(item):
            nested_queries = item.get("queries", [])
            if not isinstance(nested_queries, list):
                raise ValueError(f"{part_name}: nested wrapper queries must be a list")
            nested_candidates = item.get("candidates", [])
            nested_flat, nested_extra = _flatten_candidates(
                nested_candidates, part_name=part_name)
            extra_queries.extend(q for q in nested_queries if isinstance(q, dict))
            extra_queries.extend(nested_extra)
            flat.extend(nested_flat)
        else:
            flat.append(item)
    return flat, extra_queries


def _load_part(path: Path) -> dict:
    # Shards can be written by Windows tools that emit UTF-8 with BOM. Accept it
    # at the merge boundary and keep RoleNavi-owned outputs no-BOM UTF-8.
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return {"queries": [], "candidates": data}
    if not isinstance(data, dict):
        raise ValueError(f"{path.name}: part must be an object or list")
    candidates = data.get("candidates", data.get("entries", []))
    queries = data.get("queries", [])
    if not isinstance(candidates, list):
        raise ValueError(f"{path.name}: candidates must be a list")
    if not isinstance(queries, list):
        raise ValueError(f"{path.name}: queries must be a list")
    candidates, nested_queries = _flatten_candidates(candidates, part_name=path.name)
    queries = queries + nested_queries
    return {"queries": queries, "candidates": candidates}


def _query_key(query: dict) -> tuple[str, str, str, str, str]:
    return (
        str(query.get("scope", "")).strip().lower(),
        str(query.get("company", "")).strip().lower(),
        str(query.get("attempt", "")).strip().lower(),
        str(query.get("observed", "")).strip().lower(),
        str(query.get("q", "")).strip().lower(),
    )


def _is_runner_owned_query(query: dict) -> bool:
    scope = str(query.get("scope", "")).strip().lower()
    return bool(query.get("runner_owned")) or scope == "linkedin jobs"


def _is_runner_owned_candidate(candidate: dict) -> bool:
    if candidate.get("runner_owned"):
        return True
    haystack = " ".join(
        str(candidate.get(field, ""))
        for field in ("source_url", "job_page_url", "url")
    ).lower()
    return "linkedin.com/jobs" in haystack or "linkedin.com/jobs/view" in haystack


def _load_preserved_runner_entries(out_path: Path) -> tuple[list[dict], list[dict]]:
    """Keep runner-owned observations when merge is rerun after probe/finalize.

    Shard merge is allowed to replace shard-owned candidate logs, but it must not
    erase observations appended by runner-owned helpers such as
    probe_linkedin_jobs.py. The post-run validator may regenerate coverage after
    finalize; preserving these entries keeps that later merge idempotent.
    """
    if not out_path.exists():
        return [], []
    try:
        data = json.loads(out_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return [], []
    if not isinstance(data, dict):
        return [], []
    queries = [
        dict(q) for q in data.get("queries", [])
        if isinstance(q, dict) and _is_runner_owned_query(q)
    ]
    candidates = [
        dict(c) for c in data.get("candidates", [])
        if isinstance(c, dict) and _is_runner_owned_candidate(c)
    ]
    return queries, candidates


def merge_parts(project: Path, parts_dir: Path | None = None,
                out_path: Path | None = None) -> dict:
    project = Path(project)
    parts_dir = parts_dir or project / "targets" / "research-log.parts"
    out_path = out_path or project / "targets" / "research-log.json"
    if not parts_dir.is_dir():
        raise FileNotFoundError(f"research parts directory missing: {parts_dir}")

    preserved_queries, preserved_candidates = _load_preserved_runner_entries(out_path)
    queries: list[dict] = []
    by_key: dict[str, dict] = {}
    part_names: list[str] = []
    failed_parts: list[dict] = []
    warnings: list[str] = []
    for path in sorted(parts_dir.glob("*.json")):
        try:
            part = _load_part(path)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            failed_parts.append({"part": path.name, "error": str(e)[:500]})
            continue
        part_names.append(path.name)
        queries.extend(part["queries"])
        for candidate in part["candidates"]:
            key = _candidate_key(candidate)
            if key in by_key:
                by_key[key] = _merge_candidate(by_key[key], candidate)
            else:
                by_key[key] = dict(candidate)

    for candidate in preserved_candidates:
        key = _candidate_key(candidate)
        if key in by_key:
            by_key[key] = _merge_candidate(by_key[key], candidate)
        else:
            by_key[key] = dict(candidate)

    query_keys = {_query_key(q) for q in queries}
    for query in preserved_queries:
        key = _query_key(query)
        if key not in query_keys:
            queries.append(query)
            query_keys.add(key)

    if not part_names:
        details = "; ".join(f"{p['part']}: {p['error']}" for p in failed_parts[:5])
        raise ValueError("no valid research part files to merge"
                         + (f" ({details})" if details else ""))
    if failed_parts:
        warnings.append(f"{len(failed_parts)} research part file(s) failed to merge")
    if preserved_queries or preserved_candidates:
        warnings.append("preserved runner-owned research-log entries from previous pass")

    payload = {
        "schema": "research-log-v1",
        "merge_status": "partial" if failed_parts else "ok",
        "merged_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parts": part_names,
        "failed_parts": failed_parts,
        "warnings": warnings,
        "queries": queries,
        "candidates": list(by_key.values()),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    return payload


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    ap = argparse.ArgumentParser(description="Merge search research-log parts.")
    ap.add_argument("project", type=Path)
    ap.add_argument("--parts-dir", type=Path)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    try:
        payload = merge_parts(args.project, args.parts_dir, args.out)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    label = "PARTIAL" if payload.get("merge_status") == "partial" else "OK"
    print(f"{label}: merged {len(payload['parts'])} part(s), "
          f"{len(payload['candidates'])} unique candidate(s)")
    for failed in payload.get("failed_parts", []):
        print(f"  WARN: skipped {failed['part']}: {failed['error']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
