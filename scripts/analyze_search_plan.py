#!/usr/bin/env python3
"""Classify search plan readiness before capture shards run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


MIN_ARCHETYPE_PEERS = 2
DEFAULT_ADJACENT_PER_SEED_SOFT_MAX = 3
DEFAULT_ADJACENT_SOFT_MIN = 12
PROFILE_DERIVED_SOFT_MAX = 2

PROFILE_DRIVEN_TERMS = (
    "candidate profile",
    "candidate's profile",
    "candidate background",
    "candidate's background",
    "past experience",
    "prior experience",
    "previous experience",
    "former employer",
    "resume",
    "cv",
    "work history",
    "profile includes",
)

WEAK_DEFAULT_COMPANIES: dict[str, tuple[str, tuple[str, ...]]] = {
    "discord": ("consumer community/chat app", ("community", "chat", "discord")),
    "spotify": (
        "music/audio media platform",
        ("spotify", "music", "audio", "streaming media"),
    ),
    "lazada": (
        "e-commerce/retail marketplace",
        ("lazada", "ecommerce", "e-commerce", "retail"),
    ),
    "riotgames": ("gaming", ("gaming", "games", "esports", "riot")),
    "hoyoverse": ("gaming", ("gaming", "games", "esports", "hoyoverse")),
    "electronicarts": ("gaming", ("gaming", "games", "esports")),
    "ea": ("gaming", ("gaming", "games", "esports")),
    "roblox": ("gaming", ("gaming", "games", "esports", "roblox")),
}

WEAK_ADJACENCY_TERMS: dict[str, tuple[str, ...]] = {
    "gaming": ("gaming", "games", "game studio", "esports"),
    "e-commerce/retail": ("ecommerce", "e-commerce", "retail"),
    "consumer community/chat": ("community app", "chat app", "chat platform"),
    "music/audio": ("music", "audio streaming", "podcast"),
    "travel": ("travel platform", "airline", "tourism"),
}

BROAD_BREADTH_VALUES = {
    "broad",
    "wide",
    "full-market",
    "full_market",
    "comprehensive",
    "aggressive",
}

CLOSE_PEER_NAME_EXCEPTIONS = {
    "netflix",
    "snap",
    "snapinc",
}


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


def _project_metadata(project: Path) -> dict:
    merged: dict = {}
    for name in ("project.json", "project-meta.json"):
        data = _load_json(project / name, {})
        if isinstance(data, dict):
            merged.update(data)
    return merged


def _declared_seeds(project: Path) -> list[str]:
    seeds = _project_metadata(project).get("target_companies", [])
    if seeds:
        return [str(seed) for seed in seeds if str(seed).strip()]
    return []


def _universe_companies(universe: dict) -> list[dict]:
    out: list[dict] = []
    for bucket in universe.get("buckets", []) if isinstance(universe, dict) else []:
        for company in bucket.get("companies", []) if isinstance(bucket, dict) else []:
            if isinstance(company, dict) and company.get("name"):
                item = dict(company)
                item["_bucket"] = str(bucket.get("bucket", ""))
                item["_bucket_has_expansion_note"] = bool(
                    str(bucket.get("expansion_note", "")).strip()
                )
                out.append(item)
    return out


def _metadata_text(project_meta: dict) -> str:
    return json.dumps(project_meta, ensure_ascii=False).lower()


def _company_text(company: dict) -> str:
    fields = [
        company.get("name", ""),
        company.get("_bucket", ""),
        company.get("rationale", ""),
        company.get("why_relevant", ""),
        company.get("evidence", ""),
        company.get("notes", ""),
    ]
    return " ".join(str(field or "") for field in fields).lower()


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _explicit_broad_breadth(project_meta: dict, universe: dict) -> bool:
    values: list[str] = []
    for source in (project_meta, universe):
        if not isinstance(source, dict):
            continue
        for key in (
            "search_breadth",
            "universe_breadth",
            "company_universe_breadth",
            "expansion_mode",
            "breadth",
        ):
            value = source.get(key)
            if value is not None:
                values.append(str(value).strip().lower())
    return any(value in BROAD_BREADTH_VALUES for value in values)


def _weak_default_adjacency_reason(
    company: dict,
    declared_norms: set[str],
    project_text: str,
) -> str:
    name = str(company.get("name", ""))
    norm = _norm(name)
    if norm in declared_norms:
        return ""
    if norm in CLOSE_PEER_NAME_EXCEPTIONS:
        return ""
    label_terms = WEAK_DEFAULT_COMPANIES.get(norm)
    if label_terms:
        label, allow_terms = label_terms
        if not _has_any(project_text, allow_terms):
            return (
                f"{name}: weak default adjacency ({label}) for the current target "
                "seeds; keep it only if the user explicitly asked for that domain "
                "or the rationale ties it tightly to a seed/company thesis."
            )
    company_text = _company_text(company)
    for label, weak_terms in WEAK_ADJACENCY_TERMS.items():
        if _has_any(company_text, weak_terms) and not _has_any(project_text, weak_terms):
            return (
                f"{name}: weak default adjacency ({label}) for the current target "
                "seeds; exclude it by default unless the user requested that domain "
                "or the company rationale explains a close seed-level relationship."
            )
    return ""


def analyze(project: Path) -> dict:
    project = Path(project)
    targets = project / "targets"
    universe = _load_json(targets / "company-universe.json", {})
    plan = _load_json(targets / "source-plan.json", {})
    companies = _universe_companies(universe)
    project_meta = _project_metadata(project)
    project_text = _metadata_text(project_meta)
    explicit_broad = _explicit_broad_breadth(project_meta, universe)
    plan_companies = {
        _norm(c.get("name", "")): c
        for c in plan.get("companies", []) if isinstance(c, dict)
    } if isinstance(plan, dict) else {}
    declared = _declared_seeds(project)
    declared_norms = {_norm(seed) for seed in declared}
    seed_norms = set(declared_norms)
    seed_norms.update(
        _norm(c.get("name", "")) for c in companies
        if c.get("seed") or _norm(c.get("name", "")) in declared_norms
    )
    issues: list[str] = []

    if not companies:
        issues.append("company-universe.json has no companies; capture cannot run.")
    if not plan_companies:
        issues.append("source-plan.json has no companies; capture cannot run.")

    universe_norms = {_norm(c.get("name", "")): str(c.get("name", "")) for c in companies}
    excluded_norms = {
        _norm(str(e.get("name_or_bucket", "")))
        for e in universe.get("excluded", []) if isinstance(e, dict)
    } if isinstance(universe, dict) else set()
    exception_norms = set()
    for item in universe.get("expansion_exceptions", []) if isinstance(universe, dict) else []:
        if isinstance(item, dict):
            for key in ("seed", "seed_or_archetype", "bucket", "archetype", "name"):
                if item.get(key):
                    exception_norms.add(_norm(str(item[key])))
        else:
            exception_norms.add(_norm(str(item)))

    for seed in declared:
        ns = _norm(seed)
        if ns not in universe_norms and ns not in excluded_norms:
            issues.append(
                f"declared seed '{seed}' is absent from company-universe.json; "
                "seeds are a floor and must be included or explicitly excluded."
            )
        if ns not in plan_companies and ns not in excluded_norms:
            issues.append(
                f"declared seed '{seed}' is absent from source-plan.json; "
                "every searched universe company needs source coverage."
            )

    for company in companies:
        name = str(company.get("name", ""))
        if _norm(name) not in plan_companies:
            issues.append(f"universe company '{name}' is missing from source-plan.json.")

    bucket_members: dict[str, list[dict]] = {}
    for company in companies:
        bucket_members.setdefault(_norm(company.get("_bucket", "")), []).append(company)

    for seed_norm in sorted(seed_norms):
        seed = next((c for c in companies if _norm(c.get("name", "")) == seed_norm), None)
        if not seed:
            continue
        if seed_norm in excluded_norms or seed_norm in exception_norms:
            continue
        seed_bucket_norm = _norm(seed.get("_bucket", ""))
        if seed.get("_bucket_has_expansion_note") or seed_bucket_norm in exception_norms:
            continue
        peers = [
            c for c in bucket_members.get(seed_bucket_norm, [])
            if _norm(c.get("name", "")) != seed_norm
            and not c.get("seed")
            and _norm(c.get("name", "")) not in declared_norms
        ]
        if len(peers) < MIN_ARCHETYPE_PEERS:
            issues.append(
                f"{seed.get('name')}: seed archetype expanded into only "
                f"{len(peers)} non-seed peer(s); expand adjacent companies before capture "
                "or record an expansion_note/exclusion."
            )

    if declared and len(companies) <= len(declared):
        issues.append(
            "company universe is literal seed-only; adjacent company expansion is default "
            "behavior and must happen before capture."
        )

    if declared and not explicit_broad:
        non_seed_count = len([
            c for c in companies
            if _norm(c.get("name", "")) not in declared_norms and not c.get("seed")
        ])
        soft_max = max(DEFAULT_ADJACENT_SOFT_MIN,
                       len(declared) * DEFAULT_ADJACENT_PER_SEED_SOFT_MAX)
        if non_seed_count > soft_max:
            issues.append(
                f"default company universe has {non_seed_count} non-seed additions; "
                "default search should stay to close peers (roughly 1-3 per seed) "
                "rather than an arbitrary broad market map. Set expansion_mode=broad "
                "only when the user explicitly asks for a broad sweep."
            )

    profile_driven = [
        str(company.get("name", ""))
        for company in companies
        if _norm(company.get("name", "")) not in declared_norms
        and _has_any(_company_text(company), PROFILE_DRIVEN_TERMS)
    ]
    if len(profile_driven) > PROFILE_DERIVED_SOFT_MAX:
        issues.append(
            "candidate-profile-driven expansion includes "
            f"{len(profile_driven)} companies ({', '.join(profile_driven)}); "
            "candidate history should mainly inform fit scoring. Default search may "
            "add at most 1-2 exceptionally close profile-derived employers."
        )

    weak_issues = []
    for company in companies:
        issue = _weak_default_adjacency_reason(company, declared_norms, project_text)
        if issue:
            weak_issues.append(issue)
    issues.extend(weak_issues)

    status = "partial" if issues else "ok"
    return {
        "status": status,
        "issues": issues,
        "companies": len(companies),
        "source_plan_companies": len(plan_companies),
        "declared_seeds": declared,
    }


def _print_text(report: dict) -> None:
    print(
        f"{report['status'].upper()}: companies={report['companies']} "
        f"source_plan_companies={report['source_plan_companies']} "
        f"declared_seeds={len(report['declared_seeds'])}"
    )
    for item in report.get("issues", []):
        print(f"  PARTIAL: {item}")


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = argparse.ArgumentParser(description="Analyze RoleScout search plan readiness.")
    parser.add_argument("project", type=Path)
    parser.add_argument("--json", action="store_true")
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
    return 2 if report["status"] == "partial" else 0


if __name__ == "__main__":
    raise SystemExit(main())
