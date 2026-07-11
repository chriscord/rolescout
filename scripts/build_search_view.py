#!/usr/bin/env python3
"""Build the deterministic UI-visible job list view after fast search.

The raw `data/job_list.csv` stays the capture store. This script writes
`data/job_list.visible.csv` from a lightweight filter plan:

  deterministic search -> optional lightweight filter-plan judgment
  -> deterministic view build

The plan intentionally filters only mechanical view constraints such as direct
posting URL quality, target-location compatibility, and clearly out-of-band
seniority. It does not score role fit.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).parent))

from job_url_policy import is_direct_posting_url  # noqa: E402
from location_normalize import (  # noqa: E402
    CITY_COUNTRIES,
    COUNTRY_ALIASES,
    normalize_location_value,
)
from normalize_job_url import canonicalize as canonicalize_job_url  # noqa: E402
from schema_defs import JOB_LIST_COLUMNS  # noqa: E402


REGION_COUNTRIES = {
    "Americas": {
        "Brazil", "Canada", "Mexico", "USA",
    },
    "APAC": {
        "Australia", "China", "Hong Kong", "India", "Japan", "Singapore",
        "South Korea", "Taiwan",
    },
    "EMEA": {
        "France", "Germany", "Ireland", "Italy", "Netherlands", "Poland",
        "Portugal", "Spain", "UAE", "United Kingdom",
    },
    "Europe": {
        "France", "Germany", "Ireland", "Italy", "Netherlands", "Poland",
        "Portugal", "Spain", "United Kingdom",
    },
    "LATAM": {
        "Brazil", "Mexico",
    },
}

NON_JOB_TITLE_PATTERNS = (
    r"^all teams$",
    r"^early careers$",
    r"^employee awards$",
    r"^engineering$",
    r"^here$",
    r"^interviewing$",
    r"^life at ",
    r"^sales$",
    r"^university$",
    r"^view this page",
    r"eeo policy$",
)

PROTECTED_LEVEL_PHRASES = (
    "assistant vice president",
    "assistant vp",
    "associate partner",
    "associate principal",
)


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")


def _csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=JOB_LIST_COLUMNS, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in JOB_LIST_COLUMNS})


def _canonical_country(value: str) -> str:
    raw = str(value or "").strip()
    return COUNTRY_ALIASES.get(raw.lower(), raw)


def _split_location_tags(value: str) -> list[str]:
    normalized = normalize_location_value(value)
    return [piece.strip() for piece in normalized.split(";") if piece.strip()]


def _tag_city_country(tag: str) -> tuple[str, str]:
    tag = tag.strip()
    if not tag:
        return "", ""
    if "," in tag:
        city, country = [piece.strip() for piece in tag.rsplit(",", 1)]
        return city, _canonical_country(country)
    if tag in REGION_COUNTRIES:
        return "", tag
    country = _canonical_country(tag)
    if country in set(COUNTRY_ALIASES.values()) or country in REGION_COUNTRIES:
        return "", country
    low = tag.lower()
    if low in CITY_COUNTRIES:
        return tag, CITY_COUNTRIES[low]
    return "", country


def _target_location_filter(target_locations: list[str]) -> dict[str, Any]:
    cities: list[str] = []
    countries: list[str] = []
    for value in target_locations:
        for tag in _split_location_tags(value):
            city, country = _tag_city_country(tag)
            if city and city not in cities:
                cities.append(city)
            if country and country not in countries:
                countries.append(country)
    return {
        "mode": "positive_target_country_or_city",
        "target_cities": sorted(cities),
        "target_countries": sorted(countries),
        "include_remote": True,
        "exclude_missing_location": True,
    }


def _is_entry_junior_target(target_level: str) -> bool:
    return bool(re.search(
        r"\b(intern|internship|student|graduate|new\s+grad|entry|junior)\b",
        str(target_level or "").lower(),
    ))


def _default_negative_level_terms(target_level: str) -> list[str]:
    level = str(target_level or "").lower()
    too_low = ["intern", "internship", "student", "graduate", "new grad", "entry", "junior"]
    too_high = ["head", "vp", "vice president", "cxo"]
    if _is_entry_junior_target(target_level):
        too_high = [*too_high, "chief", "director", "senior director"]
        return sorted(set([*too_high, "lead", "manager", "principal", "senior", "staff"]))
    if any(term in level for term in ("senior manager", "sr manager", "lead", "principal", "staff")):
        return [*too_low, "associate", "assistant", "senior director", *too_high]
    if "director" in level:
        return [*too_low, "associate", "assistant", "cxo"]
    if "manager" in level:
        return [*too_low, "associate", "assistant", "director", *too_high]
    if "senior" in level:
        return [*too_low, "intern", "student", "head", "vp", "vice president", "cxo"]
    return too_low


def default_filter_plan(project: Path) -> dict[str, Any]:
    meta = _read_json(project / "project-meta.json", {})
    target_locations = [
        str(item) for item in meta.get("target_locations", [])
        if str(item).strip()
    ]
    target_level = str(meta.get("target_level", ""))
    negative_terms = _default_negative_level_terms(target_level)
    return {
        "schema": "rolescout-search-view-filter-plan-v1",
        "generated_at": _now_utc(),
        "source": "deterministic_default",
        "target_level": target_level,
        "target_locations": target_locations,
        "location_filter": _target_location_filter(target_locations),
        "level_filter": {
            "mode": "negative_title_terms",
            "negative_terms": sorted(set(negative_terms)),
            "protected_phrases": list(PROTECTED_LEVEL_PHRASES),
        },
        "direct_posting_filter": {
            "mode": "strict_direct_posting_url",
            "exclude_non_posting_pages": True,
        },
    }


def _sanitize_filter_plan(project: Path, plan: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    changed = False
    meta = _read_json(project / "project-meta.json", {})
    target_level = str(plan.get("target_level") or meta.get("target_level", ""))
    target_locations = [
        str(item) for item in (plan.get("target_locations") or meta.get("target_locations", []))
        if str(item).strip()
    ]

    loc_filter = plan.get("location_filter", {})
    if not isinstance(loc_filter, dict):
        loc_filter = {}
    default_loc_filter = _target_location_filter(target_locations)
    for key in ("target_cities", "target_countries"):
        if not loc_filter.get(key):
            loc_filter[key] = default_loc_filter[key]
            changed = True
    if loc_filter.get("mode") == "positive_country_or_remote":
        loc_filter["mode"] = "positive_target_country_or_city"
        changed = True
    if loc_filter.get("exclude_missing_location") is not True:
        loc_filter["exclude_missing_location"] = True
        changed = True
    plan["location_filter"] = loc_filter

    level_filter = plan.get("level_filter", {})
    if not isinstance(level_filter, dict):
        level_filter = {}
    terms = [
        str(term).strip().lower()
        for term in level_filter.get("negative_terms", [])
        if str(term).strip()
    ]
    if _is_entry_junior_target(target_level):
        cleaned = [term for term in terms if term != "executive"]
        if cleaned != terms:
            terms = cleaned
            changed = True
        if "chief" not in terms:
            terms.append("chief")
            changed = True
    else:
        cleaned = [term for term in terms if term not in {"chief", "executive"}]
        if cleaned != terms:
            terms = cleaned
            changed = True
    deduped_terms = sorted(set(terms))
    if level_filter.get("negative_terms") != deduped_terms:
        level_filter["negative_terms"] = deduped_terms
        changed = True
    protected = [
        str(item).strip().lower()
        for item in level_filter.get("protected_phrases", PROTECTED_LEVEL_PHRASES)
        if str(item).strip()
    ]
    for phrase in PROTECTED_LEVEL_PHRASES:
        if phrase not in protected:
            protected.append(phrase)
            changed = True
    if level_filter.get("protected_phrases") != protected:
        level_filter["protected_phrases"] = protected
        changed = True
    plan["level_filter"] = level_filter
    return plan, changed


def _load_or_create_plan(project: Path, plan_path: Path | None = None) -> dict[str, Any]:
    path = plan_path or project / "targets" / "search-view-filter-plan.json"
    plan = _read_json(path, {})
    if isinstance(plan, dict) and plan.get("schema") == "rolescout-search-view-filter-plan-v1":
        plan, changed = _sanitize_filter_plan(project, plan)
        if changed:
            _write_json(path, plan)
        return plan
    plan = default_filter_plan(project)
    _write_json(path, plan)
    return plan


def _matches_level_filter(row: dict[str, str], plan: dict[str, Any]) -> tuple[bool, str]:
    title = str(row.get("title", "")).lower()
    if any(re.search(pattern, title) for pattern in NON_JOB_TITLE_PATTERNS):
        return False, "non_posting_title"
    level_filter = plan.get("level_filter", {}) if isinstance(plan, dict) else {}
    protected = [
        str(item).lower()
        for item in level_filter.get("protected_phrases", PROTECTED_LEVEL_PHRASES)
    ]
    if any(phrase in title for phrase in protected):
        return True, ""
    for term in level_filter.get("negative_terms", []):
        term = str(term).strip().lower()
        if not term:
            continue
        pattern = r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, title):
            return False, f"level_negative:{term}"
    return True, ""


def _row_countries(row: dict[str, str]) -> set[str]:
    known_countries = (
        set(COUNTRY_ALIASES.values()) |
        set(CITY_COUNTRIES.values()) |
        set(REGION_COUNTRIES)
    )
    countries: set[str] = set()
    for tag in _split_location_tags(row.get("location", "")):
        _, country = _tag_city_country(tag)
        if country in known_countries:
            countries.add(country)
    return countries


def _row_cities(row: dict[str, str]) -> set[str]:
    cities: set[str] = set()
    for tag in _split_location_tags(row.get("location", "")):
        city, _ = _tag_city_country(tag)
        if city and city != "-":
            cities.add(city.lower())
    return cities


def _matches_location_filter(row: dict[str, str], plan: dict[str, Any]) -> tuple[bool, str]:
    loc_filter = plan.get("location_filter", {}) if isinstance(plan, dict) else {}
    location = str(row.get("location", "")).strip()
    is_remote = str(row.get("remote_policy", "")).strip().lower() == "remote"
    target_cities = {
        str(item).strip().lower()
        for item in loc_filter.get("target_cities", [])
        if str(item).strip()
    }
    target_countries = {
        _canonical_country(item)
        for item in loc_filter.get("target_countries", [])
        if str(item).strip()
    }
    if not target_cities and not target_countries:
        return True, ""
    row_cities = _row_cities(row)
    if row_cities & target_cities:
        return True, ""
    row_countries = _row_countries(row)
    expanded_targets = set(target_countries)
    for target in list(target_countries):
        expanded_targets.update(REGION_COUNTRIES.get(target, set()))
    if row_countries:
        if row_countries & expanded_targets:
            return True, ""
        return False, "location_mismatch"
    if is_remote and loc_filter.get("include_remote", True):
        return True, ""
    if not location:
        return False, "missing_location"
    return False, "location_mismatch"


def _matches_direct_posting(row: dict[str, str]) -> tuple[bool, str]:
    url = str(row.get("job_page_url") or row.get("source_url") or "").strip()
    if not is_direct_posting_url(url):
        return False, "non_direct_or_non_posting_url"
    return True, ""


def _canonicalize_visible_urls(row: dict[str, str]) -> None:
    for key in ("source_url", "job_page_url"):
        value = str(row.get(key, "")).strip()
        if not value:
            continue
        try:
            row[key] = canonicalize_job_url(value)
        except ValueError:
            row[key] = value


def _reconcile_focused_jobs(project: Path, excluded: list[dict[str, str]]) -> None:
    focus_path = project / "data" / "focused-jobs.json"
    focus = _read_json(focus_path, {})
    if not isinstance(focus, dict):
        return
    ids = [str(item) for item in focus.get("job_ids", []) if str(item).strip()]
    if not ids:
        return
    excluded_by_id = {
        str(item.get("job_id", "")): str(item.get("reason", "hidden"))
        for item in excluded
        if str(item.get("job_id", "")).strip()
    }
    removed = [
        {"job_id": job_id, "reason": excluded_by_id[job_id]}
        for job_id in ids
        if job_id in excluded_by_id
    ]
    if not removed:
        return
    kept_ids = [job_id for job_id in ids if job_id not in excluded_by_id]
    _write_json(focus_path, {
        **focus,
        "job_ids": kept_ids,
        "updated_at": _now_utc()[:10],
        "auto_unfocused_hidden": removed,
        "auto_unfocused_hidden_at": _now_utc(),
    })


def build_view(project: Path, plan_path: Path | None = None) -> dict[str, Any]:
    plan = _load_or_create_plan(project, plan_path)
    rows = _csv_rows(project / "data" / "job_list.csv")
    kept: list[dict[str, str]] = []
    excluded: list[dict[str, str]] = []
    counts: dict[str, int] = {}
    for row in rows:
        checks = (
            _matches_direct_posting(row),
            _matches_location_filter(row, plan),
            _matches_level_filter(row, plan),
        )
        reason = next((reason for ok, reason in checks if not ok), "")
        if reason:
            counts[reason] = counts.get(reason, 0) + 1
            excluded.append({
                "job_id": row.get("job_id", ""),
                "company": row.get("company", ""),
                "title": row.get("title", ""),
                "source_url": row.get("source_url", ""),
                "reason": reason,
            })
        else:
            visible_row = dict(row)
            visible_row["location"] = normalize_location_value(visible_row.get("location", ""))
            _canonicalize_visible_urls(visible_row)
            kept.append(visible_row)

    _write_csv(project / "data" / "job_list.visible.csv", kept)
    summary = {
        "schema": "rolescout-search-view-summary-v1",
        "generated_at": _now_utc(),
        "project": project.name,
        "plan_source": plan.get("source", ""),
        "raw_rows": len(rows),
        "visible_rows": len(kept),
        "excluded_rows": len(excluded),
        "exclusion_counts": dict(sorted(counts.items())),
    }
    _write_json(project / "targets" / "search-view-summary.json", summary)
    _write_json(project / "targets" / "search-view-exclusions.json", {
        "schema": "rolescout-search-view-exclusions-v1",
        "generated_at": summary["generated_at"],
        "excluded": excluded[:5000],
    })
    _reconcile_focused_jobs(project, excluded)
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build UI-visible job_list view.")
    parser.add_argument("project", type=Path)
    parser.add_argument("--plan", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    project = args.project if args.project.is_absolute() else (ROOT / args.project)
    if not (project / "project.json").exists():
        project = ROOT / "projects" / str(args.project)
    if not (project / "project.json").exists():
        print(f"ERROR: project not found: {args.project}", file=sys.stderr)
        return 1
    summary = build_view(project, args.plan)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(
            f"OK: visible job view {summary['visible_rows']}/{summary['raw_rows']} "
            f"row(s); exclusions={summary['exclusion_counts']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
